"""Email front door: people email ops@acme.test; the agent replies in-thread.

Mail is not authenticated (SMTP From can be spoofed), so:
  * only internal senders are accepted, and requester authority is checked against PeopleHub;
  * anything destructive or irreversible needs a confirmation: the agent mails a one-time code
    to the requester's address *as recorded in PeopleHub*, and only a reply quoting it proceeds.
"""
from __future__ import annotations

import hashlib
import json
import secrets

import httpx

from rung import policy, runtime, store
from rung.syntax import confirm_code, is_snake
from rung.apps import registry
from sandbox.common import url


def _thread_answers(body: str) -> dict:
    """Replies answer questions in the 'key: value' form the questions ask for; values are typed later."""
    out = {}
    for line in body.splitlines():
        key, sep, value = line.partition(":")
        key = key.strip()
        if sep and is_snake(key) and value.strip():
            out[key] = value.strip()
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
        code = confirm_code(m["body"])
        if code:
            rid = codes.get(hashlib.sha256(code.encode()).hexdigest())
            if rid and store.get_run(rid)["state"] == "awaiting_confirmation":
                runtime.confirm_and_run(rid, model=model)
                mail.send(sender, f"Re: {m['subject']}", store.get_run(rid)["report"], in_reply_to=m.get("message_id"))
                handled.append(rid)
            continue
        answers, request = {}, f"{m['subject']}\n{m['body'].strip()}"
        if m.get("in_reply_to") in threads:
            prev = store.get_run(threads[m["in_reply_to"]])
            answers, request = _thread_answers(m["body"]), prev["request"]
            if prev.get("goal"):   # an approval in the thread refers to the draft we proposed in it
                drafted = next((q["drafted"] for q in json.loads(prev["goal"]).get("questions", []) if q.get("drafted")), None)
                if drafted:
                    answers.setdefault("drafted", drafted)
        approver = (lambda kind, q, o: "yes" if kind == "confirm" else None)
        rid = runtime.start(request, sender, approver, model, answers, channel="email")
        run = store.get_run(rid)
        if run["state"] == "ignored":
            continue  # the model read it as not a request (FYIs, notes): don't reply to everything
        body = run["report"]
        if run["state"] == "awaiting_confirmation":
            c = secrets.token_hex(4).upper()
            store.event(rid, "inbox.confirm_code", {"code_hash": hashlib.sha256(c.encode()).hexdigest()})
            me = next((e for e in hr.search(None, q=sender) if (e.get("work_email") or "").lower() == sender), None)
            to = (me or {}).get("work_email", sender)
            mail.send(to, f"Confirm: {m['subject']}", f"Someone asked me (from your address) to do something that can't be undone.\n"
                                                      f"If this was you, reply with:  CONFIRM {c}\n\n{body}")
        out = mail.send(sender, f"Re: {m['subject']}", body, in_reply_to=m.get("message_id"))
        store.event(rid, "inbox.replied", {"message_id": out["message_id"], "to": sender})
        handled.append(rid)
    return handled
