"""Evidence for "how this is done here", gathered deterministically from structured sources.

Code never reads the evidence for meaning: it collects it, tags each item with an id and a
trust flag (from structured facts such as the sender's domain), and hands it to the model,
which proposes steps (rung.llm.calls.propose_steps). rung.proposals then validates every
proposal against the apps, written policy and these trust flags.
"""
from __future__ import annotations

from rung import policy
from rung.apps import registry
from rung.syntax import confirm_code

MAIL_LIMIT = 15        # most recent messages to ops@
CHAT_PER_CHANNEL = 10  # most recent messages per channel


def peers(ctx, employee: dict) -> list[dict]:
    """How active colleagues with the same title and team are set up in chat (one item per peer)."""
    hr, chat = registry.get("hr"), registry.get("chat")
    out = []
    for p in hr.search(ctx, title=employee["title"], team=employee["team"], status="active"):
        if p["id"] == employee["id"] or not p.get("chat_username"):
            continue
        u = chat.probe_user(ctx, p["chat_username"])
        if not u or not u.get("active"):
            continue
        out.append({"id": f"peer:{p['chat_username']}", "kind": "peer_setup", "trusted": True, "peer": p["full_name"],
                    "title": p["title"], "team": p["team"], "channels": sorted(u.get("channels", []))})
    return out


def mail(ctx) -> list[dict]:
    """Recent mail to ops@, unfiltered: sender, trust flag from the sender's domain, full body."""
    out = []
    for m in [m for m in registry.get("mail").search(to="ops@") if not confirm_code(m["body"])][:MAIL_LIMIT]:
        # confirmation-code replies are protocol, not knowledge (and their random codes would vary between runs)
        out.append({"id": f"mail:{m['id']}", "kind": "mail", "trusted": policy.trusted_sender(m["sender"]), "sender": m["sender"],
                    "date": (m.get("ts") or "")[:10], "subject": m["subject"], "body": m["body"]})
    return out


def chat(ctx) -> list[dict]:
    """Recent messages in every chat channel, as posted (internal workspace: trusted authors)."""
    out = []
    for item in registry.get("chat").recent_messages(ctx, CHAT_PER_CHANNEL):
        out.append({"id": f"chat:{len(out) + 1}", "kind": "chat", "trusted": True, **item})
    return out


def sop(ctx, path: str) -> list[dict]:
    from rung.apps.docs import drive_path
    p = drive_path(path)
    if not p.exists():
        return []
    return [{"id": f"sop:{path}", "kind": "sop", "trusted": True, "path": path, "text": p.read_text()[:4000]}]


def request(asks: list[str]) -> dict:
    """The requester's explicit asks. The requester is authorised, but the asks still go through validation."""
    return {"id": "request", "kind": "request", "trusted": True, "asks": list(asks)}


SOURCES = {"peers": lambda ctx, emp, spec: peers(ctx, emp), "mail": lambda ctx, emp, spec: mail(ctx),
           "chat": lambda ctx, emp, spec: chat(ctx), "sop": lambda ctx, emp, spec: sop(ctx, spec["sop"]) if spec.get("sop") else []}


def gather(ctx, employee: dict, spec: dict, asks: list[str]) -> list[dict]:
    """Evidence named by the procedure's `propose.evidence`, plus the request's own asks."""
    ev = []
    for name in spec.get("evidence", []):
        try:
            ev += SOURCES[name](ctx, employee, spec)
        except Exception as ex:  # noqa: BLE001 -- a source being down means less evidence, never a guess
            ctx.log("evidence.error", {"source": name, "error": f"{type(ex).__name__}: {ex}"})
    ev.append(request(asks))
    ctx.log("evidence.gathered", {"items": [{"id": e["id"], "trusted": e["trusted"]} for e in ev]})
    return ev
