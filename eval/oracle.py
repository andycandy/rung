"""Independent ground truth. Reads the apps' databases and the Drive directly --
privileged access the agent never has. The agent's own probes are not used here."""
from __future__ import annotations

import json
import subprocess

from sandbox.common import DRIVE, db


def chat_user(username):
    with db("chat") as c:
        u = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if not u:
            return None
        chans = sorted(r["name"] for r in c.execute(
            "SELECT ch.name FROM channel_members m JOIN channels ch ON ch.id=m.channel_id WHERE m.user_id=?", (u["id"],)))
        return {**dict(u), "channels": chans}


def chat_users_like(prefix):
    with db("chat") as c:
        return [dict(r) for r in c.execute("SELECT * FROM users WHERE username LIKE ?", (prefix + "%",))]


def hr_employee(full_name):
    with db("hr") as c:
        r = c.execute("SELECT * FROM employees WHERE full_name=?", (full_name,)).fetchone()
        return dict(r) if r else None


def mail_to(addr, subject_contains=""):
    with db("mail") as c:
        return [dict(r) for r in c.execute("SELECT * FROM messages WHERE recipients LIKE ? AND subject LIKE ?",
                                           (f"%{addr}%", f"%{subject_contains}%"))]


def drive_pdf_text(path):
    p = DRIVE / path
    if not p.exists():
        return None
    return subprocess.run(["pdftotext", str(p), "-"], capture_output=True, text=True).stdout


def chat_audit(action=None):
    with db("chat") as c:
        q = "SELECT * FROM audit" + (" WHERE action=?" if action else "")
        return [dict(r) | {"detail": json.loads(r["detail"])} for r in c.execute(q, (action,) if action else ())]


def sso_user(username):
    with db("sso") as c:
        r = c.execute("SELECT * FROM auth_user WHERE username=?", (username,)).fetchone()
        return dict(r) if r else None


def latest_mail_to(addr, subject_contains=""):
    ms = mail_to(addr, subject_contains)
    return sorted(ms, key=lambda m: m["id"])[-1] if ms else None
