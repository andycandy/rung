"""Run model: understand -> ask (batched) -> confirm -> execute as a saga -> verify -> report.

States: planned -> running -> (waiting_input | scheduled) -> running -> done | partial | compensated | escalated
  * checkpoint after every verified intent; a crashed run resumes where it stopped
    (precondition probes make re-execution idempotent)
  * a failed *required* intent triggers compensation of completed reversible
    intents in reverse order; if anything irreversible already happened, escalate
  * intents with not_before are parked; `tick` wakes them on time
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict
from datetime import datetime

from rung import ladder, policy, report, store
from rung.apps import registry
from rung.ctx import Ctx
from rung.types import Gap, GoalSpec, Intent
from rung.understand import understand
from sandbox.common import now

MAX_ASK_ROUNDS = 3


class Crash(Exception):
    """Injected crash (eval): simulates the process dying mid-run."""


def save_goal(rid, goal: GoalSpec):
    store.set_run(rid, goal=goal.to_json())


def load_goal(rid) -> GoalSpec:
    d = json.loads(store.get_run(rid)["goal"])
    g = GoalSpec(**{k: v for k, v in d.items() if k not in ("intents", "gaps")})
    g.gaps = [Gap(**x) for x in d["gaps"]]
    g.intents = [Intent.from_dict(x) for x in d["intents"]]
    return g


def _ask(ctx, kind, question, options=None):
    ans = ctx.approver(kind, question, options or []) if ctx.approver else None
    ctx.log("human.ask", {"kind": kind, "question": question, "answer": ans})
    return ans


VALID = {
    "subject": lambda v: str(v).strip().lstrip("#[").rstrip("]").isdigit(),
    "start_date": lambda v: bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(v).strip())),
    "date": lambda v: bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(v).strip())),
    "rehire": lambda v: str(v).strip().lower() in ("reactivate", "new"),
    "negation_ok": lambda v: str(v).strip().lower() in ("yes", "no"),
    "approve_procedure": lambda v: str(v).strip().lower() in ("yes", "no"),
    "sop": lambda v: bool(re.fullmatch(r"[\w./-]+\.(md|txt)", str(v).strip())),
    "drafted": lambda v: bool(re.fullmatch(r"[a-z][a-z0-9_]*", str(v).strip())),
}


def validate_answers(answers: dict) -> tuple[dict, list[str]]:
    """Answers are checked against their question's type; bad ones are dropped and re-asked."""
    good, bad = {}, []
    for k, v in answers.items():
        chk = VALID.get(k)
        if chk is None or chk(v):
            good[k] = str(v).strip() if isinstance(v, str) else v
        else:
            bad.append(f"{k}={v!r}")
    if good.get("negation_ok") == "no":
        good.pop("negation_ok")
    return good, bad


def start(request: str, requester: str, approver=None, model=None, answers: dict | None = None,
          channel: str = "cli", parent: str | None = None) -> str:
    rid = store.new_run(request, requester)
    ctx = Ctx(rid, model=model, approver=approver)
    if parent:
        ctx.log("run.child_of", {"parent": parent})
    try:
        answers, bad = validate_answers(dict(answers or {}))
        try:
            for _ in range(MAX_ASK_ROUNDS):
                goal = understand(ctx, request, requester, answers)
                if bad:
                    goal.flags.append({"kind": "invalid_answer", "detail": f"Ignored answers that didn't fit the question: {', '.join(bad)}"})
                if not goal.questions:
                    break
                got = False
                for q in goal.questions:   # ask budget: only what context couldn't answer
                    if q.get("drafted"):
                        answers["drafted"] = q["drafted"]
                    a = _ask(ctx, "question", q["q"])
                    if a:
                        ok, b2 = validate_answers({q["key"]: a})
                        if ok:
                            answers.update(ok)
                            got = True
                        bad += b2
                if not got:
                    break
        except Exception as ex:  # noqa: BLE001 -- a system of record is down etc.: fail closed, explain
            goal = GoalSpec(request, requester, "unknown")
            goal.flags.append({"kind": "escalation", "detail": f"Couldn't understand the request because a system failed: {type(ex).__name__}: {ex}"})
            save_goal(rid, goal)
            store.set_run(rid, state="escalated")
            store.set_run(rid, report=report.write(rid, goal, ctx))
            return rid
        save_goal(rid, goal)
        ctx.log("understand.goal", {"task_type": goal.task_type, "gaps": [asdict(g) for g in goal.gaps],
                                    "flags": goal.flags, "questions": goal.questions, "split": goal.split,
                                    "intents": [{"d": i.description, "delta": i.delta.describe(), "risk": i.risk, "origin": i.origin,
                                                 "status": i.status, "not_before": i.not_before} for i in goal.intents]})
        if goal.rejected:
            store.set_run(rid, state="rejected")
            store.set_run(rid, report=report.write(rid, goal, ctx))
            return rid
        if goal.split:
            kids = [start(request, requester, approver, model, {**answers, "subject": str(pid)}, channel, parent=rid) for pid in goal.split]
            goal.flags.append({"kind": "split", "detail": f"The request names {len(kids)} people; handled as separate runs: {', '.join(kids)}"})
            save_goal(rid, goal)
            store.set_run(rid, state="split")
            store.set_run(rid, report=report.write(rid, goal, ctx))
            return rid
        if goal.questions:
            store.set_run(rid, state="waiting_input")
            store.set_run(rid, report=report.write(rid, goal, ctx))
            return rid
        confirm_precedent(ctx, goal)
        if channel == "email" and _destructive(goal) and not answers.get("_confirmed"):
            # e-mail can be spoofed: anything destructive needs a confirmation from the requester's HR-registered address
            store.set_run(rid, state="awaiting_confirmation")
            save_goal(rid, goal)
            store.set_run(rid, report=report.write(rid, goal, ctx))
            return rid
        save_goal(rid, goal)
        execute(ctx, goal)
    finally:
        ctx.close()
    return rid


