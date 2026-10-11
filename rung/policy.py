"""Policy gate -- deterministic code outside the model.

Risk lives on the *intent* (what state changes), never on the rung that executes
it, so a learned tool cannot bypass a gate the UI path would have hit.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from rung.types import RISKS, Intent

POLICY_FILE = Path(__file__).resolve().parent.parent / "memory" / "policies.yaml"


@lru_cache
def policy() -> dict:
    return yaml.safe_load(POLICY_FILE.read_text())


def classify(intent: Intent) -> str:
    d = intent.delta
    for r in policy()["risk"]:
        w = r["when"]
        if w.get("app") not in (None, d.app) or w.get("entity") not in (None, d.entity):
            continue
        if "field" in w and w["field"] not in d.expect:
            continue
        if "value" in w and d.expect.get(w["field"]) != w["value"]:
            continue
        return r["risk"]
    return "irreversible" if intent.irreversible else "reversible_write"


def violations(intent: Intent, subject: dict) -> list[dict]:
    d = intent.delta
    out = []
    for rule in policy()["rules"]:
        deny = rule["deny"]
        if deny.get("app") not in (None, d.app) or deny.get("entity") not in (None, d.entity):
            continue
        if "channel" in deny and str(d.key.get("channel", "")).lstrip("#").lower() != deny["channel"]:
            continue
        if "expect_exists" in deny and d.expect.get("exists") != deny["expect_exists"]:
            continue
        if "expect_role" in deny and d.expect.get("role") != deny["expect_role"]:
            continue
        unless = rule.get("unless", {})
        if "subject_team" in unless and (subject or {}).get("team") == unless["subject_team"]:
            continue
        out.append({"rule": rule["id"], "reason": rule["reason"]})
    return out


def precedent_allowed(risk: str) -> bool:
    return RISKS.index(risk) <= RISKS.index(policy()["precedent_max_risk"])


def gate(intent: Intent) -> str:
    """-> auto | approve"""
    g = policy()["gates"][intent.risk]
    if g == "auto":
        return "auto"
    if g == "auto_if_procedure":
        return "auto" if intent.origin == "sop" else "approve"
    return "approve"


def trusted_sender(addr: str) -> bool:
    return addr.split("@")[-1].strip(">").lower() in policy()["trusted_mail_domains"]
