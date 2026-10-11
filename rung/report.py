"""Evidence report: what was understood (with sources), what was done (and how), how
each outcome was verified, what was flagged, and what remains."""
from __future__ import annotations

import time

from rung import store

ICON = {"done": "DONE", "skipped": "SKIPPED", "blocked": "BLOCKED", "failed": "FAILED", "scheduled": "SCHEDULED",
        "compensated": "UNDONE", "pending": "PENDING", "running": "INTERRUPTED"}


def write(rid, goal, ctx) -> str:
    run = store.get_run(rid)
    L = [f"# Run {rid}: {goal.task_type}", "", f"**Request** ({goal.requester}): {goal.request}", "",
         f"**State:** {run['state']}" + (f" (next step due {run['wake_at']})" if run.get("wake_at") and run["state"] == "scheduled" else ""), ""]
    if goal.questions:
        L += ["## Waiting for input", *[f"- {q['q']}" for q in goal.questions], ""]
    if goal.gaps:
        L += ["## What I worked out (and from where)", "| detail | value | how | source |", "|---|---|---|---|"]
        for g in goal.gaps:
            v = ", ".join(map(str, g.value)) if isinstance(g.value, list) else g.value
            L.append(f"| {g.field} | {v} | {g.how} | {g.source} |")
        L.append("")
    if goal.flags:
        L += ["## Flags", *[f"- **{f['kind']}**: {f['detail']}" for f in goal.flags], ""]
    if goal.intents:
        L += ["## Steps", "| # | step | status | how it ran | verified | notes |", "|---|---|---|---|---|---|"]
        for n, i in enumerate(goal.intents, 1):
            r = i.result
            how = r.get("rung", "")
            att = r.get("attempts") or []
            if len(att) > 1:
                how += " (after: " + ", ".join(f"{a['rung']} failed" for a in att if not a["ok"]) + ")"
            notes = []
            if i.not_before and i.status == "scheduled":
                notes.append(f"due {i.not_before}")
            if r.get("blocked"):
                notes.append(f"policy {r['blocked']['rule']}")
            if r.get("why"):
                notes.append(r["why"])
            if r.get("unintended"):
                notes.append("UNINTENDED CHANGE: " + "; ".join(r["unintended"]))
            if r.get("compensation"):
                notes.append(f"undo: {r['compensation']['delta']} ({'ok' if r['compensation']['ok'] else 'FAILED'})")
            if i.origin != "sop":
                notes.append(f"from {i.origin}")
            if r.get("ms"):
                notes.append(f"{r['ms']} ms")
            L.append(f"| {n} | {i.description} | {ICON.get(i.status, i.status)} | {how} | {r.get('label') or ''} | {'; '.join(notes)} |")
        L.append("")
        cites = [(i.description, c) for i in goal.intents for c in i.citations if c.quote]
        if cites:
            L += ["## Evidence for precedent-based steps", *[f"- {d}: {c.source} - {c.quote}" for d, c in cites], ""]
    st = ctx.stats
    L += ["## Cost", f"- LLM calls: {st['llm_calls']}  |  explored (UI) steps: {st['explore_steps']}  |  compiled steps: {st['compiled_steps']}"
          f"  |  wall time: {time.time() - st['t0']:.1f}s", "", f"Full event log: `python -m rung events {rid}`"]
    store.event(rid, "run.stats", {k: (v if k != "t0" else round(time.time() - v, 2)) for k, v in st.items()})
    md = "\n".join(L)
    (store.artifacts_dir(rid) / "report.md").write_text(md)
    return md
