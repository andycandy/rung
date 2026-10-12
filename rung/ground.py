"""Grounding: what the model said the request says -> checked facts, or questions.

Deterministic, and touching structured data only: HR records, e-mail syntax, ISO dates,
task keys. The only comparison involving words is a name the model extracted against the
full name on an HR record, token by token. Every person the model reports ends up
resolved, asked about, or rejected with a reason -- never dropped.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from rung.dates import is_iso_date
from rung.llm.calls import Understanding
from rung.syntax import placeholders
from rung.types import Gap

PUNCT = ".,;:!?'\"()[]"


@dataclass
class Grounded:
    ignore: bool = False                        # not a request: nothing to do
    task_type: str | None = None
    subjects: list = field(default_factory=list)  # resolved HR employees
    date: str | None = None
    date_text: str | None = None
    asks: list = field(default_factory=list)
    gaps: list = field(default_factory=list)
    questions: list = field(default_factory=list)
    flags: list = field(default_factory=list)


def _tokens(s: str) -> list[str]:
    return [t for t in (w.strip(PUNCT).casefold() for w in (s or "").split()) if t]


def _fmt(e: dict) -> str:
    return f"[{e['id']}] {e['full_name']} - {e['title']}, {e['team']}, {e['status']}, starts {e['start_date']}"


def find_person(ctx, hr, ref) -> list[dict]:
    """HR records for one PersonRef: exact work e-mail if the request gave one, else every
    token of the name as written must be a token of the record's full name."""
    if ref.email:
        want = ref.email.strip().casefold()
        return [e for e in hr.search(ctx, q=ref.email.strip()) if (e.get("work_email") or "").casefold() == want]
    toks = _tokens(ref.as_written)
    if not toks:
        return []
    cands = {}
    for t in toks:
        for e in hr.search(ctx, q=t):
            cands[e["id"]] = e
    return [e for e in cands.values() if set(toks) <= set(_tokens(e["full_name"]))]


def _by_role(ctx, hr, role_hint: str, statuses) -> list[dict]:
    want = set(_tokens(role_hint))
    out = []
    for st in statuses or []:
        out += [e for e in hr.search(ctx, status=st) if want and want <= set(_tokens(e.get("title")))]
    return out


def ground_people(ctx, hr, u: Understanding, proc: dict, answers: dict, g: Grounded):
    statuses = proc.get("subject", {}).get("statuses")
    task = proc["task_type"]
    if answers.get("subject"):
        try:
            emp = hr.probe(ctx, "employee", {"id": int(str(answers["subject"]).strip().lstrip("#[").rstrip("]"))})[0]
        except (ValueError, TypeError):
            emp = None
        if not emp or (statuses and emp["status"] not in statuses):
            g.questions.append({"key": "subject", "q": f"'{answers['subject']}' isn't a PeopleHub id this task applies to "
                                                       f"(needs status {'/'.join(statuses or [])}). Which person (id)?"})
            return
        g.subjects.append(emp)
        g.gaps.append(Gap("subject", emp["full_name"], "resolved", f"answer from requester (PeopleHub #{emp['id']})"))
        return
    if not u.people:
        g.questions.append({"key": "subject", "q": "Who is this for? I couldn't find a person named in the request."})
        return
    seen: dict[int, str] = {}
    for ref in u.people:
        label = ref.as_written + (f" <{ref.email}>" if ref.email and ref.email != ref.as_written else "")
        cands = find_person(ctx, hr, ref)
        relevant = [e for e in cands if not statuses or e["status"] in statuses]
        if len(relevant) == 1:
            e = relevant[0]
            if e["id"] in seen:   # the same person named twice ("Priya Sharma ... priya.sharma@acme.test")
                g.gaps.append(Gap("subject", e["full_name"], "resolved", f"'{label}' is the same person as '{seen[e['id']]}'"))
                continue
            seen[e["id"]] = label
            note = f"PeopleHub #{e['id']} ({e['status']}) from '{label}'" + (" by exact work e-mail" if ref.email else "")
            others = [c for c in cands if c is not e]
            if others:
                note += "; ignored (status not " + "/".join(statuses) + "): " + ", ".join(f"{c['full_name']} ({c['status']})" for c in others)
            g.subjects.append(e)
            g.gaps.append(Gap("subject", e["full_name"], "resolved", note))
        elif len(relevant) > 1:
            g.gaps.append(Gap("subject", None, "ask", f"'{label}' matches {len(relevant)} people"))
            g.questions.append({"key": "subject", "q": f"'{label}' matches several people: {'; '.join(_fmt(e) for e in relevant)}. Which one (id)?"})
        elif cands:
            states = ", ".join(f"{c['full_name']} is {c['status']}" for c in cands)
            g.gaps.append(Gap("subject", None, "ask", f"'{label}': {states}"))
            g.questions.append({"key": "subject", "q": f"{states} in PeopleHub, so there's nothing to {task} for them "
                                                       f"(this task needs status {'/'.join(statuses or [])}). Did you mean someone else?"})
        else:
            hint = _by_role(ctx, hr, ref.role_hint, statuses) if ref.role_hint else []
            opts = f" People with a matching title this applies to: {'; '.join(_fmt(e) for e in hint)}." if hint else ""
            how = "work e-mail" if ref.email else "name"
            g.gaps.append(Gap("subject", None, "ask", f"'{label}': no PeopleHub record with that {how}"))
            g.questions.append({"key": "subject", "q": f"I couldn't find '{label}' in PeopleHub.{opts} Which person (id)?"})


