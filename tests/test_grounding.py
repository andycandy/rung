"""Grounding unit tests: hand-written `Understanding` objects (what a model might say)
fed straight into rung.ground against a small in-memory PeopleHub. No model, no sandbox."""
import pytest

from rung.ground import ground
from rung.llm.calls import DateRef, PersonRef, Understanding

ROWS = [
    {"id": 1, "full_name": "Arjun Mehta", "preferred_name": "Arjun", "work_email": "arjun.mehta@acme.test", "chat_username": "arjun.mehta",
     "title": "Head of Analytics", "team": "Analytics", "status": "active", "start_date": "2025-01-01"},
    {"id": 11, "full_name": "Priya Sharma", "preferred_name": "Priya", "work_email": "priya.sharma@acme.test", "chat_username": None,
     "title": "Data Analyst", "team": "Analytics", "status": "offer_accepted", "start_date": "2026-10-12"},
    {"id": 12, "full_name": "Karan Shah", "preferred_name": "Karan", "work_email": "karan.shah@acme.test", "chat_username": None,
     "title": "Data Engineer", "team": "Data Platform", "status": "offer_accepted", "start_date": "2026-10-12"},
    {"id": 13, "full_name": "Karan Malhotra", "preferred_name": "Karan", "work_email": "karan.malhotra@acme.test", "chat_username": None,
     "title": "Software Engineer", "team": "Engineering", "status": "offer_accepted", "start_date": "2026-10-12"},
    {"id": 14, "full_name": "Priya Nair", "preferred_name": "Priya", "work_email": "priya.nair@acme.test", "chat_username": "priya.nair",
     "title": "Accountant", "team": "Finance", "status": "active", "start_date": "2024-01-01"},
]


class FakeHR:
    """Same filters as the PeopleHub API: q is a substring of name/e-mail/username, the rest exact."""
    def search(self, ctx, q=None, status=None, title=None, team=None):
        out = []
        for e in ROWS:
            if q and not any(q.casefold() in (e.get(f) or "").casefold() for f in ("full_name", "preferred_name", "work_email", "chat_username")):
                continue
            if (status and e["status"] != status) or (title and e["title"] != title) or (team and e["team"] != team):
                continue
            out.append(dict(e))
        return out

    def probe(self, ctx, entity, key):
        return next((dict(e) for e in ROWS if e["id"] == key["id"]), None), "verified"


PROCS = {"onboarding": {"task_type": "onboarding", "description": "set up a new joiner", "checks": ["date_agreement"],
                        "subject": {"entity": "employee", "statuses": ["offer_accepted", "onboarding"]}},
         "offboarding": {"task_type": "offboarding", "description": "remove a leaver's access", "checks": ["date_required"],
                         "subject": {"entity": "employee", "statuses": ["active", "onboarding"]}},
         "joiners_report": {"task_type": "joiners_report", "description": "report", "subject": {"entity": "none"}}}


def U(**kw):
    base = dict(is_request=True, intent="do", task="onboarding", task_confidence="high", people=[], dates=[], explicit_asks=[], unclear=[])
    base.update(kw)
    return Understanding(**base)


def P(name, email=None, role=None):
    return PersonRef(as_written=name, email=email, role_hint=role)


def go(u, answers=None):
    return ground(None, FakeHR(), u, PROCS, answers or {})


def keys(g):
    return [q["key"] for q in g.questions]


# ------------------------------------------------------------------ people
def test_every_person_is_kept_one_by_name_one_by_email():
    g = go(U(people=[P("Priya"), P("karan.shah@acme.test", email="karan.shah@acme.test")]))
    assert [e["id"] for e in g.subjects] == [11, 12] and not g.questions


def test_full_name_tokens_must_all_match():
    g = go(U(people=[P("priya sharma")]))
    assert [e["id"] for e in g.subjects] == [11]
    g = go(U(people=[P("Priya Smith")]))          # 'smith' is in no full name: nobody, and we ask
    assert not g.subjects and keys(g) == ["subject"]


def test_status_filter_picks_the_pending_priya_and_says_so():
    g = go(U(people=[P("Priya")]))                 # Priya Nair is active: not someone to onboard
    assert [e["id"] for e in g.subjects] == [11]
    assert "Priya Nair (active)" in g.gaps[0].source


def test_ambiguous_name_asks_with_candidates_and_resolves_nobody():
    g = go(U(people=[P("Karan")]))
    assert not g.subjects and keys(g) == ["subject"]
    assert "[12] Karan Shah" in g.questions[0]["q"] and "[13] Karan Malhotra" in g.questions[0]["q"]


def test_same_person_named_twice_is_one_subject():
    g = go(U(people=[P("Priya Sharma"), P("priya.sharma@acme.test", email="priya.sharma@acme.test")]))
    assert [e["id"] for e in g.subjects] == [11] and not g.questions
    assert any("same person" in gp.source for gp in g.gaps)