def _destructive(goal: GoalSpec) -> bool:
    return any(i.irreversible or i.delta.expect.get("exists") is False or i.delta.expect.get("active") is False
               for i in goal.intents if i.status == "pending")


def confirm_and_run(rid: str, approver=None, model=None):
    goal = load_goal(rid)
    ctx = Ctx(rid, model=model, approver=approver)
    ctx.log("run.confirmed", {})
    try:
        execute(ctx, goal)
    finally:
        ctx.close()


def confirm_precedent(ctx, goal: GoalSpec):
    """First use of a precedent-only step: one batched confirmation, remembered afterwards."""
    pending = [i for i in goal.intents if i.needs_confirmation and i.status == "pending"]
    if not pending:
        return
    lines = "\n".join(f"  - {i.description}  [{'; '.join(c.quote for c in i.citations if c.source.startswith('precedent'))[:140]}]" for i in pending)
    a = _ask(ctx, "confirm", f"These steps come from how things were done before, not from a written procedure:\n{lines}\nGo ahead? (yes / no / comma-separated items to skip)")
    a = (a or "no").strip().lower()
    for i in pending:
        skip = a == "no" or (a not in ("yes", "y") and i.delta.key.get("channel", "~") in [x.strip().lstrip("#") for x in a.split(",")])
        if skip:
            i.status = "skipped"
            i.result["why"] = "declined by requester"
        else:
            i.needs_confirmation = False
            store.put_confirmation(i.result["confirm_key"], "yes")


def _late_bind(obj, goal):
    """Values that depend on what actually happened (e.g. which channels were really joined)."""
    from rung.derive import DONE_CHANNELS
    if isinstance(obj, dict):
        return {k: _late_bind(v, goal) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_late_bind(v, goal) for v in obj]
    if isinstance(obj, str) and DONE_CHANNELS in obj:
        done = [i.delta.key["channel"] for i in goal.intents if i.delta.entity == "channel_member"
                and i.delta.expect.get("exists") is True and i.status == "done"]
        return obj.replace(DONE_CHANNELS, ", ".join(f"#{c}" for c in done) or "your team's channels (to be confirmed)")
    return obj


def _unintended(app, delta, before, after) -> list[str]:
    spec = app.projection.get(delta.entity, {})
    if not before or not after:
        return []
    out = []
    for f in spec.get("compare", []):
        if f in delta.expect or f == "exists":
            continue
        if before.get(f) != after.get(f):
            out.append(f"{f}: {before.get(f)!r} -> {after.get(f)!r}")
    return out


