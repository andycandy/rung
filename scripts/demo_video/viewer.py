"""Live demo viewer: the browser Rung is operating (left) and its thinking (right, fixed).

Reads only what Rung already writes: the latest browser frame (RUNG_DEMO_FRAMES) and the
append-only event log in var/agent.db, which it turns into readable lines. It never talks to
the agent, so the run is exactly the same with or without it.

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

RUNG_LABEL = {"noop": "already true", "learned": "learned API tool", "documented": "documented API / CLI",
              "probation": "tool on probation", "ui": "UI exploration (model)"}


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


def describe(kind: str, d: dict) -> dict | None:
    """One event -> one line for the panel (or None to skip it)."""
    if kind == "demo.chapter":
        return {"cls": "chapter", "text": d.get("title", ""), "sub": d.get("subtitle", "")}
    if kind == "run.created":
        return {"cls": "request", "icon": "📨", "text": f"“{_short(d.get('request'), 200)}”", "sub": d.get("requester", "")}
    if kind == "understand.model":
        people = ", ".join(p.get("as_written", "") for p in d.get("people", [])) or "nobody named"
        dates = ", ".join(f"{x.get('as_written')} → {x.get('iso')}" for x in d.get("dates", [])) or "no date"
        task = d.get("task") or "an unknown task"
        return {"cls": "think", "icon": "🧠", "text": f"Reads it as: <b>{task}</b> · {people} · {dates}"
                                                    + (f" · asks: {', '.join(d['explicit_asks'])}" if d.get("explicit_asks") else "")}
    if kind == "evidence.gathered":
        items = d.get("items", [])
        cnt = lambda p: sum(1 for i in items if i["id"].startswith(p))  # noqa: E731
        bad = sum(1 for i in items if not i["trusted"])
        return {"cls": "code", "icon": "🔎", "text": f"Code gathers evidence: {cnt('peer:')} peer setups · {cnt('mail:')} mails"
                                                   f"{f' (<b>{bad} untrusted</b>)' if bad else ''} · {cnt('chat:')} chat messages · the SOP"}
    if kind == "model.thinking":
        return {"cls": "thinking", "icon": "💭", "text": "Model reasoning: " + _short(d.get("text"), 230)}
    if kind == "proposals.model":
        chans = [s["delta"].get("key", {}).get("channel") or _short(json.dumps(s["delta"].get("expect")), 40) for s in d.get("steps", [])]
        return {"cls": "think", "icon": "💡", "text": "Model proposes: " + ", ".join(f"#{c}" if c and not str(c).startswith("{") else str(c) for c in chans)}
    if kind == "proposals.validated":
        planned = [p.split("channel=")[1].split(",")[0] if "channel=" in p else p for p in d.get("planned", [])]
        rejected = []
        for f in d.get("flags", []):
            if f.get("kind") == "policy_conflict":
                rejected.append("blocked by written policy")
            elif f.get("kind") == "suspicious_instruction":
                rejected.append("untrusted mail ignored")
            elif f.get("kind") in ("precedent_rejected", "refused_request", "proposal_rejected"):
                rejected.append(_short(f.get("detail"), 80))
        return {"cls": "code", "icon": "🛡️", "text": "Code validates → planned: " + ", ".join(f"#{c}" for c in planned)
                                                    + (f" · <b>rejected</b>: {'; '.join(sorted(set(rejected)))}" if rejected else "")}
    if kind == "howto.sources":
        items = d.get("items", [])
        return {"cls": "code", "icon": "📚", "text": f"Unknown task. Code collects {len(items)} company sources (Drive handbook & templates, ops@ mail) to look for a how-to"}
    if kind == "howto.pick":
        return {"cls": "think", "icon": "📌", "text": f"Model picks {', '.join(d.get('source_ids', [])) or 'nothing'} "
                                                    f"({d.get('confidence')}): {_short(d.get('why'), 160)}"}
    if kind == "procedure.draft":
        errs = d.get("errors") or []
        return {"cls": "code" if not errs else "warn", "icon": "✍️",
                "text": "Model drafts a procedure → code validator: " + ("<b>valid</b>, saved to drafts/ (not usable until approved)" if not errs
                                                                           else f"rejected: {_short('; '.join(errs), 140)} → redraft")}
    if kind == "procedure.approved":
        return {"cls": "learn", "icon": "🎓", "text": f"Skill approved by the requester → <b>{d.get('task_type')}</b> is now a known procedure"}
    if kind == "human.ask":
        q, a = _short(d.get("question"), 220), d.get("answer")
        return {"cls": "ask", "icon": "❓", "text": f"Asks: {q}" + (f" → <b>{a}</b>" if a else "")}
    if kind == "step.start":
        return {"cls": "step", "icon": "▶", "text": _short(d.get("description"), 120), "ladder": "start"}
    if kind == "explore.step":
        acts = ", ".join(f"{a.get('action')} {a.get('value') or ''}".strip() for a in d.get("actions", [])) or d.get("action")
        return {"cls": "ui", "icon": "🖱️", "text": f"{_short(acts, 70)} — <i>{_short(d.get('reason'), 150)}</i>", "ladder": "ui"}
    if kind == "step.end":
        r = _rung(d.get("rung", ""))
        att = d.get("attempts") or []
        ms = next((a.get("ms") for a in reversed(att) if a.get("ok")), None)
        failed = [a["rung"] for a in att if not a.get("ok")]
        txt = (f"✓ verified · {RUNG_LABEL.get(r, d.get('rung'))}" + (f" · {ms} ms" if ms is not None else "")
               + (f" (after: {', '.join(failed)} failed)" if failed else "")) if d.get("ok") else f"✗ not done ({d.get('rung')})"
        return {"cls": "ok" if d.get("ok") else "warn", "text": txt, "ladder": r if d.get("ok") else "fail", "ms": ms, "rung": d.get("rung")}
    if kind == "promoter.synthesized":
        return {"cls": "learn", "icon": "🧩", "text": "Recorded UI traffic → code synthesizes a typed API tool"}
    if kind == "promoter.promoted":
        return {"cls": "learn", "icon": "🎓", "text": "Sandbox test passed (do → verify → undo → verify) → <b>API tool promoted</b>", "ladder": "promoted"}
    if kind == "promoter.rejected":
        return {"cls": "warn", "icon": "✗", "text": f"Tool failed its sandbox test: {_short(d.get('reason'), 100)}"}
    if kind == "promoter.skip":
        return {"cls": "code", "icon": "·", "text": "No JSON API behind this UI (form posts) → it stays on the UI rung"}
    if kind == "rung.demoted":
        return {"cls": "warn", "icon": "⚠️", "text": f"Learned tool broke: {_short(d.get('reason'), 110)} → <b>demoted</b>, falling back down the ladder",
                "ladder": "demoted"}
    if kind == "saga.compensate":
        return {"cls": "warn", "icon": "↩️", "text": f"Required step failed → undoing: {', '.join(d.get('undo', []))}"}
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
        except sqlite3.Error:
            rows = []
        con.close()
        for rowid, run_id, kind, data in rows:
            try:
                line = describe(kind, json.loads(data))
            except Exception:  # noqa: BLE001
                line = None
            out.append({"id": rowid, "run": run_id, **(line or {"skip": True})})
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
