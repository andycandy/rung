"""Understanding: request -> GoalSpec (resolved subject, gaps, questions, intents).

Split of responsibility:
  * the model reads the request -- one typed call, `understand_request` -- and later proposes
    the variable steps of the plan from gathered evidence (`propose_steps`);
  * code grounds what the model said against systems of record (rung.ground), checks the
    requester's authority, validates every proposed step (rung.proposals), applies policy and
    builds the plan from a structured procedure.
Code never reads the request for meaning. If the model can't produce a valid reading, the
run escalates; there is no parser to fall back to.
"""
from __future__ import annotations

import fnmatch
import string
from datetime import timedelta
from pathlib import Path

import yaml

from rung import derive, policy, precedent, proposals, store
from rung.apps import registry
from rung.ground import Grounded, ground
from rung.types import Citation, Delta, Gap, GoalSpec, Intent
from sandbox.common import DRIVE, now

PROC_DIR = Path(__file__).resolve().parent.parent / "memory" / "procedures"
NO_MODEL = "Rung needs a model (set RUNG_MODEL)"


def procedures() -> dict:
    return {p.stem: yaml.safe_load(p.read_text()) for p in sorted(PROC_DIR.glob("*.yaml"))}


def known_tasks(procs: dict) -> dict:
    return {k: p.get("description", "") for k, p in procs.items()}


def today_str(t=None) -> str:
    """Today plus a four-week calendar. Models are unreliable at date arithmetic ("Monday" from a
    Thursday came back as a Tuesday), so code supplies the facts and the model only looks them up."""
    t = t or now().date()
    days = " | ".join(f"{(t + timedelta(days=i)).strftime('%a')} {(t + timedelta(days=i)).isoformat()}" + (" (today)" if i == 0 else "")
                      for i in range(28))
    return f"{t.isoformat()} ({t.strftime('%A')}), timezone Asia/Kolkata (UTC+05:30)\nCalendar: {days}"


def _requester(ctx, requester: str) -> dict | None:
    hr = registry.get("hr")
    return next((e for e in hr.search(ctx, q=requester) if (e.get("work_email") or "").casefold() == requester.casefold()), None)


def requester_refused(ctx, requester: str) -> str | None:
    """Only active employees at a company address may ask at all. -> reason if not."""
    rules = policy.policy().get("requesters", {})
    if not policy.trusted_sender(requester):
        return f"{requester} is not a company address; requests are only accepted from {', '.join(rules.get('internal_domains', []))}."
    me = _requester(ctx, requester)
    if not me or me["status"] != "active":
        return f"{requester} is not an active employee in PeopleHub."
    return None


def authorize(ctx, proc: dict, requester: str, subject: dict | None) -> str | None:
    """Requester must be an active internal employee with authority for this task. -> reason if denied."""
    rules = policy.policy().get("requesters", {})
    refused = requester_refused(ctx, requester)
    if refused:
        return refused
    me = _requester(ctx, requester)
    auth = rules.get("authority", {})
    allowed = auth.get(proc["task_type"], auth.get("default", []))
    for rule in allowed:
        if rule == "manager_of_subject" and subject and subject.get("manager_id") == me["id"]:
            return None
        if rule.startswith("team:") and me.get("team") == rule[5:]:
            return None
        if rule.startswith("title:") and fnmatch.fnmatchcase((me.get("title") or "").casefold(), rule[6:].casefold()):
            return None
    return f"{me['full_name']} ({me['title']}, {me['team']}) isn't authorised to request {proc['task_type']}" + \
        (f" for {subject['full_name']}" if subject else "") + f" (allowed: {', '.join(allowed)})."


def username_from_name(full: str) -> str:
    return "".join(c for c in full.casefold().replace(" ", ".") if c in string.ascii_lowercase + ".")


# ------------------------------------------------------------------ templating (our own YAML templates)
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
        parts = list(string.Formatter().parse(obj))
        if len(parts) == 1 and parts[0][0] == "" and parts[0][1]:   # exactly "{name}": keep the value's type
            v = scope.get_path(parts[0][1])
            return v if v is not None else obj
        out = []
        for literal, name, _, _ in parts:
            out.append(literal)
            if name is not None:
                v = scope.get_path(name)
                out.append(str(v) if v is not None else "{" + name + "}")
        return "".join(out)
    return obj


