"""Shared helpers for the sandbox company ("Acme Analytics").

The sandbox stands in for a real company's SaaS estate. Each app keeps its own
SQLite database under var/sandbox so the eval oracle can inspect ground truth
directly -- something the agent is never allowed to do.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VAR = Path(os.environ.get("RUNG_VAR", ROOT / "var"))
SANDBOX = VAR / "sandbox"
DRIVE = SANDBOX / "drive"
CLOCK_FILE = SANDBOX / "clock.json"
FLAGS_FILE = SANDBOX / "flags.json"

PORTS = {"hr": 7101, "chat": 7102, "mail": 7103, "smtp": 7125}
DOMAIN = "acme.test"


def url(app: str) -> str:
    return f"http://127.0.0.1:{PORTS[app]}"


def now() -> datetime:
    """Sandbox clock. A fake clock lets the eval jump to an employee's start date."""
    if CLOCK_FILE.exists():
        data = json.loads(CLOCK_FILE.read_text())
        return datetime.fromisoformat(data["now"]) + timedelta(
            seconds=(datetime.now(timezone.utc) - datetime.fromisoformat(data["set_at"])).total_seconds()
        )
    return datetime.now(timezone.utc)


def set_clock(when: datetime) -> None:
    CLOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    CLOCK_FILE.write_text(json.dumps({"now": when.isoformat(), "set_at": datetime.now(timezone.utc).isoformat()}))


def flags() -> dict:
    """Failure-injection switches read by the apps on every request (UI variant, API version, outages)."""
    if FLAGS_FILE.exists():
        try:
            return json.loads(FLAGS_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def set_flags(**kw) -> None:
    cur = flags()
    cur.update(kw)
    FLAGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    FLAGS_FILE.write_text(json.dumps(cur))


def db(name: str) -> sqlite3.Connection:
    SANDBOX.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(SANDBOX / f"{name}.db", timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]