def ground_dates(u: Understanding, answers: dict, g: Grounded):
    if answers.get("date"):
        g.date, g.date_text = answers["date"], answers["date"]
        g.gaps.append(Gap("date", g.date, "resolved", "answer from requester"))
        return
    unsure = [d for d in u.dates if d.confidence == "low" or d.iso is None or not is_iso_date(d.iso)]
    sure = sorted({d.iso for d in u.dates if d not in unsure})
    if unsure:
        d = unsure[0]
        g.questions.append({"key": "date", "q": f"I couldn't tell which date you meant by '{d.as_written}'"
                                                + (f" (maybe {d.iso}?)" if d.iso and is_iso_date(d.iso) else "") + ". Which date (YYYY-MM-DD)?"})
        return
    if len(sure) > 1:
        said = ", ".join(f"'{d.as_written}' = {d.iso}" for d in u.dates)
        g.questions.append({"key": "date", "q": f"The request mentions more than one date ({said}). Which one applies (YYYY-MM-DD)?"})
        return
    if sure:
        g.date = sure[0]
        g.date_text = next(d.as_written for d in u.dates if d.iso == g.date)
        g.gaps.append(Gap("date", g.date, "resolved", f"model's reading of '{g.date_text}'"))


def uses_date(proc: dict) -> bool:
    """Does the procedure take a date from the request? (date checks, or a {date} placeholder anywhere)"""
    if set(proc.get("checks", [])) & {"date_agreement", "date_required"}:
        return True

    def strings(x):
        if isinstance(x, dict):
            for v in x.values():
                yield from strings(v)
        elif isinstance(x, list):
            for v in x:
                yield from strings(v)
        elif isinstance(x, str):
            yield x
    return any(ph.split(".")[0] in ("date", "date_text") for s in strings([proc.get("derive", []), proc.get("steps", [])])
               for ph in placeholders(s)) or any(d.get("fn") == "request_date" for d in proc.get("derive", []))


def ground(ctx, hr, u: Understanding, procs: dict, answers: dict) -> Grounded:
    g = Grounded(asks=list(u.explicit_asks))
    if not u.is_request and not answers:
        g.ignore = True
        return g
    if u.intent != "do" and answers.get("intent_ok") != "yes":
        reading = "a cancellation (or a 'don't')" if u.intent == "cancel" else "unclear about whether you want this done"
        g.questions.append({"key": "intent_ok", "q": f"I read your message as {reading}"
                                                     + (f" about {u.task}" if u.task else "") + ", so I won't act on it as written. "
                                                     "If you do want me to go ahead, reply 'intent_ok: yes'; otherwise tell me what should happen instead."})
        return g
    task = answers.get("task_type") or u.task
    if not task or task not in procs:
        g.questions.append({"key": "task_type", "q": f"I don't have a procedure for this yet. Known tasks: {', '.join(procs)}. "
                                                     "Point me to a doc describing how it's done (reply 'sop: <path on the Drive>') "
                                                     "and I'll draft a procedure for your approval."})
        return g
    if u.task_confidence == "low" and not answers.get("task_type"):
        g.questions.append({"key": "task_type", "q": f"I think this is '{task}', but I'm not sure. Which task is it? "
                                                     + "; ".join(f"{k}: {p.get('description', '')}" for k, p in procs.items())})
        return g
    g.task_type = task
    proc = procs[task]
    if proc.get("subject", {}).get("entity") == "employee":
        ground_people(ctx, hr, u, proc, answers, g)
    if uses_date(proc):
        ground_dates(u, answers, g)
    elif u.dates:   # e.g. "this month" in a report request: noted, not asked about, since the procedure takes no date
        g.gaps.append(Gap("date", None, "resolved", "not used by this task: " + ", ".join(d.as_written for d in u.dates)))
    if u.unclear:
        g.questions.append({"key": "clarify", "q": "Before I start: " + " ".join(f"({n}) {q}" for n, q in enumerate(u.unclear, 1))
                                                   + " (reply 'clarify: <your answers>')"})
    return g