# ------------------------------------------------------------------ main entry
def understand(ctx, request: str, requester: str, answers: dict | None = None) -> GoalSpec:
    from rung.llm.calls import understand_request
    if ctx.model is None:
        raise RuntimeError(NO_MODEL)
    answers = dict(answers or {})
    procs = procedures()
    # who may ask at all is a fact about the requester, checked before the model reads anything,
    # so an outsider learns nothing about the company from our questions
    refused = requester_refused(ctx, requester)
    if refused:
        g = GoalSpec(request, requester, "unknown")
        g.flags.append({"kind": "unauthorized", "detail": refused})
        g.rejected = refused
        return g
    u = understand_request(ctx.model, request, requester, known_tasks(procs), today_str(),
                           [answers["clarify"]] if answers.get("clarify") else None)
    ctx.log("understand.model", u.model_dump())
    g = GoalSpec(request, requester, answers.get("task_type") or u.task or "unknown")
    if answers.get("sop"):   # the requester pointed at a written procedure for this: draft from it, whatever the model guessed
        if not _sop_flow(ctx, g, answers, requester):
            return g
        procs = procedures()
        answers["task_type"] = g.task_type
    gr = ground(ctx, registry.get("hr"), u, procs, answers)
    g.gaps += gr.gaps
    g.questions += gr.questions
    g.flags += gr.flags
    if gr.ignore:
        g.ignored = True
        return g
    if gr.task_type:
        g.task_type = gr.task_type
    if g.questions:
        return g
    proc = procs[gr.task_type]
    if len(gr.subjects) > 1:
        g.split = [e["id"] for e in gr.subjects]
        return g
    emp = gr.subjects[0] if gr.subjects else None
    denied = authorize(ctx, proc, requester, emp)
    if denied:
        g.flags.append({"kind": "unauthorized", "detail": denied})
        g.rejected = denied
        return g
    g.gaps.append(Gap("requester", requester, "resolved", "authorised per policies.yaml requesters"))
    build(ctx, g, proc, emp, gr, answers)
    return g


def _sop_flow(ctx, g: GoalSpec, answers: dict, requester: str) -> bool:
    """Unknown task + a pointer to a prose SOP: draft a procedure, get it approved. -> True once approved."""
    from rung import procedures as P
    if answers.get("approve_procedure") == "yes" and answers.get("drafted") and P.approve(answers["drafted"]):
        ctx.log("procedure.approved", {"task_type": answers["drafted"], "by": requester})
        g.task_type = answers["drafted"]
        g.flags.append({"kind": "procedure_learned", "detail": f"New procedure '{g.task_type}' drafted from {answers['sop']} and approved by {requester}."})
        return True
    proc, errs = P.draft_from_sop(ctx, answers["sop"])
    if not proc:
        g.questions.append({"key": "sop", "q": f"I couldn't turn {answers['sop']} into a valid procedure: {errs[:3]}. Is there a clearer doc?"})
        return False
    steps = "\n".join(f"  {i + 1}. {st['description']} -> {st['delta']}" for i, st in enumerate(proc["steps"]))
    g.questions.append({"key": "approve_procedure", "q": f"I drafted procedure '{proc['task_type']}' from {answers['sop']}:\n{steps}\n"
                                                        f"Approve it for future use? (approve_procedure: yes / no)", "drafted": proc["task_type"]})
    return False


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


