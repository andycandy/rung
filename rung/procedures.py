"""Procedures are data. This module validates them -- hand-written or model-drafted --
against what the system can actually do (known apps, entities, observable fields,
library checks/derivations), and manages model-drafted ones through human approval.

Flow for an unknown task: the requester points at a prose SOP on the Drive ->
the model drafts a structured procedure -> it is validated here -> the requester
approves -> it is saved to memory/procedures/ and used from then on.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from rung import derive
from rung.apps import registry
from rung.syntax import is_snake, placeholders

PROC_DIR = Path(__file__).resolve().parent.parent / "memory" / "procedures"
DRAFTS = PROC_DIR / "drafts"
EXTRA_KEYS = {"mail": {"message": {"key": ["to", "subject"], "compare": ["exists"]}}}


def _entity_spec(app: str, entity: str):
    a = registry.get(app)
    spec = a.projection.get(entity)
    return spec


def _strings(x):
    if isinstance(x, dict):
        for v in x.values():
            yield from _strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from _strings(v)
    elif isinstance(x, str):
        yield x


def validate(proc: dict) -> list[str]:
    errs = []
    for k in ("task_type", "description", "steps"):
        if k not in proc:
            errs.append(f"missing '{k}'")
    if errs:
        return errs
    if not is_snake(str(proc["task_type"])):
        errs.append("task_type must be snake_case")
    if "match" in proc:
        errs.append("'match' lists are not allowed: the model picks the task from 'description'")
    for c in proc.get("checks", []):
        if c not in derive.CHECKS:
            errs.append(f"unknown check '{c}' (known: {sorted(derive.CHECKS)})")
    names = {"employee", "drive", "requester", "request", "date", "date_text", "item"}
    for d in proc.get("derive", []):
        if d.get("fn") not in derive.FNS:
            errs.append(f"unknown derivation '{d.get('fn')}' (known: {sorted(derive.FNS)})")
        names.add(d.get("name"))
    prop = proc.get("propose")
    if prop is not None:
        from rung.precedent import SOURCES
        if not isinstance(prop, dict) or not prop.get("about"):
            errs.append("propose: needs 'about' (what the model should propose)")
        else:
            errs += [f"propose: unknown evidence source '{e}' (known: {sorted(SOURCES)})" for e in prop.get("evidence", []) if e not in SOURCES]
            from rung.proposals import SUBJECT_KEY
            known = {f"{a}.{e}" for a, e in SUBJECT_KEY}
            errs += [f"propose: '{x}' can't be proposed (known: {sorted(known)})" for x in prop.get("entities", []) if x not in known]
            if prop.get("after") and prop["after"] not in [st.get("id") for st in proc["steps"]]:
                errs.append(f"propose: 'after' names no step: {prop['after']}")
    for i, st in enumerate(proc["steps"]):
        where = f"step {i + 1} ({st.get('id', '?')})"
        dl = st.get("delta") or {}
        try:
            registry.get(dl.get("app"))
        except KeyError:
            errs.append(f"{where}: unknown app '{dl.get('app')}' (known: {registry.names()})")
            continue
        spec = _entity_spec(dl["app"], dl.get("entity"))
        if not spec:
            errs.append(f"{where}: app '{dl['app']}' has no observable entity '{dl.get('entity')}'")
            continue
        allowed = set(spec.get("compare", [])) | {"exists"}
        for f in (dl.get("expect") or {}):
            base = f[:-10] if f.endswith("__contains") else f
            if base not in allowed and not (base == "text" and "text" in allowed):
                errs.append(f"{where}: '{f}' is not an observable field of {dl['app']}/{dl['entity']} (allowed: {sorted(allowed)})")
        if set((dl.get("key") or {}).keys()) != set(spec.get("key", [])):
            errs.append(f"{where}: key must be exactly {spec.get('key')} for {dl['app']}/{dl['entity']}")
        if not st.get("description"):
            errs.append(f"{where}: needs a description")
        for text in _strings(st):
            try:
                found = placeholders(text)
            except ValueError as ex:
                errs.append(f"{where}: malformed template {text[:40]!r}: {ex}")
                continue
            for ph in found:
                if ph.split(".")[0] not in names:
                    errs.append(f"{where}: placeholder '{{{ph}}}' is never defined")
    return errs


def draft_from_sop(ctx, sop_path: str) -> tuple[dict | None, list[str]]:
    """Draft from one Drive document the requester pointed at (`sop: <path>`)."""
    from rung.apps.docs import drive_path
    p = drive_path(sop_path)
    if not p.exists():
        return None, [f"no such document on the Drive: {sop_path}"]
    return draft_from_sources(ctx, [{"id": f"doc:{sop_path}", "label": sop_path, "text": p.read_text()}])


def draft_from_sources(ctx, sources: list[dict]) -> tuple[dict | None, list[str]]:
    """Draft a procedure from one or more source texts ({id, label, text}): documents and/or mails.
    The validator and its error feedback are the same whatever the sources; the draft is saved under
    drafts/ only, and becomes a procedure only when a human approves it."""
    from rung.llm.calls import draft_procedure
    vocab = {"apps": {n: {e: {"key": s.get("key"), "fields": s.get("compare")} for e, s in registry.get(n).projection.items()}
                      for n in registry.names()},
             "checks": sorted(derive.CHECKS), "derivations": sorted(derive.FNS),
             "example": yaml.safe_load((PROC_DIR / "offboarding.yaml").read_text())}
    text = "\n\n".join(f"### Source {s['label']}\n{s['text']}" for s in sources)
    errs: list[str] = []
    for attempt in range(3):   # validation errors are fed back to the model
        proc = draft_procedure(ctx.model, text, vocab, errs)
        errs = validate(proc)
        ctx.log("procedure.draft", {"sources": [s["id"] for s in sources], "attempt": attempt + 1, "errors": errs})
        if not errs:
            save_draft(proc)
            return proc, []
    return None, errs


def save_draft(proc: dict) -> Path:
    DRAFTS.mkdir(parents=True, exist_ok=True)
    p = DRAFTS / f"{proc['task_type']}.yaml"
    p.write_text(yaml.safe_dump(proc, sort_keys=False))
    return p


def approve(task_type: str) -> bool:
    d = DRAFTS / f"{task_type}.yaml"
    if not d.exists():
        return False
    d.rename(PROC_DIR / f"{task_type}.yaml")
    return True
