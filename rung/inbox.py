"""Email front door: people email ops@acme.test; the agent replies in-thread.

Mail is not authenticated (SMTP From can be spoofed), so:
  * only internal senders are accepted, and requester authority is checked against PeopleHub;
  * anything destructive or irreversible needs a confirmation: the agent mails a one-time code
    to the requester's address *as recorded in PeopleHub*, and only a reply quoting it proceeds.
"""
from __future__ import annotations

import re
import secrets

import httpx

from rung import policy, runtime, store
from rung.apps import registry
from rung.understand import extract
from rung.ctx import Ctx
from sandbox.common import url


def _thread_answers(body: str) -> dict:
    out = {}
    for line in body.splitlines():
        m = re.match(r"\s*([a-z_]+)\s*[:=]\s*(.+)", line)
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def process(model=None) -> list[str]:
    mail, hr = registry.get("mail"), registry.get("hr")
    handled = []
    threads = {}
    codes = {}
    for r in store.list_runs():
        for e in store.events(r["id"]):
            if e["kind"] == "inbox.replied":
                threads[e["data"]["message_id"]] = r["id"]
            if e["kind"] == "inbox.confirm_code":
                codes[e["data"]["code_hash"]] = r["id"]
    for m in mail.api("/api/v1/messages", to="ops@", unread=1):
        httpx.post(url("mail") + f"/api/v1/messages/{m['id']}/read")
        sender = m["sender"].strip("<>").lower()
        if not policy.trusted_sender(sender):
            continue
        code = re.search(r"\bCONFIRM\s+([A-Z0-9]{8})\b", m["body"])
        if code:
            import hashlib
            rid = codes.get(hashlib.sha256(code.group(1).encode()).hexdigest())
            if rid and store.get_run(rid)["state"] == "awaiting_confirmation":
                runtime.confirm_and_run(rid, model=model)
                mail.send(sender, f"Re: {m['subject']}", store.get_run(rid)["report"], in_reply_to=m.get("message_id"))
                handled.append(rid)
            continue
        answers, request = {}, m["body"].strip()
        if m.get("in_reply_to") in threads:
            prev = store.get_run(threads[m["in_reply_to"]])
            answers, request = _thread_answers(m["body"]), prev["request"]
        else:
            ex = extract(Ctx("inbox"), f"{m['subject']}\n{request}")
            if not (ex.get("task_type") or ex.get("ambiguous")):
                continue  # not a request (FYIs, notes): don't reply to everything
        approver = (lambda kind, q, o: "yes" if kind == "confirm" else None)
        rid = runtime.start(request, sender, approver, model, answers, channel="email")
        run = store.get_run(rid)
        body = run["report"]
        if run["state"] == "awaiting_confirmation":
            c = secrets.token_hex(4).upper()
            import hashlib
            store.event(rid, "inbox.confirm_code", {"code_hash": hashlib.sha256(c.encode()).hexdigest()})
            me = next((e for e in hr.search(None, q=sender) if (e.get("work_email") or "").lower() == sender), None)
            to = (me or {}).get("work_email", sender)
            mail.send(to, f"Confirm: {m['subject']}", f"Someone asked me (from your address) to do something that can't be undone.\n"
                                                      f"If this was you, reply with:  CONFIRM {c}\n\n{body}")
        out = mail.send(sender, f"Re: {m['subject']}", body, in_reply_to=m.get("message_id"))
        store.event(rid, "inbox.replied", {"message_id": out["message_id"], "to": sender})
        handled.append(rid)
    return handled