def variable_steps(ctx, goal: GoalSpec, proc: dict, emp: dict | None, scope: Scope, asks: list[str], fixed: list[Intent]) -> list[Intent]:
    """Evidence -> model proposes -> code validates. Covers the procedure's variable part
    (`propose:` in its YAML) and the requester's explicit asks."""
    spec = proc.get("propose")
    if not spec and not asks:
        return []
    if not emp:
        for a in asks:
            goal.flags.append({"kind": "refused_request", "detail": f"You asked for '{a}': not done. {proc['task_type']} has no per-person steps "
                                                                    "to attach it to; please send it as its own request."})
        return []
    from rung.llm.calls import propose_steps
    spec = spec or {"about": "Only the requester's explicit asks; nothing else.", "evidence": []}
    username = scope.get("username") or emp.get("chat_username")
    subject = {"id": emp["id"], "full_name": emp["full_name"], "title": emp.get("title"), "team": emp.get("team"), "username": username}
    evidence = precedent.gather(ctx, emp, spec, asks)
    for e in evidence:   # code's own flag, from the structured trust flag, whatever the model makes of it
        if not e["trusted"]:
            goal.flags.append({"kind": "suspicious_instruction", "source": e["id"],
                               "detail": f"{e['kind']} {e['id']} from {e.get('sender', '?')} is outside the company's trusted domains: "
                                         f"shown to the planner as evidence, but it can't justify any step ({e.get('subject', '')!r})."})
    steps = propose_steps(ctx.model, goal.task_type, spec["about"], subject, proposals.vocabulary(),
                          [f"{i.description}: {i.delta.describe()}" for i in fixed], evidence, asks, int(spec.get("min_steps", 0)))
    ctx.log("proposals.model", {"steps": [s.model_dump() for s in steps]})
    needs_channels = any("channel" in (s.delta.get("key") or {}) for s in steps) or store.corrections(goal.task_type)
    channels = set(registry.get("chat").channels(ctx)) if needs_channels else set()
    intents, flags = proposals.validate(steps, evidence, subject, goal.task_type, channels, [i.delta for i in fixed],
                                        allowed=set(spec.get("entities", [])))
    proposals.apply_corrections(intents, flags, goal.task_type, emp, username, channels)
    goal.flags += flags
    ctx.log("proposals.validated", {"planned": [i.delta.describe() for i in intents], "flags": flags})
    goal.gaps.append(Gap("proposed_steps", [i.description for i in intents] or "none", "resolved",
                         f"model proposal from {len(evidence)} evidence items ({', '.join(e['id'] for e in evidence)}); "
                         f"validated in code, {len(flags)} flagged"))
    if asks:   # every ask is accounted for in the report, whatever the model made of it
        planned = [i.description for i in intents if i.origin == "request"]
        goal.flags.append({"kind": "request_asks", "detail": f"You asked: {'; '.join(asks)}. Planned from your asks: "
                                                             f"{', '.join(planned) or 'nothing'} (refusals, if any, are listed above)."})
    return intents


def build(ctx, goal: GoalSpec, proc: dict, emp: dict | None, gr: Grounded, answers: dict):
    """Generic plan builder. Procedures compose library checks and derivations
    (rung/derive.py) in YAML; adding a task type needs no code."""
    goal.subject = emp or {}
    scope = Scope(employee=emp or {}, drive=str(DRIVE), requester=goal.requester, request=goal.request,
                  date=gr.date, date_text=gr.date_text)
    for chk in proc.get("checks", []):
        derive.CHECKS[chk](ctx, goal, proc, scope, answers)
    if goal.questions:
        return
    for d in proc.get("derive", []):
        scope[d["name"]] = derive.FNS[d["fn"]](ctx, goal, scope, **render(d.get("args", {}), scope))
    after = (proc.get("propose") or {}).get("after")
    fixed, insert_at = [], None
    for step in proc["steps"]:
        if step.get("if") and not _truthy(step["if"], scope):
            pass
        elif "foreach" in step:
            items = render(step["foreach"], scope)
            for item in items if isinstance(items, list) else []:
                fixed.append(_intent_from_step(step, Scope(scope, item=item)))
        else:
            fixed.append(_intent_from_step(step, scope))
        if step.get("id") == after:
            insert_at = len(fixed)
    extra = variable_steps(ctx, goal, proc, emp, scope, gr.asks, fixed)
    insert_at = len(fixed) if insert_at is None else insert_at
    goal.intents = fixed[:insert_at] + extra + fixed[insert_at:]
    # written policy filters everything, whatever its origin
    for it in goal.intents:
        for v in policy.violations(it, emp):
            it.status = "blocked"
            it.result["blocked"] = v
            goal.flags.append({"kind": "policy_conflict", "detail": f"'{it.description}' is planned but {v['reason']} Skipped."})
        if it.origin == "precedent" and it.status != "blocked":
            key = f"{goal.task_type}:{(emp or {}).get('title')}:{it.delta.signature()}:{it.delta.key.get('channel')}"
            it.needs_confirmation = store.get_confirmation(key) != "yes"
            it.result["confirm_key"] = key
    # reversible first, irreversible last; scheduled steps after immediate ones
    goal.intents.sort(key=lambda i: (i.not_before is not None, i.irreversible))
