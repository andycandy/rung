"""Understanding: request -> GoalSpec (resolved subject, gaps, questions, intents).

Split of responsibility:
  * the model (or the offline parser) only *extracts* what the request says;
  * code resolves it against systems of record, mines precedent, applies policy
    and builds the plan from a structured procedure.
Every unstated detail ends up as a Gap: resolved (with source), default, or ask.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

from rung import derive, policy, store
from rung.apps import registry
from rung.types import Citation, Delta, Gap, GoalSpec, Intent
from sandbox.common import DRIVE, now

PROC_DIR = Path(__file__).resolve().parent.parent / "memory" / "procedures"
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"]
# generic English only; candidate name tokens are further filtered by whether PeopleHub knows them
STOP = set(WEEKDAYS) | set(MONTHS) | {"please", "hi", "hey", "hello", "the", "a", "an", "our", "can", "could", "would", "you", "i",
                                      "we", "she", "he", "they", "her", "his", "their", "them", "today", "tomorrow", "next", "this",
                                      "thanks", "thank", "and", "also"}


def procedures() -> dict:
    return {p.stem: yaml.safe_load(p.read_text()) for p in sorted(PROC_DIR.glob("*.yaml"))}


# ------------------------------------------------------------------ extraction (model or offline)
def extract(ctx, request: str) -> dict:
    procs = procedures()
    if ctx.model:
        from rung.llm.calls import resolve_goal
        ctx.stats["llm_calls"] += 1
        out = resolve_goal(ctx.model, request, {k: v.get("match", []) for k, v in procs.items()})
        return out.model_dump()
    low = request.lower()
    scores = {k: sum(1 for m in p.get("match", []) if re.search(rf"\b{re.escape(m)}\b", low)) for k, p in procs.items()}
    top = max(scores.values()) if scores else 0
    winners = [k for k, v in scores.items() if v == top and v > 0]
    task = winners[0] if len(winners) == 1 else None
    if len(winners) > 1:
        return {"task_type": None, "ambiguous": winners, "person": None, "date_text": None, "notes": ""}
    names = [w for w in re.findall(r"\b[A-Z][a-z]+\b", request) if w.lower() not in STOP and
             w.lower() not in {m for p in procs.values() for mm in p.get("match", []) for m in mm.split()}]
    return {"task_type": task, "person": " ".join(names) or None, "date_text": _date_text(low), "notes": ""}


def _date_text(low: str):
    m = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", low)
    if m:
        return m.group(1)
    for d in WEEKDAYS + ["tomorrow"]:
        if re.search(rf"\b{d}\b", low):
            return d
    return None


def resolve_date(text: str | None) -> str | None:
    if not text:
        return None
    today = now().astimezone().date()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    if text == "tomorrow":
        return (today + timedelta(days=1)).isoformat()
    if text in WEEKDAYS:
        ahead = (WEEKDAYS.index(text) - today.weekday()) % 7 or 7
        return (today + timedelta(days=ahead)).isoformat()
    return None


# ------------------------------------------------------------------ entity resolution
def resolve_subject(ctx, proc: dict, person: str | None, answers: dict) -> tuple[dict | None, list[Gap]]:
    hr = registry.get("hr")
    statuses = proc["subject"].get("statuses")
    if answers.get("subject"):
        emp = hr.probe(ctx, "employee", {"id": int(answers["subject"])})[0]
        if not emp or (statuses and emp["status"] not in statuses):
            return None, [Gap("subject", None, "ask", question=f"PeopleHub #{answers['subject']} isn't someone this task applies to "
                                                               f"(needs status {statuses}). Who did you mean?")]
        return emp, [Gap("subject", emp["full_name"], "resolved", f"answer from requester (PeopleHub #{emp['id']})")]
    if not person:
        return None, [Gap("subject", None, "ask", question="Who is this for? I couldn't find a name in the request.")]
    found: dict[int, tuple[dict, int]] = {}
    for tok in person.split():
        for e in hr.search(ctx, q=tok):
            hits = sum(1 for t in person.lower().split() if t in e["full_name"].lower().split())
            found[e["id"]] = (e, max(hits, found.get(e["id"], (None, 0))[1]))
    if not found:
        return None, [Gap("subject", None, "ask", question=f"I couldn't find '{person}' in PeopleHub. What's their full name?")]
    best = max(h for _, h in found.values())
    cands = [e for e, h in found.values() if h == best]
    relevant = [e for e in cands if not statuses or e["status"] in statuses]
    if len(relevant) == 1:
        e = relevant[0]
        note = f"PeopleHub #{e['id']} ({e['status']})"
        if len(cands) > 1:
            note += f"; other matches ignored because their status isn't {statuses}: " + ", ".join(f"{c['full_name']} ({c['status']})" for c in cands if c is not e)
        return e, [Gap("subject", e["full_name"], "resolved", note)]
    pool = relevant or cands
    opts = "; ".join(f"[{e['id']}] {e['full_name']} - {e['title']}, {e['team']}, {e['status']}, starts {e['start_date']}" for e in pool)
    return None, [Gap("subject", None, "ask", question=f"'{person}' matches several people: {opts}. Which one (id)?")]


def username_from_name(full: str) -> str:
    return re.sub(r"[^a-z.]", "", full.lower().replace(" ", "."))


# ------------------------------------------------------------------ templating
class Scope(dict):
    def get_path(self, path):
        cur = self
        for part in path.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        return cur


def render(obj, scope: Scope):
    if isinstance(obj, dict):
        return {k: render(v, scope) for k, v in obj.items()}
    if isinstance(obj, list):
        return [render(v, scope) for v in obj]
    if isinstance(obj, str):
        m = re.fullmatch(r"\{([\w.]+)\}", obj)
        if m:
            v = scope.get_path(m.group(1))
            return v if v is not None else obj
        return re.sub(r"\{([\w.]+)\}", lambda mm: str(scope.get_path(mm.group(1)) if scope.get_path(mm.group(1)) is not None else mm.group(0)), obj)
    return obj


# ------------------------------------------------------------------ main entry
def understand(ctx, request: str, requester: str, answers: dict | None = None) -> GoalSpec:
    answers = answers or {}
    if answers.get("task_type"):
        ex = extract(ctx, request)
        ex["task_type"], ex["ambiguous"] = answers["task_type"], None
    else:
        ex = extract(ctx, request)
    ctx.log("understand.extract", ex)
    procs = procedures()
    if ex.get("ambiguous"):
        g = GoalSpec(request, requester, "unknown")
        g.questions.append({"key": "task_type", "q": f"This could be any of: {', '.join(ex['ambiguous'])}. Which one did you mean?"})
        return g
    if not ex.get("task_type") or ex["task_type"] not in procs:
        g = GoalSpec(request, requester, "unknown")
        g.questions.append({"key": "task_type", "q": f"I don't have a procedure for this yet. Known tasks: {', '.join(procs)}. "
                                                     "Can you point me to how it's done (a doc or a past example)?"})
        return g
    proc = procs[ex["task_type"]]
    goal = GoalSpec(request, requester, ex["task_type"])
    if proc.get("subject", {}).get("entity") == "employee":
        emp, gaps = resolve_subject(ctx, proc, ex.get("person"), answers)
        goal.gaps += gaps
    else:
        emp = None
    for gp in goal.gaps:
        if gp.how == "ask":
            goal.questions.append({"key": gp.field, "q": gp.question})
    if goal.questions:
        return goal
    build(ctx, goal, proc, emp, ex, answers)
    return goal


# ------------------------------------------------------------------ plan building (generic)
def _truthy(cond: str, scope: Scope) -> bool:
    neg = cond.startswith("not ")
    v = bool(scope.get_path(cond[4:] if neg else cond))
    return not v if neg else v


def _intent_from_step(step, scope, origin="sop", extra_cites=()):
    delta = Delta(**render(step["delta"], scope))
    it = Intent(description=render(step["description"], scope), delta=delta, args=render(step.get("args", {}), scope),
                irreversible=step.get("irreversible", False),
                not_before=render(step.get("not_before"), scope) if step.get("not_before") else None,
                origin=origin, citations=[Citation("sop", step.get("cite", ""))] + list(extra_cites))
    it.result["required"] = step.get("required", False)
    it.risk = policy.classify(it)
    return it


def build(ctx, goal: GoalSpec, proc: dict, emp: dict | None, ex: dict, answers: dict):
    """Generic plan builder. Procedures compose library checks and derivations
    (rung/derive.py) in YAML; adding a task type needs no code."""
    goal.subject = emp or {}
    scope = Scope(employee=emp or {}, drive=str(DRIVE), requester=goal.requester, request=goal.request,
                  date_text=ex.get("date_text"), _cites=[], _precedent_items=[])
    for chk in proc.get("checks", []):
        derive.CHECKS[chk](ctx, goal, proc, scope, answers)
    if goal.questions:
        return
    for d in proc.get("derive", []):
        scope[d["name"]] = derive.FNS[d["fn"]](ctx, goal, scope, **render(d.get("args", {}), scope))
    for step in proc["steps"]:
        if step.get("if") and not _truthy(step["if"], scope):
            continue
        if "foreach" in step:
            items = render(step["foreach"], scope)
            for item in items if isinstance(items, list) else []:
                s2 = Scope(scope, item=item)
                cites = [c for c in scope["_cites"] if c.ref == f"#{item}"]
                origin = "precedent" if item in scope["_precedent_items"] else "sop"
                goal.intents.append(_intent_from_step(step, s2, origin, cites))
        else:
            goal.intents.append(_intent_from_step(step, scope))
    # written policy filters everything, whatever its origin
    for it in goal.intents:
        for v in policy.violations(it, emp):
            it.status = "blocked"
            it.result["blocked"] = v
            src = next((c.quote for c in it.citations if c.source.startswith("precedent")), "")
            goal.flags.append({"kind": "policy_conflict",
                               "detail": f"Precedent suggests '{it.description}' ({src}) but {v['reason']} Skipped."})
        if it.origin == "precedent" and it.status != "blocked":
            key = f"{goal.task_type}:{(emp or {}).get('title')}:{it.delta.signature()}:{it.delta.key.get('channel')}"
            it.needs_confirmation = store.get_confirmation(key) != "yes"
            it.result["confirm_key"] = key
    # reversible first, irreversible last; scheduled steps after immediate ones
    goal.intents.sort(key=lambda i: (i.not_before is not None, i.irreversible))
