"""Understanding: request -> GoalSpec (resolved subject, gaps, questions, intents).

Split of responsibility:
  * the model (or the offline parser) only *extracts* what the request says;
  * code resolves it against systems of record, checks the requester's authority,
    mines precedent, applies policy and builds the plan from a structured procedure.
Every unstated detail ends up as a Gap: resolved (with source), default, or ask.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from rung import dates, derive, policy, store
from rung.apps import registry
from rung.types import Citation, Delta, Gap, GoalSpec, Intent
from sandbox.common import DRIVE, now

PROC_DIR = Path(__file__).resolve().parent.parent / "memory" / "procedures"
# generic English only; name candidates are further filtered by whether PeopleHub knows them
STOP = set(dates.WEEKDAYS) | set(dates.MONTHS) | {
    "please", "pls", "hi", "hey", "hello", "the", "a", "an", "our", "can", "could", "would", "will", "you", "i", "we", "me", "us",
    "she", "he", "they", "her", "his", "their", "them", "him", "today", "tomorrow", "next", "this", "thanks", "thank", "and",
    "also", "too", "is", "are", "was", "be", "to", "for", "of", "on", "in", "at", "with", "from", "as", "by", "it", "its",
    "new", "get", "set", "up", "sort", "out", "back", "us", "team", "starting", "starts", "start", "joining", "joins", "day",
    "last", "first", "week", "month", "end", "morning", "asap", "now", "all", "any", "some", "who", "what", "everyone",
    "has", "have", "had", "been", "said", "will", "just", "there", "here", "that", "so", "if", "not", "do", "does", "make", "add"}
NEGATION = re.compile(r"\b(don'?t|do not|not (be )?joining|isn'?t (joining|coming|starting)|no longer|cancel+(ed)?|withdr[ae]wn?|"
                      r"hold off|rescinded|never mind|not anymore|anymore)\b", re.I)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def procedures() -> dict:
    return {p.stem: yaml.safe_load(p.read_text()) for p in sorted(PROC_DIR.glob("*.yaml"))}


def _match(pattern: str, low: str) -> bool:
    if pattern.startswith("re:"):
        return bool(re.search(pattern[3:], low))
    return bool(re.search(rf"\b{re.escape(pattern)}\b", low))


# ------------------------------------------------------------------ extraction (model or offline)
def extract(ctx, request: str) -> dict:
    procs = procedures()
    today = now().astimezone().date()
    iso, phrase = dates.resolve(request, today)
    base = {"date": iso, "date_text": phrase, "negated": bool(NEGATION.search(request)), "emails": EMAIL.findall(request)}
    if ctx.model:
        from rung.llm.calls import resolve_goal
        ctx.stats["llm_calls"] += 1
        try:
            out = resolve_goal(ctx.model, request, {k: v.get("match", []) for k, v in procs.items()}).model_dump()
            return {**base, **{k: v for k, v in out.items() if v is not None}, "mentions": _merge_mentions(request, out.get("person"))}
        except Exception as ex:  # noqa: BLE001 -- fall back to the offline parser, and say so
            ctx.log("llm.error", {"call": "resolve_goal", "error": str(ex)})
    low = request.lower()
    scores = {k: sum(1 for m in p.get("match", []) if _match(m, low)) for k, p in procs.items()}
    top = max(scores.values()) if scores else 0
    winners = [k for k, v in scores.items() if v == top and v > 0]
    if len(winners) > 1:
        return {**base, "task_type": None, "ambiguous": winners, "mentions": _mentions(request)}
    return {**base, "task_type": winners[0] if winners else None, "mentions": _mentions(request)}


def _mentions(request: str, person: str | None = None) -> list[str]:
    """Runs of consecutive candidate-name words ('Priya', 'Karan Shah', 'priya sharma'), or e-mails."""
    procs = procedures()
    task_words = {w for p in procs.values() for m in p.get("match", []) if not m.startswith("re:") for w in m.split()}
    text = EMAIL.sub(" , ", person or request)
    out, cur = [], []
    for tok in re.findall(r"[A-Za-z][A-Za-z'.-]*|[^\sA-Za-z]+", text):
        w = re.sub(r"'s$|[.']$", "", tok).lower()
        if re.fullmatch(r"[a-z][a-z-]+", w) and w not in STOP and w not in task_words and len(w) > 1:
            cur.append(w)
        else:
            if cur:
                out.append(" ".join(cur))
            cur = []
    if cur:
        out.append(" ".join(cur))
    return out + EMAIL.findall(person or request)


def _merge_mentions(request: str, person: str | None) -> list[str]:
    """The model names one person; the request may name more ('Priya ... karan.shah@acme.test too').
    Keep the model's mentions and add every offline mention it doesn't already cover, so a
    second person is split out instead of silently dropped."""
    model = _mentions(request, person) if person else []
    covered = [set(m.split()) for m in model]
    extra = [m for m in _mentions(request) if m not in model and not any(set(m.split()) <= c for c in covered)]
    return model + extra


# ------------------------------------------------------------------ entity resolution
def _resolve_mention(ctx, mention: str):
    hr = registry.get("hr")
    if "@" in mention:
        return [e for e in hr.search(ctx, q=mention) if (e.get("work_email") or "").lower() == mention.lower()]
    toks = mention.split()
    known = [t for t in toks if hr.search(ctx, q=t)]   # drop words PeopleHub has never heard of ("accounts", "sorted")
    if not known:
        return []
    cands = {e["id"]: e for e in hr.search(ctx, q=known[0])}
    return [e for e in cands.values() if all(t in e["full_name"].lower().split() for t in known)]


def resolve_subjects(ctx, proc: dict, ex: dict, answers: dict) -> tuple[list[dict], list[Gap]]:
    """-> (resolved employees, gaps). More than one employee means the request names several people."""
    hr = registry.get("hr")
    statuses = proc["subject"].get("statuses")
    if answers.get("subject"):
        try:
            emp = hr.probe(ctx, "employee", {"id": int(str(answers["subject"]).strip().lstrip("#[").rstrip("]"))})[0]
        except (ValueError, TypeError):
            emp = None
        if not emp or (statuses and emp["status"] not in statuses):
            return [], [Gap("subject", None, "ask", question=f"'{answers['subject']}' isn't a PeopleHub id this task applies to "
                                                             f"(needs status {statuses}). Which person (id)?")]
        return [emp], [Gap("subject", emp["full_name"], "resolved", f"answer from requester (PeopleHub #{emp['id']})")]
    mentions = ex.get("mentions") or []
    resolved, gaps, seen = [], [], set()
    for m in mentions:
        cands = _resolve_mention(ctx, m)
        if not cands:
            continue
        relevant = [e for e in cands if not statuses or e["status"] in statuses]
        if len(relevant) == 1:
            e = relevant[0]
            if e["id"] in seen:
                continue
            seen.add(e["id"])
            note = f"PeopleHub #{e['id']} ({e['status']}) from '{m}'"
            others = [c for c in cands if c is not e]
            if others:
                note += "; ignored (status not " + "/".join(statuses) + "): " + ", ".join(f"{c['full_name']} ({c['status']})" for c in others)
            resolved.append(e)
            gaps.append(Gap("subject", e["full_name"], "resolved", note))
        elif len(relevant) > 1:
            opts = "; ".join(f"[{e['id']}] {e['full_name']} - {e['title']}, {e['team']}, {e['status']}, starts {e['start_date']}" for e in relevant)
            gaps.append(Gap("subject", None, "ask", question=f"'{m}' matches several people: {opts}. Which one (id)?"))
        else:
            states = ", ".join(f"{c['full_name']} is {c['status']}" for c in cands)
            gaps.append(Gap("subject", None, "ask", question=f"{states} in PeopleHub, so there's nothing to {proc['task_type']} "
                                                             f"(this task needs status {'/'.join(statuses)}). Did you mean someone else?"))
    if not resolved and not gaps:
        gaps.append(Gap("subject", None, "ask", question="Who is this for? I couldn't match a name in the request to anyone in PeopleHub."))
    return resolved, gaps


def authorize(ctx, proc: dict, requester: str, subject: dict | None) -> str | None:
    """Requester must be an active internal employee with authority for this task. -> reason if denied."""
    rules = policy.policy().get("requesters", {})
    if not policy.trusted_sender(requester):
        return f"{requester} is not a company address; requests are only accepted from {', '.join(rules.get('internal_domains', []))}."
    hr = registry.get("hr")
    me = next((e for e in hr.search(ctx, q=requester) if (e.get("work_email") or "").lower() == requester.lower()), None)
    if not me or me["status"] != "active":
        return f"{requester} is not an active employee in PeopleHub."
    auth = rules.get("authority", {})
    allowed = auth.get(proc["task_type"], auth.get("default", []))
    for rule in allowed:
        if rule == "manager_of_subject" and subject and subject.get("manager_id") == me["id"]:
            return None
        if rule.startswith("team:") and me.get("team") == rule[5:]:
            return None
        if rule.startswith("title:") and re.fullmatch(rule[6:].replace("*", ".*"), me.get("title") or "", re.I):
            return None
    return f"{me['full_name']} ({me['title']}, {me['team']}) isn't authorised to request {proc['task_type']}" + \
        (f" for {subject['full_name']}" if subject else "") + f" (allowed: {', '.join(allowed)})."


def username_from_name(full: str) -> str:
    return re.sub(r"[^a-z.]", "", full.lower().replace(" ", "."))


def resolve_date(text):  # kept for derive.date_agreement callers
    return dates.resolve(text or "", now().astimezone().date())[0]


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
    ex = extract(ctx, request)
    if answers.get("task_type"):
        ex["task_type"], ex["ambiguous"] = answers["task_type"], None
    if answers.get("date"):
        ex["date"] = answers["date"]
    ctx.log("understand.extract", ex)
    procs = procedures()
    g = GoalSpec(request, requester, ex.get("task_type") or "unknown")
    if ex.get("negated") and not answers.get("negation_ok"):
        g.questions.append({"key": "negation_ok", "q": "Your message sounds like a cancellation or a 'don't'. I won't act on it as written. "
                                                      "If you do want me to go ahead, reply 'negation_ok: yes'; otherwise tell me what should happen instead."})
        return g
    if ex.get("ambiguous"):
        g.questions.append({"key": "task_type", "q": f"This could be any of: {', '.join(ex['ambiguous'])}. Which one did you mean?"})
        return g
    if (not ex.get("task_type") or ex["task_type"] not in procs) and answers.get("sop"):
        from rung import procedures as P
        if answers.get("approve_procedure") == "yes" and answers.get("drafted") and P.approve(answers["drafted"]):
            ctx.log("procedure.approved", {"task_type": answers["drafted"], "by": requester})
            procs = procedures()
            ex["task_type"] = answers["drafted"]
            g.task_type = ex["task_type"]
            g.flags.append({"kind": "procedure_learned", "detail": f"New procedure '{ex['task_type']}' drafted from {answers['sop']} and approved by {requester}."})
        elif not ctx.model:
            g.questions.append({"key": "sop", "q": "Drafting a procedure from a prose document needs a language model, and none is configured "
                                                   "(set RUNG_MODEL). Until then this task can't be automated."})
            return g
        else:
            proc, errs = P.draft_from_sop(ctx, answers["sop"])
            if not proc:
                g.questions.append({"key": "sop", "q": f"I couldn't turn {answers['sop']} into a valid procedure: {errs[:3]}. Is there a clearer doc?"})
                return g
            steps = "\n".join(f"  {i + 1}. {st['description']} -> {st['delta']}" for i, st in enumerate(proc["steps"]))
            g.questions.append({"key": "approve_procedure", "q": f"I drafted procedure '{proc['task_type']}' from {answers['sop']}:\n{steps}\n"
                                                                f"Approve it for future use? (approve_procedure: yes / no)", "drafted": proc["task_type"]})
            return g
    if not ex.get("task_type") or ex["task_type"] not in procs:
        g.questions.append({"key": "task_type", "q": f"I don't have a procedure for this yet. Known tasks: {', '.join(procs)}. "
                                                     "Point me to a doc describing how it's done (reply 'sop: <path on the Drive>') "
                                                     "and I'll draft a procedure for your approval."})
        return g
    if ex.get("date_text") and not ex.get("date"):
        g.questions.append({"key": "date", "q": f"I couldn't tell which date you meant by '{ex['date_text']}'. Which date (YYYY-MM-DD)?"})
        return g
    proc = procs[ex["task_type"]]
    emps = [None]
    if proc.get("subject", {}).get("entity") == "employee":
        emps, gaps = resolve_subjects(ctx, proc, ex, answers)
        g.gaps += gaps
        g.questions += [{"key": gp.field, "q": gp.question} for gp in gaps if gp.how == "ask"]
        if g.questions:
            return g
        if len(emps) > 1:
            g.split = [e["id"] for e in emps]
            return g
    denied = authorize(ctx, proc, requester, emps[0])
    if denied:
        g.flags.append({"kind": "unauthorized", "detail": denied})
        g.rejected = denied
        return g
    g.gaps.append(Gap("requester", requester, "resolved", "authorised per policies.yaml requesters"))
    build(ctx, g, proc, emps[0], ex, answers)
    return g


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
                  date=ex.get("date"), date_text=ex.get("date_text"), _cites=[], _precedent_items=[])
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
