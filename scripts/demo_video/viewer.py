"""Live demo viewer: the browser Rung is operating (left) and its thinking (right, fixed).

Reads only what Rung already writes: the latest browser frame (RUNG_DEMO_FRAMES) and the
append-only event log in var/agent.db, which it turns into typed items for the page. It never
talks to the agent, so the run is exactly the same with or without it.

  python scripts/demo_video/viewer.py      # http://127.0.0.1:7190/
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from sandbox.common import VAR  # noqa: E402

FRAMES = Path(os.environ.get("RUNG_DEMO_FRAMES", VAR / "demo"))
app = Flask(__name__)
PORT = 7190

# which model call a reasoning trace belongs to, by the opening of its system prompt (labels only)
THINKING = [("You read one message", "reading the request"), ("You plan the variable part", "planning steps from evidence"),
            ("A requester asked", "looking for a written how-to"), ("You operate a web app", "operating the UI")]
WHY = {"410": "410 Gone", "404": "404 Not Found", "405": "405 Not Allowed", "409": "409 Conflict", "422": "422"}


def _rung(r: str) -> str | None:
    if not r:
        return None
    if r.startswith("noop"):
        return "noop"
    if r.startswith("learned"):
        return "learned"
    if r.startswith("probation"):
        return "probation"
    if r.startswith("documented") or r.startswith("script"):
        return "documented"
    if r.startswith("explore"):
        return "ui"
    return None


def _short(s, n=170):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _why(detail) -> str:
    d = str(detail or "")
    return next((v for k, v in WHY.items() if k in d), _short(d.split(":")[0], 40))


def describe(kind: str, d: dict) -> dict | None:
    """One event -> one item for the viewer (or None to skip it). Each item has a type `t` the page acts on."""
    if kind == "demo.chapter":
        return {"t": "chapter", "title": d.get("title", ""), "sub": d.get("subtitle", "")}
    if kind == "run.created":
        return {"t": "task", "request": _short(d.get("request"), 200), "requester": d.get("requester", "")}
    if kind == "understand.model":
        people = ", ".join(p.get("as_written", "") for p in d.get("people", [])) or "nobody named"
        dates = ", ".join(f"{x.get('as_written')} → {x.get('iso')}" for x in d.get("dates", [])) or "no date"
        return {"t": "feed", "phase": "Planning", "cls": "think", "icon": "🧠",
                "text": f"<b>Model reads it as</b> {d.get('task') or 'an unknown task'} · {people} · {dates}"}
    if kind == "evidence.gathered":
        items = d.get("items", [])
        cnt = lambda p: sum(1 for i in items if i["id"].startswith(p))  # noqa: E731
        bad = sum(1 for i in items if not i["trusted"])
        return {"t": "feed", "cls": "code", "icon": "🔎",
                "text": f"<b>Code gathers evidence</b>: {cnt('peer:')} peer setups · {cnt('mail:')} mails"
                        + (f" (<span class=r>{bad} untrusted</span>)" if bad else "") + f" · {cnt('chat:')} chat messages · the SOP"}
    if kind == "model.thinking":
        label = next((v for k, v in THINKING if d.get("call", "").startswith(k)), "drafting a procedure")
        return {"t": "thinking", "label": label, "text": _short(d.get("text"), 330)}
    if kind == "proposals.model":
        chans = [s["delta"].get("key", {}).get("channel") for s in d.get("steps", [])]
        return {"t": "feed", "cls": "think", "icon": "💡",
                "text": "<b>Model proposes</b>: " + (", ".join(f"#{c}" for c in chans if c) or "no extra steps")}
    if kind == "proposals.validated":
        planned = [p.split("channel=")[1].split(",")[0] if "channel=" in p else p for p in d.get("planned", [])]
        rejected = []
        for f in d.get("flags", []):
            if f.get("kind") == "policy_conflict":
                rejected.append("blocked by written policy")
            elif f.get("kind") == "suspicious_instruction":
                rejected.append("untrusted mail ignored")
            elif f.get("kind") in ("precedent_rejected", "refused_request", "proposal_rejected"):
                rejected.append(_short(f.get("detail"), 70))
        return {"t": "feed", "cls": "code", "icon": "🛡️",
                "text": "<b>Code validates</b> → keeps " + (", ".join(f"#{c}" for c in planned) or "nothing")
                        + (f" · <span class=r>rejects: {'; '.join(sorted(set(rejected)))}</span>" if rejected else "")}
    if kind == "howto.sources":
        return {"t": "feed", "phase": "Looking for a how-to", "cls": "code", "icon": "📚",
                "text": f"<b>No procedure for this.</b> Code collects {len(d.get('items', []))} company sources (Drive handbook, templates, ops@ mail)"}
    if kind == "howto.pick":
        return {"t": "feed", "cls": "think", "icon": "📌",
                "text": f"<b>Model picks</b> {', '.join(d.get('source_ids', [])) or 'nothing'} ({d.get('confidence')}): {_short(d.get('why'), 140)}"}
    if kind == "procedure.draft":
        errs = d.get("errors") or []
        return {"t": "feed", "phase": "Drafting a skill", "cls": "warn" if errs else "code", "icon": "✍️",
                "text": "<b>Model drafts a procedure</b> → code validator: "
                        + (f"<span class=r>rejected: {_short('; '.join(errs), 120)}</span> → redraft" if errs
                           else "valid, saved as a draft (unusable until approved)")}
    if kind == "procedure.approved":
        return {"t": "feed", "cls": "learn", "icon": "🎓", "text": f"<b>Skill approved</b> → {d.get('task_type')} is now a known procedure"}
    if kind == "human.ask":
        a = d.get("answer")
        return {"t": "feed", "phase": None if a else "Waiting for the requester", "cls": "ask", "icon": "❓",
                "text": f"<b>Asks the requester</b>: {_short(d.get('question'), 190)}" + (f" → <b>{a}</b>" if a else " <i>(sent, waiting)</i>")}
    if kind == "understand.goal":
        return {"t": "plan", "steps": [{"id": i.get("id"), "d": _short(i.get("d"), 58), "status": i.get("status"),
                                        "due": (i.get("not_before") or "")[:16].replace("T", " ")} for i in d.get("intents", [])]}
    if kind == "step.start":
        return {"t": "step_start", "sid": d.get("intent"), "d": _short(d.get("description"), 60)}
    if kind == "explore.step":
        acts = " → ".join(f"{a.get('action')} {a.get('value') or ''}".strip() for a in d.get("actions", [])) or d.get("action")
        return {"t": "explore", "acts": _short(acts, 80), "reason": _short(d.get("reason"), 150)}
    if kind == "step.end":
        att = d.get("attempts") or []
        return {"t": "step_end", "sid": d.get("intent"), "ok": d.get("ok"), "rung": _rung(d.get("rung", "")), "raw": d.get("rung"),
                "ms": next((a.get("ms") for a in reversed(att) if a.get("ok")), None),
                "failed": [{"rung": _rung(a.get("rung", "")), "why": _why(a.get("detail"))} for a in att if not a.get("ok")]}
    if kind == "promoter.promoted":
        return {"t": "promoted", "cls": "learn", "icon": "🧩",
                "text": "<b>New API tool learned</b>: recorded UI traffic → typed tool → sandbox test (do → verify → undo → verify) passed → promoted"}
    if kind == "promoter.rejected":
        return {"t": "feed", "cls": "warn", "icon": "✗", "text": f"Synthesized tool failed its sandbox test: {_short(d.get('reason'), 100)}"}
    if kind == "rung.demoted":
        return {"t": "demoted", "cls": "warn", "icon": "⚠️", "why": _why(d.get("reason")),
                "text": f"<b>Learned tool broke</b> ({_why(d.get('reason'))}) → demoted, falling back down the ladder"}
    if kind == "saga.compensate":
        return {"t": "feed", "cls": "warn", "icon": "↩️", "text": f"<b>Required step failed</b> → undoing: {', '.join(d.get('undo', []))}"}
    if kind == "run.stats":
        return {"t": "task_end", "calls": d.get("llm_calls"), "secs": d.get("t0")}
    return None


@app.get("/events")
def events():
    after = int(request.args.get("after", 0))
    db = VAR / "agent.db"
    out = []
    if db.exists():
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
        try:
            rows = con.execute("SELECT rowid, run_id, kind, data FROM events WHERE rowid > ? ORDER BY rowid LIMIT 300", (after,)).fetchall()
            for rowid, run_id, kind, data in rows:
                try:
                    item = describe(kind, json.loads(data))
                except Exception:  # noqa: BLE001
                    item = None
                if item and item["t"] == "task_end":
                    st = con.execute("SELECT state FROM runs WHERE id = ?", (run_id,)).fetchone()
                    item["state"] = st[0] if st else None
                out.append({"id": rowid, "run": run_id, **(item or {"skip": True})})
        except sqlite3.Error:
            pass
        con.close()
    return jsonify({"events": out, "done": (FRAMES / "done").exists()})


@app.get("/frame.jpg")
def frame():
    p = FRAMES / "frame.jpg"
    if not p.exists():
        return Response(status=204)
    r = send_file(p, mimetype="image/jpeg", max_age=0)
    r.headers["Cache-Control"] = "no-store"
    return r


@app.get("/frame.json")
def frame_meta():
    p = FRAMES / "frame.json"
    return jsonify(json.loads(p.read_text()) if p.exists() else {})


@app.get("/")
def index():
    return Response((Path(__file__).parent / "viewer.html").read_text(encoding="utf-8"), mimetype="text/html")


if __name__ == "__main__":
    app.run("127.0.0.1", PORT, threaded=True)
