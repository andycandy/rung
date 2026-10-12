"""Core vocabulary (frozen).

Intent identity is defined by its *state delta*, not by a verb name: two requests
phrased differently ("add Priya to #data", "give her access to the data room")
that produce the same delta shape reuse the same learned interface.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

# risk classes, in increasing order
RISKS = ["read", "reversible_write", "irreversible", "elevated"]


def risk_max(a: str, b: str) -> str:
    return a if RISKS.index(a) >= RISKS.index(b) else b


@dataclass
class Delta:
    """Expected change of observable state: entity `entity` in `app`, identified by
    `key`, should afterwards satisfy `expect` (field -> value; `exists` is special)."""
    app: str
    entity: str
    key: dict
    expect: dict

    def signature(self) -> str:
        """Shape of the change, independent of the concrete entity. Boolean/enum-like
        targets are part of the shape (deactivate != reactivate); free-text values are not."""
        parts = []
        for k in sorted(self.expect):
            v = self.expect[k]
            parts.append(f"{k}={v}" if isinstance(v, bool) or k in ("role", "status") else f"{k}=*")
        return f"{self.app}:{self.entity}:{','.join(sorted(self.key))}:{'|'.join(parts)}"

    def sig_id(self) -> str:
        return hashlib.sha1(self.signature().encode()).hexdigest()[:10]

    def describe(self) -> str:
        k = ", ".join(f"{a}={b}" for a, b in self.key.items())
        e = ", ".join(f"{a}={b}" for a, b in self.expect.items())
        return f"{self.app}/{self.entity}[{k}] -> {e}"


@dataclass
class Citation:
    source: str      # sop | policy | precedent:mail | precedent:chat | precedent:peers | request | hr | correction | runlog
    ref: str         # where (message id, file path, peer names...)
    quote: str = ""  # short excerpt (always treated as data, never instructions)


@dataclass
class Intent:
    description: str
    delta: Delta
    args: dict = field(default_factory=dict)
    risk: str = "reversible_write"
    irreversible: bool = False
    not_before: Optional[str] = None        # ISO time; scheduled intents wait for it
    origin: str = "sop"                     # sop | precedent | request | correction
    citations: list = field(default_factory=list)
    needs_confirmation: bool = False        # precedent-only steps on first use
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    status: str = "pending"                 # pending|skipped|done|failed|compensated|scheduled|blocked
    result: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)

    @staticmethod
    def from_dict(d):
        d = dict(d)
        d["delta"] = Delta(**d["delta"])
        d["citations"] = [Citation(**c) if isinstance(c, dict) else c for c in d.get("citations", [])]
        return Intent(**d)


@dataclass
class Gap:
    """An unstated detail of the request and how it was resolved."""
    field: str
    value: Any = None
    how: str = "resolved"   # resolved | default | ask
    source: str = ""
    question: str = ""


@dataclass
class GoalSpec:
    request: str
    requester: str
    task_type: str
    subject: dict = field(default_factory=dict)      # resolved primary entity (e.g. the employee)
    gaps: list = field(default_factory=list)
    intents: list = field(default_factory=list)
    flags: list = field(default_factory=list)        # policy conflicts, injection attempts, ...
    questions: list = field(default_factory=list)
    split: list = field(default_factory=list)        # several people named -> one child run each
    rejected: str = ""                               # authorization failure (nothing is done)
    ignored: bool = False                            # the model read it as not a request: nothing to do

    def to_json(self):
        return json.dumps(asdict(self), default=str)


@dataclass
class ExecResult:
    ok: bool
    rung: str = ""            # which rung executed (learned:<id> | documented_api | script:<tool> | explore:llm | noop:already-satisfied)
    detail: str = ""
    ms: int = 0
    llm_calls: int = 0
    recording: list = field(default_factory=list)
    data: dict = field(default_factory=dict)
