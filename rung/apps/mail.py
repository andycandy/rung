"""Mailroom adapter: send over SMTP (an existing tool), read via the search API."""
from __future__ import annotations

import email.utils
import mimetypes
import smtplib
from email.message import EmailMessage
from pathlib import Path

import httpx

from rung.apps.base import App, Candidate
from rung.ctx import vault
from rung.types import Delta
from sandbox.common import PORTS, url


class MailApp(App):
    name = "mail"
    kind = "service"
    projection = {"message": {"key": ["to", "subject"], "compare": ["exists"]}}

    def api(self, path, **params):
        r = httpx.get(url("mail") + path, params=params, timeout=10)
        r.raise_for_status()
        return r.json()

    def search(self, q=None, to=None, sender=None):
        return self.api("/api/v1/messages", q=q, to=to, **({"from": sender} if sender else {}))

    def probe(self, ctx, entity, key):
        for m in self.search(to=key["to"]):
            if m["subject"].strip().lower() == key["subject"].strip().lower():
                return {"exists": True, "id": m["id"], "body": m["body"], "sender": m["sender"]}, "verified"
        return None, "verified"

    def send(self, to, subject, body, attachments=(), in_reply_to=None):
        msg = EmailMessage()
        msg["From"] = vault()["mail"]["address"]
        msg["To"] = to
        msg["Subject"] = subject
        msg["Message-ID"] = email.utils.make_msgid(domain="acme.test")
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
        msg.set_content(body)
        for a in attachments or []:
            p = Path(a)
            mt = (mimetypes.guess_type(p.name)[0] or "application/octet-stream").split("/")
            msg.add_attachment(p.read_bytes(), maintype=mt[0], subtype=mt[1], filename=p.name)
        with smtplib.SMTP("127.0.0.1", PORTS["smtp"], timeout=10) as s:
            s.send_message(msg)
        return {"message_id": msg["Message-ID"]}

    def candidates(self, ctx, delta: Delta, args: dict):
        if delta.entity == "message" and delta.expect.get("exists") is True:
            return [Candidate("script:smtp", lambda _c: self.send(delta.key["to"], delta.key["subject"], args.get("body", ""),
                                                                   args.get("attachments"), args.get("in_reply_to")))]
        return []

    def inverse(self, delta, before):
        return None  # sent mail cannot be unsent