def test_email_must_match_a_work_email_exactly():
    g = go(U(people=[P("Karan", email="karan@acme.test")]))   # no work e-mail is exactly this
    assert not g.subjects and keys(g) == ["subject"]


def test_description_without_a_name_asks_and_offers_role_matches():
    g = go(U(people=[P("the new analyst", role="analyst")]))
    assert not g.subjects and keys(g) == ["subject"]
    assert "[11] Priya Sharma" in g.questions[0]["q"]        # offered as an option, not chosen


def test_person_in_the_wrong_state_is_asked_about_not_dropped():
    g = go(U(people=[P("Priya Sharma"), P("Arjun Mehta")]))   # Arjun is active: nothing to onboard
    assert keys(g) == ["subject"] and "Arjun Mehta is active" in g.questions[0]["q"]
    assert len(g.gaps) == 2                                   # both people accounted for


def test_no_people_asks_who():
    g = go(U(people=[]))
    assert keys(g) == ["subject"] and not g.subjects


def test_subject_answer_overrides_and_is_checked():
    assert [e["id"] for e in go(U(people=[P("Karan")]), {"subject": "12"}).subjects] == [12]
    assert keys(go(U(people=[P("Karan")]), {"subject": "1"})) == ["subject"]   # Arjun: wrong status


def test_report_task_needs_no_person():
    g = go(U(task="joiners_report", people=[]))
    assert not g.questions and g.task_type == "joiners_report"


# ------------------------------------------------------------------ intent, task, request
@pytest.mark.parametrize("intent", ["cancel", "unclear"])
def test_not_do_asks_and_does_nothing(intent):
    g = go(U(intent=intent, people=[P("Priya Sharma")]))
    assert keys(g) == ["intent_ok"] and not g.subjects


def test_intent_confirmed_by_requester_proceeds():
    g = go(U(intent="cancel", people=[P("Priya Sharma")]), {"intent_ok": "yes"})
    assert [e["id"] for e in g.subjects] == [11]


def test_not_a_request_is_ignored():
    g = go(U(is_request=False, intent="unclear", task=None, task_confidence="low"))
    assert g.ignore and not g.questions


@pytest.mark.parametrize("task, conf", [(None, "high"), ("onboarding", "low")])
def test_unknown_or_unsure_task_asks(task, conf):
    g = go(U(task=task, task_confidence=conf, people=[P("Priya")]))
    assert keys(g) == ["task_type"] and not g.subjects


def test_unclear_items_are_asked_in_one_batch():
    g = go(U(people=[P("Priya")], unclear=["Which laptop?", "Which office?"]))
    assert keys(g) == ["clarify"] and "(1) Which laptop?" in g.questions[0]["q"] and "(2) Which office?" in g.questions[0]["q"]


# ------------------------------------------------------------------ dates
def test_confident_iso_date_is_used():
    g = go(U(people=[P("Priya")], dates=[DateRef(as_written="Monday", iso="2026-10-12", confidence="high")]))
    assert g.date == "2026-10-12" and g.date_text == "Monday" and not g.questions


@pytest.mark.parametrize("d", [DateRef(as_written="a week from Friday", iso="2026-10-16", confidence="low"),
                               DateRef(as_written="sometime soon", iso=None, confidence="high"),
                               DateRef(as_written="the 31st", iso="2026-09-31", confidence="high")])
def test_unsure_or_invalid_date_asks_and_never_defaults(d):
    g = go(U(people=[P("Priya")], dates=[d]))
    assert "date" in keys(g) and g.date is None


def test_two_different_dates_ask():
    g = go(U(people=[P("Priya")], dates=[DateRef(as_written="Monday", iso="2026-10-12", confidence="high"),
                                         DateRef(as_written="the 19th", iso="2026-10-19", confidence="high")]))
    assert "date" in keys(g) and g.date is None


def test_date_answer_wins():
    g = go(U(people=[P("Priya")], dates=[DateRef(as_written="later", iso=None, confidence="low")]), {"date": "2026-10-30"})
    assert g.date == "2026-10-30" and not g.questions


def test_iso_validator():
    from rung.dates import is_iso_date
    assert is_iso_date("2026-10-12")
    assert not any(is_iso_date(x) for x in ("2026-10-1", "2026-13-01", "12/10/2026", "Monday", None, "2026-10-12T09:00"))


def test_dates_are_not_asked_about_when_the_task_takes_none():
    g = go(U(task="joiners_report", dates=[DateRef(as_written="this month", iso=None, confidence="low")]))
    assert not g.questions and g.date is None and "not used by this task" in g.gaps[0].source


def test_real_procedures_declare_their_date_use():
    import yaml
    from rung.ground import uses_date
    procs = {n: yaml.safe_load(open(f"memory/procedures/{n}.yaml")) for n in ("onboarding", "offboarding", "joiners_report")}
    assert uses_date(procs["onboarding"]) and uses_date(procs["offboarding"]) and not uses_date(procs["joiners_report"])
