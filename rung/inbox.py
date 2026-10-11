"""Email front door: people email ops@acme.test; the agent replies in-thread with
questions or the evidence report. Answers come back as replies ("rehire: reactivate")."""
from __future__ import annotations

import re

import httpx

from rung import policy, runtime, store
from rung.apps import registry
from sandbox.common import url


def _thread_answers(body: str) -> dict:
    out = {}
    for line in body.splitlines():
        m = re.match(r"\s*([a-z_]+)\s*[:=]\s*(.+)", line)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def process(model=None) -> list[str]:
    mail = registry.get("mail")
    handled = []
    waiting = {e["data"].get("message_id"): e["run_id"] for r in store.list_runs("waiting_input")
               for e in store.events(r["id"]) if e["kind"] == "inbox.replied"}
    for m in mail.api("/api/v1/messages", to="ops@", unread=1):
        httpx.post(url("mail") + f"/api/v1/messages/{m['id']}/read")
        sender = m["sender"].strip("<>")
        if not policy.trusted_sender(sender):
            continue  # outside senders can't task the operator
        answers, request = {}, m["body"].strip()
        if m.get("in_reply_to") in waiting:
            prev = store.get_run(waiting[m["in_reply_to"]])
            answers = _thread_answers(m["body"])
            request = prev["request"]
        approver = (lambda kind, q, o: "yes" if kind == "confirm" else None)  # by mail: precedent steps ok, approvals wait
        rid = runtime.start(request, sender, approver, model, answers)
        run = store.get_run(rid)
        out = mail.send(sender, f"Re: {m['subject']}", run["report"], in_reply_to=m.get("message_id"))
        store.event(rid, "inbox.replied", {"message_id": out["message_id"], "to": sender})
        handled.append(rid)
    return handled