def execute(ctx, goal: GoalSpec):
    rid = ctx.rid
    store.set_run(rid, state="running")
    crash_after = int(os.environ.get("RUNG_CRASH_AFTER", "0"))
    crash_mid = int(os.environ.get("RUNG_CRASH_MID", "0"))
    store.set_run(rid, owner=os.getpid())
    done_now = 0
    failed_required = None
    for it in goal.intents:
        if it.status in ("done", "skipped", "blocked", "compensated", "failed"):
            continue
        if it.not_before and now() < datetime.fromisoformat(it.not_before):
            it.status = "scheduled"
            continue
        if policy.gate(it) == "approve":
            a = _ask(ctx, "approve", f"Approval needed ({it.risk}): {it.description} -> {it.delta.describe()}", ["approve", "deny"])
            if (a or "").lower() not in ("approve", "yes", "y"):
                it.result["why"] = "approval denied or not given"
                if it.result.get("required"):
                    it.status = "failed"   # a required step that can't run fails the run (and triggers the saga)
                    save_goal(rid, goal)
                    failed_required = it
                    break
                it.status = "skipped"
                save_goal(rid, goal)
                continue
        if crash_after and done_now >= crash_after:
            save_goal(rid, goal)
            store.set_run(rid, state="running")
            ctx.log("run.crash_injected", {"after": done_now})
            raise Crash(f"injected crash after {done_now} intents")
        it.status = "running"
        save_goal(rid, goal)
        ctx.log("step.start", {"intent": it.id, "description": it.description, "delta": it.delta.describe(), "risk": it.risk})
        t0 = time.time()
        it.args = _late_bind(it.args, goal)
        try:
            res = ladder.ensure(ctx, it.delta, it.args)
        except Exception as ex:  # noqa: BLE001
            from rung.types import ExecResult
            res = ExecResult(ok=False, rung="error", detail=f"{type(ex).__name__}: {ex}", data={})
        app = registry.get(it.delta.app)
        it.result.update({"rung": res.rung, "label": res.data.get("label"), "ms": int((time.time() - t0) * 1000),
                          "attempts": res.data.get("attempts", []), "detail": res.detail,
                          "before": res.data.get("before"), "unintended": _unintended(app, it.delta, res.data.get("before"), res.data.get("after"))})
        if res.data.get("screenshot"):
            it.result["screenshot"] = res.data["screenshot"]
        if crash_mid and done_now + 1 >= crash_mid:
            ctx.log("run.crash_injected", {"after_write_before_checkpoint": it.description})
            raise Crash("injected crash after a write, before its checkpoint")
        it.status = "done" if res.ok else "failed"
        if res.ok and it.result["unintended"]:
            it.status = "failed"   # the expected change happened, but something else changed too
            it.result["detail"] = "unintended change: " + "; ".join(it.result["unintended"])
        ctx.log("step.end", {"intent": it.id, "ok": res.ok, "rung": res.rung, "label": it.result["label"], "attempts": it.result["attempts"]})
        save_goal(rid, goal)
        done_now += 1
        if it.status == "failed" and it.result.get("required"):
            failed_required = it
            break
    if failed_required:
        compensate(ctx, goal, failed_required)
    else:
        sched = [i for i in goal.intents if i.status == "scheduled"]
        failed = [i for i in goal.intents if i.status == "failed"]
        if failed and not any(f.get("kind") == "partial" for f in goal.flags):
            goal.flags.append({"kind": "partial", "detail": "Not done: " + "; ".join(f"{i.description} ({i.result.get('detail') or 'failed'})" for i in failed)})
        if sched and failed:
            store.set_run(rid, state="scheduled_partial", wake_at=min(i.not_before for i in sched))
        elif sched:
            store.set_run(rid, state="scheduled", wake_at=min(i.not_before for i in sched))
        elif any(i.status == "failed" for i in goal.intents):
            store.set_run(rid, state="partial")
        else:
            store.set_run(rid, state="done")
    save_goal(rid, goal)
    store.set_run(rid, report=report.write(rid, goal, ctx))


def compensate(ctx, goal: GoalSpec, failed: Intent):
    done = [i for i in goal.intents if i.status == "done" and i.result.get("rung") != "noop:already-satisfied"]
    irreversible = [i for i in done if i.irreversible]
    if irreversible:
        goal.flags.append({"kind": "escalation", "detail": f"'{failed.description}' failed after irreversible steps "
                                                           f"({', '.join(i.description for i in irreversible)}); not undoing anything automatically."})
        store.set_run(ctx.rid, state="escalated")
        return
    ctx.log("saga.compensate", {"failed": failed.description, "undo": [i.description for i in reversed(done)]})
    ok_all = True
    for i in reversed(done):
        app = registry.get(i.delta.app)
        inv = app.inverse(i.delta, i.result.get("before"))
        if inv is None:
            ok_all = False
            continue
        r = ladder.ensure(ctx, inv)
        i.status = "compensated" if r.ok else "done"
        i.result["compensation"] = {"delta": inv.describe(), "ok": r.ok, "rung": r.rung}
        ok_all &= r.ok
    for i in goal.intents:
        if i.status in ("pending", "scheduled"):
            i.status = "skipped"
            i.result["why"] = "run compensated"
    goal.flags.append({"kind": "compensated" if ok_all else "escalation",
                       "detail": f"'{failed.description}' failed; completed steps were " + ("undone." if ok_all else "partly undone - needs a human.")})
    store.set_run(ctx.rid, state="compensated" if ok_all else "escalated")


def resume(rid: str, approver=None, model=None):
    goal = load_goal(rid)
    ctx = Ctx(rid, model=model, approver=approver)
    ctx.log("run.resumed", {"state": store.get_run(rid)["state"]})
    try:
        for it in goal.intents:
            if it.status in ("running", "scheduled"):
                it.status = "pending"  # re-check with precondition probes; idempotent
        execute(ctx, goal)
    finally:
        ctx.close()
    return rid


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def tick(approver=None, model=None) -> list[str]:
    woke = []
    due = [r for r in store.list_runs("scheduled") + store.list_runs("scheduled_partial")
           if r["wake_at"] and now() >= datetime.fromisoformat(r["wake_at"])]
    # crashed runs: only if the owning process is gone (lease)
    due += [r for r in store.list_runs("running") if not (r.get("owner") and _alive(int(r["owner"])) and int(r["owner"]) != os.getpid())]
    for r in due:
        try:   # one bad run must never block the others
            resume(r["id"], approver, model)
        except Exception as ex:  # noqa: BLE001
            store.event(r["id"], "run.error", {"error": f"{type(ex).__name__}: {ex}"})
            store.set_run(r["id"], state="escalated")
        woke.append(r["id"])
    return woke
