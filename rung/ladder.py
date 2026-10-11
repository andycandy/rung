"""The interface ladder: execute a delta through the cheapest *trusted* interface,
falling down a rung whenever one fails, and verify every outcome independently.

Order:
  0. already satisfied?            -> no-op (idempotent re-runs)
  1. promoted learned rungs        -> no LLM, no browser
  2. documented API / existing tool (from discovery)
  3. probation learned rungs       -> used with verified fallback
  4. explore the UI                -> LLM or heuristic; traffic recorded; then promoter
The model never decides success: `app.holds()` reads real state after each attempt.
"""
from __future__ import annotations

import time

from rung import store
from rung.apps import registry
from rung.http import AuthExpired, EndpointGone
from rung.types import Delta, ExecResult

DEMOTE_AFTER = 2  # consecutive verified failures (hard errors demote immediately)


def _run_learned(ctx, app, r, delta, values):
    from rung.promoter import load_tool
    tool = load_tool(r["path"])
    args = {**values, **delta.key}
    try:
        return tool.run(app.http(ctx), args)
    except AuthExpired:
        ctx.log("auth.expired", {"app": app.name, "rung": r["id"]})
        app.relogin(ctx)
        return tool.run(app.http(ctx), args)


def ensure(ctx, delta: Delta, args: dict | None = None, depth: int = 0) -> ExecResult:
    app = registry.get(delta.app)
    args = args or {}
    values = app.values(delta, args) if hasattr(app, "values") else {**delta.key, **args}
    sig = delta.signature()
    t_start = time.time()

    ok, before, label = app.holds(ctx, delta)
    if ok:
        ctx.log("step.noop", {"delta": delta.describe(), "reason": "already satisfied"})
        return ExecResult(ok=True, rung="noop:already-satisfied", data={"label": label, "before": before})

    attempts = []

    def finish(res: ExecResult, rung_id=None):
        good, after, lab = app.holds(ctx, delta)
        res.ok = good
        res.data.update({"label": lab, "before": before, "after": after})
        if rung_id:
            r = store.record_rung_use(rung_id, good, res.ms)
            if r and not good and r["consecutive_failures"] >= DEMOTE_AFTER:
                store.update_rung(rung_id, status="demoted", note="verified failures")
                ctx.log("rung.demoted", {"rung": rung_id, "reason": "postcondition failed repeatedly"})
            if r and good and r["status"] == "candidate" and r["successes"] >= 2:
                store.update_rung(rung_id, status="promoted", note="graduated from probation")
                ctx.log("rung.promoted", {"rung": rung_id, "reason": "probation passed"})
        attempts.append({"rung": res.rung, "ok": good, "ms": res.ms, "detail": res.detail})
        return good

    # 1. promoted learned rungs
    for r in store.rungs_for(app.name, sig):
        t0 = time.time()
        try:
            out = _run_learned(ctx, app, r, delta, values)
            res = ExecResult(ok=True, rung=f"learned:{r['id']}", ms=int((time.time() - t0) * 1000), data={"response": out})
        except EndpointGone as ex:
            store.record_rung_use(r["id"], False, 0)
            store.update_rung(r["id"], status="demoted", note=f"endpoint gone: {ex}")
            ctx.log("rung.demoted", {"rung": r["id"], "reason": str(ex)})
            attempts.append({"rung": f"learned:{r['id']}", "ok": False, "detail": str(ex)})
            continue
        except Exception as ex:  # noqa: BLE001
            used = store.record_rung_use(r["id"], False, 0)
            hard = type(ex).__name__ == "Drift" or "410" in str(ex)
            if hard or (used and used["consecutive_failures"] >= DEMOTE_AFTER):
                store.update_rung(r["id"], status="demoted", note=f"{type(ex).__name__}: {ex}")
                ctx.log("rung.demoted", {"rung": r["id"], "reason": str(ex)})
            attempts.append({"rung": f"learned:{r['id']}", "ok": False, "detail": f"{type(ex).__name__}: {ex}"})
            continue
        if finish(res, r["id"]):
            ctx.stats["compiled_steps"] += 1
            return _done(ctx, res, attempts, t_start)

    # 2. documented API / existing tools
    try:
        cands = app.candidates(ctx, delta, args)
    except Exception as ex:  # noqa: BLE001
        cands = []
        attempts.append({"rung": "discovery", "ok": False, "detail": str(ex)})
    for c in cands:
        t0 = time.time()
        try:
            out = c.run(ctx)
            res = ExecResult(ok=True, rung=c.name, ms=int((time.time() - t0) * 1000), data={"response": out})
        except Exception as ex:  # noqa: BLE001
            attempts.append({"rung": c.name, "ok": False, "detail": f"{type(ex).__name__}: {ex}"})
            continue
        if finish(res):
            ctx.stats["compiled_steps"] += 1
            return _done(ctx, res, attempts, t_start)

    # 3. probation rungs
    for r in store.rungs_for(app.name, sig, statuses=("candidate",)):
        t0 = time.time()
        try:
            out = _run_learned(ctx, app, r, delta, values)
            res = ExecResult(ok=True, rung=f"probation:{r['id']}", ms=int((time.time() - t0) * 1000), data={"response": out})
        except Exception as ex:  # noqa: BLE001
            store.record_rung_use(r["id"], False, 0)
            store.update_rung(r["id"], status="rejected", note=f"failed on probation: {ex}")
            attempts.append({"rung": f"probation:{r['id']}", "ok": False, "detail": str(ex)})
            continue
        if finish(res, r["id"]):
            ctx.stats["compiled_steps"] += 1
            return _done(ctx, res, attempts, t_start)

    # 4. explore
    if app.explorable:
        res = app.explore(ctx, delta, values)
        good = finish(res)
        ctx.log("explore.result", {"delta": delta.describe(), "ok": good, "detail": res.detail,
                                   "actions": res.data.get("actions"), "requests": [f"{r['method']} {r['url']} {r['status']}" for r in res.recording]})
        if good:
            from rung.promoter import learn
            learn(ctx, app, delta, values, res.recording, depth=depth)
            return _done(ctx, res, attempts, t_start)

    res = ExecResult(ok=False, rung="none", detail="all rungs failed", data={"before": before})
    return _done(ctx, res, attempts, t_start)


def _done(ctx, res: ExecResult, attempts, t_start):
    res.data["attempts"] = attempts
    res.data["total_ms"] = int((time.time() - t_start) * 1000)
    return res
