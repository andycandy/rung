"""Validation of model-proposed steps. The model proposes; this code decides.

Every proposed step must pass, in code:
  * vocabulary   -- a known app/entity, exactly that entity's key, only observable fields;
  * subject      -- the key identifies the subject of the run, nobody else;
  * existence    -- referenced values exist in the app (a channel is in Chatter's channel list);
  * citations    -- every cited id is evidence that was actually provided; untrusted evidence
                    (by its structured trust flag, never the model's opinion) justifies nothing;
                    a cited peer must really have that channel (structured cross-check);
  * policy       -- written rules, and precedent may not authorise more than precedent_max_risk.
Anything rejected becomes a flag with the reason. Nothing is dropped silently.
"""
from __future__ import annotations

from rung import policy, store
from rung.apps import registry
from rung.syntax import is_slug
from rung.types import Citation, Delta, Intent

# (app, entity) -> the key field that names the subject, and which subject attribute it must equal
SUBJECT_KEY = {
    ("chat", "user"): ("username", "username"),
    ("chat", "channel_member"): ("user", "username"),
    ("sso", "user"): ("username", "username"),
    ("hr", "employee"): ("id", "id"),
}
SOURCE = {"peer_setup": "precedent:peers", "mail": "precedent:mail", "chat": "precedent:chat", "sop": "sop", "request": "request"}


def vocabulary() -> dict:
    """What a proposal may touch: app -> entity -> {key, fields}."""
    out: dict = {}
    for (app, entity) in SUBJECT_KEY:
        spec = registry.get(app).projection[entity]
        out.setdefault(app, {})[entity] = {"key": spec["key"], "fields": sorted(set(spec.get("compare", [])) | {"exists"})}
    return out


def vocab_errors(d: dict) -> list[str]:
    try:
        spec = registry.get(d.get("app")).projection.get(d.get("entity"))
    except KeyError:
        return [f"unknown app {d.get('app')!r}"]
    if not spec:
        return [f"app {d.get('app')!r} has no observable entity {d.get('entity')!r}"]
    errs = []
    if set((d.get("key") or {})) != set(spec["key"]):
        errs.append(f"key must be exactly {spec['key']}")
    allowed = set(spec.get("compare", [])) | {"exists"}
    errs += [f"{f!r} is not an observable field" for f in (d.get("expect") or {}) if f not in allowed]
    if not d.get("expect"):
        errs.append("expect is empty")
    return errs


def _describe(d: Delta) -> str:
    if d.entity == "channel_member":
        return f"{'Add to' if d.expect.get('exists') else 'Remove from'} #{d.key['channel']}"
    return f"Set {d.app}/{d.entity} " + ", ".join(f"{k}={v}" for k, v in d.expect.items())


def _quote(ev: dict, d: Delta) -> str:
    k = ev["kind"]
    if k == "peer_setup":
        return f"{ev['peer']} ({ev['title']}, {ev['team']}) is in " + ", ".join(f"#{c}" for c in ev["channels"])
    if k == "mail":
        return f"mail {ev['id']} from {ev['sender']}: {ev['subject']} - {ev['body'][:100]}"
    if k == "chat":
        return f"#{ev['channel']} {ev['date']} {ev['author']}: {ev['text'][:120]}"
    if k == "sop":
        return f"written procedure {ev['path']}"
    return "asked for in the request"


