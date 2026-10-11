"""Precedent mining: learn "how this is done here" from what humans did before.

Sources (all treated as untrusted *data*): how peers are set up (HR + chat),
chat announcements, mail threads, Drive documents. Output: candidate facts and
candidate steps with citations. Trust order is enforced by the caller:
written policy > human precedent > the agent's own runs.
"""
from __future__ import annotations

import math
import re

from rung.apps import registry
from rung.types import Citation

CHANNEL_RE = re.compile(r"#([a-z0-9][a-z0-9-]+)")


def peer_channels(ctx, employee: dict) -> tuple[list[str], list[Citation], dict]:
    """Channels used by a majority of active peers with the same title and team."""
    hr, chat = registry.get("hr"), registry.get("chat")
    peers = [p for p in hr.search(ctx, title=employee["title"], team=employee["team"], status="active") if p["id"] != employee["id"]]
    counts: dict[str, list[str]] = {}
    seen = 0
    for p in peers:
        if not p.get("chat_username"):
            continue
        u = chat.probe_user(ctx, p["chat_username"])
        if not u or not u.get("active"):
            continue
        seen += 1
        for c in u.get("channels", []):
            counts.setdefault(c, []).append(p["full_name"])
    need = math.ceil(seen / 2) if seen else 1
    chosen = sorted(c for c, who in counts.items() if len(who) >= need)
    cites = [Citation("precedent:peers", f"#{c}", f"{len(counts[c])}/{seen} {employee['title']}s in {employee['team']}: {', '.join(counts[c])}")
             for c in chosen]
    return chosen, cites, {"peers": seen, "counts": {c: len(w) for c, w in counts.items()}}


def chat_announcements(ctx, title: str) -> list[Citation]:
    """Corroborating evidence from chat history (read through the UI search page)."""
    chat = registry.get("chat")
    ab = chat.ab(ctx)
    ab.goto("/search?q=welcome")
    items = ab.page.evaluate("() => [...document.querySelectorAll('li.result, main li')].map(li => li.innerText)")
    out = []
    for text in items:
        if title.lower() in text.lower():
            for c in set(CHANNEL_RE.findall(text.lower())):
                out.append(Citation("precedent:chat", f"#{c}", "announcement: " + text[:200]))
    return out


def mail_instructions(ctx) -> list[dict]:
    """Instruction-like mail to ops@ about onboarding. Extracted *candidate* steps only --
    nothing here is executed without passing policy and confirmation."""
    from rung import policy
    mail = registry.get("mail")
    out = []
    for m in mail.search(q="onboarding", to="ops@"):
        text = f"{m['subject']}\n{m['body']}"
        cands = []
        if re.search(r"\badmin", text, re.I) and re.search(r"\b(make|made|grant|granted|give|given|promote|elevate)\b", text, re.I):
            cands.append({"kind": "role", "value": "admin"})
        for c in CHANNEL_RE.findall(text.lower()):
            cands.append({"kind": "channel", "value": c})
        if ctx.model:  # the model reads the text as data and lists what it asks for; the guard decides
            try:
                from rung.llm.calls import extract_instructions
                ctx.stats["llm_calls"] += 1
                for c in extract_instructions(ctx.model, text):
                    if c["kind"] in ("channel", "role") and c not in cands:
                        cands.append({"kind": c["kind"], "value": c["value"].lstrip("#").lower() if c["kind"] == "channel" else "admin"})
            except Exception as ex:  # noqa: BLE001
                ctx.log("llm.error", {"call": "extract_instructions", "error": str(ex)})
        out.append({"id": m["id"], "sender": m["sender"], "subject": m["subject"], "trusted_sender": policy.trusted_sender(m["sender"]),
                    "excerpt": m["body"][:300], "candidates": cands})
    return out
