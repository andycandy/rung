"""Mailroom -- company mail: a real SMTP server (aiosmtpd) plus a Mailpit-style JSON API.

The agent sends mail over SMTP like any mail client, and reads mail through the
search API. It is also the inbox where people send requests to ops@acme.test.
"""
from __future__ import annotations

import email
import email.policy
import json
import threading

from aiosmtpd.controller import Controller
from flask import Flask, abort, jsonify, request

from sandbox.common import PORTS, db, now, rows

app = Flask("mailroom")


def init(con):
    con.executescript(
        """CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, message_id TEXT, in_reply_to TEXT,
           sender TEXT, recipients TEXT, subject TEXT, body TEXT, ts TEXT, read INTEGER DEFAULT 0);"""
    )


def store(sender, rcpts, raw: bytes | str, ts=None):
    msg = email.message_from_bytes(raw if isinstance(raw, bytes) else raw.encode(), policy=email.policy.default)
    part = msg.get_body(preferencelist=("plain", "html"))
    body = part.get_content() if part else ""
    atts = [p.get_filename() for p in msg.iter_attachments() if p.get_filename()]
    if atts:
        body += "\n\n[attachments: " + ", ".join(atts) + "]"
    with db("mail") as con:
        con.execute("INSERT INTO messages(message_id,in_reply_to,sender,recipients,subject,body,ts) VALUES(?,?,?,?,?,?,?)",
                    (msg.get("Message-ID"), msg.get("In-Reply-To"), sender, json.dumps(rcpts), msg.get("Subject", ""),
                     body, ts or now().isoformat()))


class Handler:
    async def handle_DATA(self, server, session, envelope):
        store(envelope.mail_from, envelope.rcpt_tos, envelope.original_content)
        return "250 Message accepted"


def _fmt(r):
    r = dict(r)
    r["recipients"] = json.loads(r["recipients"])
    return r


@app.get("/api/v1/messages")
def list_messages():
    sql, args = "SELECT * FROM messages WHERE 1=1", []
    if request.args.get("to"):
        sql += " AND recipients LIKE ?"
        args.append(f"%{request.args['to']}%")
    if request.args.get("from"):
        sql += " AND sender LIKE ?"
        args.append(f"%{request.args['from']}%")
    if request.args.get("q"):
        for term in request.args["q"].lower().split():
            sql += " AND (lower(subject) LIKE ? OR lower(body) LIKE ?)"
            args += [f"%{term}%"] * 2
    if request.args.get("unread"):
        sql += " AND read=0"
    with db("mail") as con:
        return jsonify([_fmt(r) for r in con.execute(sql + " ORDER BY ts DESC LIMIT 100", args)])


@app.get("/api/v1/messages/<int:mid>")
def get_message(mid):
    with db("mail") as con:
        r = con.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
    if not r:
        abort(404)
    return jsonify(_fmt(r))


@app.post("/api/v1/messages/<int:mid>/read")
def mark_read(mid):
    with db("mail") as con:
        con.execute("UPDATE messages SET read=1 WHERE id=?", (mid,))
    return jsonify({"ok": True})


@app.get("/")
def home():
    with db("mail") as con:
        ms = rows(con.execute("SELECT id, ts, sender, recipients, subject FROM messages ORDER BY ts DESC LIMIT 200"))
    trs = "".join(f"<tr><td>{m['ts'][:16]}</td><td>{m['sender']}</td><td>{m['recipients']}</td>"
                  f"<td><a href='/api/v1/messages/{m['id']}'>{m['subject']}</a></td></tr>" for m in ms)
    return f"<html><head><title>Mailroom</title></head><body><h1>Mailroom</h1><table border=1>{trs}</table></body></html>"


def start_smtp():
    c = Controller(Handler(), hostname="127.0.0.1", port=PORTS["smtp"])
    c.start()
    return c


with db("mail") as _c:
    init(_c)
