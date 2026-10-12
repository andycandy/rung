"""Durable state: runs, append-only events, learned rungs, cached procedures, corrections.

Everything the agent does is an event. Runs can be inspected, resumed after a
crash, and every LLM/tool call can be reconstructed from the log.
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from pathlib import Path

from sandbox.common import VAR, now

DB = VAR / "agent.db"
SECRET_KEYS = re.compile(r"(cookie|authorization|csrf|password|token|secret)", re.I)


def scrub(obj):
    """Strip secrets before anything is persisted (recordings, events)."""
    if isinstance(obj, dict):
        return {k: ("<redacted>" if SECRET_KEYS.search(str(k)) else scrub(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(x) for x in obj]
    return obj


def con() -> sqlite3.Connection:
    VAR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    c.executescript(
        """
        CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, request TEXT, requester TEXT, state TEXT,
           goal TEXT, created TEXT, updated TEXT, wake_at TEXT, report TEXT, owner INTEGER);
        CREATE TABLE IF NOT EXISTS events(run_id TEXT, seq INTEGER, ts TEXT, kind TEXT, data TEXT);
        CREATE TABLE IF NOT EXISTS rungs(id TEXT PRIMARY KEY, app TEXT, signature TEXT, kind TEXT, path TEXT,
           status TEXT, version INTEGER, successes INTEGER DEFAULT 0, failures INTEGER DEFAULT 0,
           consecutive_failures INTEGER DEFAULT 0, avg_ms REAL, created TEXT, updated TEXT, note TEXT);
        CREATE TABLE IF NOT EXISTS procedures(task_type TEXT PRIMARY KEY, body TEXT, sources TEXT, confirmed INTEGER, updated TEXT);
        CREATE TABLE IF NOT EXISTS corrections(id INTEGER PRIMARY KEY, task_type TEXT, scope TEXT, body TEXT, ts TEXT);
        CREATE TABLE IF NOT EXISTS confirmations(key TEXT PRIMARY KEY, answer TEXT, ts TEXT);
        CREATE TABLE IF NOT EXISTS nav_paths(key TEXT PRIMARY KEY, paths TEXT, ts TEXT);
        """
    )
    return c


# ------------------------------------------------------------------ runs & events
def new_run(request: str, requester: str) -> str:
    rid = "r" + uuid.uuid4().hex[:7]
    with con() as c:
        c.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (rid, request, requester, "planned", None, now().isoformat(), now().isoformat(), None, None, None))
    event(rid, "run.created", {"request": request, "requester": requester})
    return rid


def set_run(rid: str, **kw):
    kw["updated"] = now().isoformat()
    with con() as c:
        c.execute(f"UPDATE runs SET {', '.join(f'{k}=?' for k in kw)} WHERE id=?", [*kw.values(), rid])


def get_run(rid: str) -> dict | None:
    with con() as c:
        r = c.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
    return dict(r) if r else None


def list_runs(state: str | None = None) -> list[dict]:
    with con() as c:
        q = "SELECT * FROM runs" + (" WHERE state=?" if state else "") + " ORDER BY created"
        return [dict(r) for r in c.execute(q, (state,) if state else ())]


def event(rid: str, kind: str, data: dict | None = None):
    with con() as c:
        seq = c.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events WHERE run_id=?", (rid,)).fetchone()[0]
        c.execute("INSERT INTO events VALUES(?,?,?,?,?)", (rid, seq, now().isoformat(), kind, json.dumps(scrub(data or {}), default=str)))


def events(rid: str) -> list[dict]:
    with con() as c:
        return [{**dict(r), "data": json.loads(r["data"])} for r in c.execute("SELECT * FROM events WHERE run_id=? ORDER BY seq", (rid,))]


# ------------------------------------------------------------------ learned rungs
def rungs_for(app: str, signature: str, statuses=("promoted",)) -> list[dict]:
    with con() as c:
        q = f"SELECT * FROM rungs WHERE app=? AND signature=? AND status IN ({','.join('?' * len(statuses))}) ORDER BY version DESC"
        return [dict(r) for r in c.execute(q, (app, signature, *statuses))]


def all_rungs() -> list[dict]:
    with con() as c:
        return [dict(r) for r in c.execute("SELECT * FROM rungs ORDER BY app, signature, version")]


def add_rung(app, signature, kind, path, status, note="") -> str:
    with con() as c:
        v = c.execute("SELECT COALESCE(MAX(version),0)+1 FROM rungs WHERE app=? AND signature=?", (app, signature)).fetchone()[0]
        rid = f"{app}-{kind}-{uuid.uuid4().hex[:6]}"
        c.execute("INSERT INTO rungs(id,app,signature,kind,path,status,version,created,updated,note) VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (rid, app, signature, kind, str(path), status, v, now().isoformat(), now().isoformat(), note))
    return rid


def update_rung(rid, **kw):
    kw["updated"] = now().isoformat()
    with con() as c:
        c.execute(f"UPDATE rungs SET {', '.join(f'{k}=?' for k in kw)} WHERE id=?", [*kw.values(), rid])


def record_rung_use(rid, ok: bool, ms: int):
    with con() as c:
        r = c.execute("SELECT * FROM rungs WHERE id=?", (rid,)).fetchone()
        if not r:
            return None
        n = r["successes"] + r["failures"]
        avg = ((r["avg_ms"] or 0) * n + ms) / (n + 1)
        if ok:
            c.execute("UPDATE rungs SET successes=successes+1, consecutive_failures=0, avg_ms=?, updated=? WHERE id=?", (avg, now().isoformat(), rid))
        else:
            c.execute("UPDATE rungs SET failures=failures+1, consecutive_failures=consecutive_failures+1, updated=? WHERE id=?", (now().isoformat(), rid))
        return dict(c.execute("SELECT * FROM rungs WHERE id=?", (rid,)).fetchone())


# ------------------------------------------------------------------ procedures / corrections / confirmations
def get_procedure(task_type):
    with con() as c:
        r = c.execute("SELECT * FROM procedures WHERE task_type=?", (task_type,)).fetchone()
    return {**dict(r), "body": json.loads(r["body"]), "sources": json.loads(r["sources"])} if r else None


def put_procedure(task_type, body, sources, confirmed=False):
    with con() as c:
        c.execute("INSERT OR REPLACE INTO procedures VALUES(?,?,?,?,?)",
                  (task_type, json.dumps(body), json.dumps(sources), int(confirmed), now().isoformat()))


def invalidate_procedure(task_type):
    with con() as c:
        c.execute("DELETE FROM procedures WHERE task_type=?", (task_type,))


def add_correction(task_type, scope, body):
    with con() as c:
        c.execute("INSERT INTO corrections(task_type,scope,body,ts) VALUES(?,?,?,?)", (task_type, scope, json.dumps(body), now().isoformat()))
    invalidate_procedure(task_type)


def corrections(task_type):
    with con() as c:
        return [{**dict(r), "body": json.loads(r["body"])} for r in c.execute("SELECT * FROM corrections WHERE task_type=?", (task_type,))]


def get_confirmation(key):
    with con() as c:
        r = c.execute("SELECT answer FROM confirmations WHERE key=?", (key,)).fetchone()
    return r["answer"] if r else None


def put_confirmation(key, answer):
    with con() as c:
        c.execute("INSERT OR REPLACE INTO confirmations VALUES(?,?,?)", (key, answer, now().isoformat()))


def get_nav_paths(key) -> list[str]:
    with con() as c:
        r = c.execute("SELECT paths FROM nav_paths WHERE key=?", (key,)).fetchone()
    return json.loads(r["paths"]) if r else []


def put_nav_paths(key, paths: list[str]):
    with con() as c:
        c.execute("INSERT OR REPLACE INTO nav_paths VALUES(?,?,?)", (key, json.dumps(paths), now().isoformat()))


def artifacts_dir(rid: str) -> Path:
    p = VAR / "runs" / rid
    p.mkdir(parents=True, exist_ok=True)
    return p
