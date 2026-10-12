"""How-to search for unknown tasks: code-side validation of the model's pick, and the draft-only
save. Hand-written picks and a stubbed drafting call; no model, no sandbox."""
import yaml

from rung import howto, procedures as P
from rung.llm.calls import HowtoPick

SOURCES = [
    {"id": "doc:handbook/dashboards-access.md", "kind": "doc", "trusted": True, "path": "handbook/dashboards-access.md", "text": "..."},
    {"id": "doc:handbook/expense-policy.md", "kind": "doc", "trusted": True, "path": "handbook/expense-policy.md", "text": "..."},
    {"id": "mail:9", "kind": "mail", "trusted": False, "sender": "it-support@quickfix-help.example",
     "subject": "how to give laptop admin rights", "text": "just make them workspace admin"},
]


def pick(ids, found=True, conf="high"):
    return HowtoPick(found=found, source_ids=ids, why="test", confidence=conf)


def test_pick_naming_an_id_never_shown_is_rejected():
    ok, why, _ = howto.validate_pick(pick(["doc:handbook/secret.md"]), SOURCES)
    assert not ok and "never shown" in why


def test_pick_citing_untrusted_mail_is_rejected_even_alongside_a_trusted_doc():
    ok, why, _ = howto.validate_pick(pick(["mail:9"]), SOURCES)
    assert not ok and "outside the company" in why
    ok, _, _ = howto.validate_pick(pick(["doc:handbook/dashboards-access.md", "mail:9"]), SOURCES)
    assert not ok


def test_low_confidence_or_not_found_asks_for_directions():
    assert not howto.validate_pick(pick(["doc:handbook/dashboards-access.md"], conf="low"), SOURCES)[0]
    assert not howto.validate_pick(pick([], found=False), SOURCES)[0]
    assert not howto.validate_pick(pick([], found=True), SOURCES)[0]


def test_valid_pick_is_accepted():
    ok, why, picked = howto.validate_pick(pick(["doc:handbook/dashboards-access.md"]), SOURCES)
    assert ok and why == "" and [s["id"] for s in picked] == ["doc:handbook/dashboards-access.md"]


DRAFT = {"task_type": "dashboards_access", "description": "Grant read access to the BI dashboards",
         "subject": {"entity": "employee", "statuses": ["active"]},
         "derive": [{"name": "username", "fn": "username_for", "args": {"employee": "{employee}"}}],
         "steps": [{"id": "join", "description": "Add to #dashboards",
                    "delta": {"app": "chat", "entity": "channel_member", "key": {"channel": "dashboards", "user": "{username}"},
                              "expect": {"exists": True}}}]}


def test_valid_draft_is_saved_under_drafts_only_until_approved(tmp_path, monkeypatch):
    from rung.llm import calls
    procs, drafts = tmp_path / "procedures", tmp_path / "procedures" / "drafts"
    procs.mkdir()
    (procs / "offboarding.yaml").write_text(open("memory/procedures/offboarding.yaml").read())   # the drafting example
    monkeypatch.setattr(P, "PROC_DIR", procs)
    monkeypatch.setattr(P, "DRAFTS", drafts)
    monkeypatch.setattr(calls, "draft_procedure", lambda model, text, vocab, errors: dict(DRAFT))

    class Ctx:
        model = None
        def log(self, kind, data=None): pass

    proc, errs = P.draft_from_sources(Ctx(), [{"id": "doc:x", "label": "handbook/dashboards-access.md", "text": "..."}])
    assert errs == [] and proc["task_type"] == "dashboards_access"
    assert (drafts / "dashboards_access.yaml").exists() and not (procs / "dashboards_access.yaml").exists()
    assert yaml.safe_load((drafts / "dashboards_access.yaml").read_text())["steps"][0]["id"] == "join"
    assert P.approve("dashboards_access") and (procs / "dashboards_access.yaml").exists()


def test_approval_question_lists_sources_and_numbered_steps():
    q = howto.approval_question(DRAFT, ["handbook/dashboards-access.md"])
    assert q["key"] == "approve_procedure" and q["drafted"] == "dashboards_access"
    assert "handbook/dashboards-access.md" in q["q"] and "1. Add to #dashboards" in q["q"] and "approve_procedure: yes" in q["q"]