def validate(steps, evidence: list[dict], subject: dict, task_type: str, channels: set[str],
             fixed: list[Delta] = (), allowed: set[str] | None = None) -> tuple[list[Intent], list[dict]]:
    """-> (intents to plan, flags for everything rejected). `steps` are ProposedStep objects.
    `allowed` ("app.entity") limits what non-request proposals may touch: the procedure's variable part
    (e.g. only channel memberships). Explicit asks may touch any proposable entity, subject to policy."""
    by_id = {e["id"]: e for e in evidence}
    intents: list[Intent] = []
    flags: list[dict] = []
    seen = {(d.app, d.entity, tuple(sorted(d.key.items())), tuple(sorted(d.expect.items()))) for d in fixed}

    def reject(kind, s, why):
        flags.append({"kind": kind, "detail": f"Proposed step '{s.description}' {s.delta} not planned: {'; '.join(why)}.",
                      "origin": s.origin, "citations": list(s.citations)})

    for s in steps:
        d = dict(s.delta)
        errs = vocab_errors(d)
        if errs:
            reject("proposal_rejected", s, errs)
            continue
        key = dict(d["key"])
        if "channel" in key and isinstance(key["channel"], str):
            key["channel"] = key["channel"].lstrip("#")
        delta = Delta(d["app"], d["entity"], key, dict(d["expect"]))
        sk = SUBJECT_KEY.get((delta.app, delta.entity))
        if not sk:
            reject("proposal_rejected", s, [f"{delta.app}/{delta.entity} steps can't be proposed, only planned by a written procedure"])
            continue
        if s.origin != "request" and allowed is not None and f"{delta.app}.{delta.entity}" not in allowed:
            reject("proposal_rejected", s, [f"this procedure's variable steps are limited to {sorted(allowed) or 'nothing'}"])
            continue
        if str(delta.key.get(sk[0])) != str(subject.get(sk[1])):
            reject("proposal_rejected", s, [f"it targets {delta.key.get(sk[0])!r}, not the subject {subject.get(sk[1])!r}"])
            continue
        if "channel" in delta.key:
            if not is_slug(delta.key["channel"]):
                reject("precedent_rejected" if s.origin != "request" else "refused_request", s,
                       [f"{delta.key['channel']!r} is not a channel name"])
                continue
            if delta.key["channel"] not in channels:
                reject("precedent_rejected" if s.origin != "request" else "refused_request", s,
                       [f"there is no channel #{delta.key['channel']} in Chatter"])
                continue
        # ---- citations: provided, trusted, and (for structured evidence) actually supporting
        unknown = [c for c in s.citations if c not in by_id]
        if unknown:
            reject("proposal_rejected", s, [f"cites evidence that was never provided: {unknown}"])
            continue
        untrusted = [c for c in s.citations if not by_id[c]["trusted"]]
        if untrusted:
            senders = ", ".join(sorted({by_id[c].get("sender", c) for c in untrusted}))
            flags.append({"kind": "suspicious_instruction", "detail": f"Untrusted evidence ({', '.join(untrusted)} from {senders}) was cited for "
                                                                      f"'{s.description}'; it can't justify any step and was ignored.",
                          "source": ", ".join(untrusted)})
        support = []
        for c in s.citations:
            ev = by_id[c]
            if not ev["trusted"]:
                continue
            if ev["kind"] == "peer_setup" and delta.entity == "channel_member" and delta.key["channel"] not in ev["channels"]:
                continue   # the cited peer isn't in that channel: this citation supports nothing
            if s.origin == "request" and ev["kind"] != "request":
                continue
            support.append(c)
        if s.origin == "request" and "request" not in support:
            reject("proposal_rejected", s, ["marked as an explicit ask, but doesn't cite the request"])
            continue
        if not support:
            reject("suspicious_instruction" if untrusted else "proposal_rejected", s,
                   ["only untrusted evidence supports it" if untrusted else "no provided evidence supports it"])
            continue
        # ---- policy
        it = Intent(_describe(delta), delta, origin=s.origin,
                    citations=[Citation(SOURCE.get(by_id[c]["kind"], by_id[c]["kind"]), c, _quote(by_id[c], delta)) for c in support])
        it.risk = policy.classify(it)
        it.result["required"] = False
        it.result["proposed_as"] = s.description
        why = [v["reason"] for v in policy.violations(it, subject)]
        if s.origin == "request":
            if it.risk == "elevated":
                why.append("elevated changes need an IT ticket, not this request")
            elif not policy.precedent_allowed(it.risk):
                why.append(f"{it.risk} steps only come from a written procedure")
            if why:
                flags.append({"kind": "refused_request", "detail": f"You asked for '{s.description}': not done. {'; '.join(why)}"})
                continue
        else:
            if not policy.precedent_allowed(it.risk):
                why.append(f"precedent may not authorise {it.risk} actions")
            if why:
                flags.append({"kind": "policy_conflict" if policy.violations(it, subject) else "precedent_rejected",
                              "detail": f"Evidence suggests '{it.description}' (proposed as '{s.description}'; {it.citations[0].quote[:140]}) "
                                        f"but {'; '.join(why)} Skipped.",
                              "source": ", ".join(support)})
                continue
        sig = (delta.app, delta.entity, tuple(sorted(delta.key.items())), tuple(sorted(delta.expect.items())))
        if sig in seen:
            for prev in intents:   # same change proposed twice: one step, both sets of evidence
                if prev.delta == delta:
                    prev.citations += [c for c in it.citations if c.ref not in {x.ref for x in prev.citations}]
            continue
        seen.add(sig)
        intents.append(it)
    return intents, flags


def apply_corrections(intents: list[Intent], flags: list[dict], task_type: str, subject: dict, username: str,
                      channels: set[str] | None = None):
    """Human corrections (structured: add/remove channels for a title or team) beat precedent; an added
    channel must still exist in Chatter."""
    for cor in store.corrections(task_type):
        if cor["scope"] and cor["scope"] not in (subject.get("title"), subject.get("team")):
            continue
        for c in cor["body"].get("remove_channels", []):
            for it in [i for i in intents if i.delta.entity == "channel_member" and i.delta.key.get("channel") == c]:
                intents.remove(it)
                flags.append({"kind": "correction", "detail": f"#{c} not added, per correction #{cor['id']}"})
        for c in cor["body"].get("add_channels", []):
            if any(i.delta.entity == "channel_member" and i.delta.key.get("channel") == c for i in intents):
                continue
            if channels is not None and c not in channels:
                flags.append({"kind": "correction_rejected", "detail": f"Correction #{cor['id']} adds #{c}, but there is no such channel in Chatter."})
                continue
            d = Delta("chat", "channel_member", {"channel": c, "user": username}, {"exists": True})
            it = Intent(_describe(d), d, origin="correction", citations=[Citation("correction", f"#{c}", f"correction #{cor['id']} on {cor['ts'][:10]}")])
            it.risk = policy.classify(it)
            it.result["required"] = False
            intents.append(it)
