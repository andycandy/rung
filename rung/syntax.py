"""Structured-syntax helpers: e-mail addresses, identifiers, template placeholders.
Nothing here reads the meaning of human text."""
from __future__ import annotations

import re
import string

EMAIL_SYNTAX = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")


def is_email(s) -> bool:
    return isinstance(s, str) and EMAIL_SYNTAX.fullmatch(s.strip()) is not None


def email_domain(addr: str) -> str:
    return addr.strip().strip("<>").rpartition("@")[2].casefold()


def is_slug(s) -> bool:
    """A chat-channel-style name: lowercase ascii letters, digits and dashes."""
    return isinstance(s, str) and len(s) > 1 and s[0] != "-" and all(c in string.ascii_lowercase + string.digits + "-" for c in s)


def is_snake(s) -> bool:
    return isinstance(s, str) and s.isidentifier() and s == s.casefold() and not s.startswith("_")


def placeholders(template: str) -> list[str]:
    """Names inside {...} in one of our own YAML templates ('Add to #{item}' -> ['item'])."""
    return [f for _, f, _, _ in string.Formatter().parse(template) if f]


def confirm_code(body: str) -> str | None:
    """The one-time code in a 'CONFIRM XXXXXXXX' mail reply (8 upper-case hex characters)."""
    toks = (body or "").split()
    for word, code in zip(toks, toks[1:]):
        if word == "CONFIRM" and len(code) == 8 and all(c in "0123456789ABCDEF" for c in code):
            return code
    return None


def choice(v, options) -> str | None:
    """A reply to a closed-set question (yes/no, reactivate/new), or None if it isn't one."""
    norm = str(v).strip().casefold()
    return norm if norm in options else None
