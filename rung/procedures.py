"""Procedures are data. This module validates them -- hand-written or model-drafted --
against what the system can actually do (known apps, entities, observable fields,
library checks/derivations), and manages model-drafted ones through human approval.

Flow for an unknown task: the requester points at a prose SOP on the Drive ->
the model drafts a structured procedure -> it is validated here -> the requester
approves -> it is saved to memory/procedures/ and used from then on.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from rung import derive
from rung.apps import registry

PROC_DIR = Path(__file__).resolve().parent.parent / "memory" / "procedures"
DRAFTS = PROC_DIR / "drafts"
EXTRA_KEYS = {"mail": {"message": {"key": ["to", "subject"], "compare": ["exists"]}}}


def _entity_spec(app: str, entity: str):
    a = registry.get(app)
    spec = a.projection.get(entity)
    return spec


def validate(proc: dict) -> list[str]:
    errs = []
    for k in ("task_type", "match", "steps"):
        if k not in proc:
            errs.append(f"missing '{k}'")
    if errs:
        return errs
    if not re.fullmatch(r"[a-z][a-z0-9_]*", str(proc["task_type"])):
        errs.append("task_type must be snake_case")
    for c in proc.get("checks", []):
        if c not in derive.CHECKS:
            errs.append(f"unknown check '{c}' (known: {sorted(derive.CHECKS)})")
    names = {"employee", "drive", "requester", "request", "date", "date_text", "item"}
    for d in proc.get("derive", []):
        if d.get("fn") not in derive.FNS:
            errs.append(f"unknown derivation '{d.get('fn')}' (known: {sorted(derive.FNS)})")
        names.add(d.get("name"))
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
        blob = yaml.safe_dump(st)
        for ph in re.findall(r"\{([\w]+)[.}]", blob):
            if ph not in names:
                errs.append(f"{where}: placeholder '{{{ph}}}' is never defined")
    return errs


def draft_from_sop(ctx, sop_path: str) -> tuple[dict | None, list[str]]:
    from rung.apps.docs import drive_path
    from rung.llm.calls import draft_procedure
    p = drive_path(sop_path)
    if not p.exists():
        return None, [f"no such document on the Drive: {sop_path}"]
    vocab = {"apps": {n: {e: {"key": s.get("key"), "fields": s.get("compare")} for e, s in registry.get(n).projection.items()}
                      for n in registry.names()},
             "checks": sorted(derive.CHECKS), "derivations": sorted(derive.FNS),
             "example": yaml.safe_load((PROC_DIR / "offboarding.yaml").read_text())}
    errs: list[str] = []
    for attempt in range(3):   # validation errors are fed back to the model
        ctx.stats["llm_calls"] += 1
        proc = draft_procedure(ctx.model, p.read_text(), vocab, errs)
        errs = validate(proc)
        ctx.log("procedure.draft", {"sop": sop_path, "attempt": attempt + 1, "errors": errs})
        if not errs:
            DRAFTS.mkdir(parents=True, exist_ok=True)
            (DRAFTS / f"{proc['task_type']}.yaml").write_text(yaml.safe_dump(proc, sort_keys=False))
            return proc, []
    return None, errs


def approve(task_type: str) -> bool:
    d = DRAFTS / f"{task_type}.yaml"
    if not d.exists():
        return False
    d.rename(PROC_DIR / f"{task_type}.yaml")
    return True
