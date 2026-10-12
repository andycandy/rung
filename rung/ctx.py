"""Per-run context: identity, browser, model, event log, approvals."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from rung import store
from sandbox.common import VAR


def vault() -> dict:
    """Credentials are read only by adapters -- never placed in model context."""
    return json.loads((VAR / "vault.json").read_text())


class CountingModel:
    """Counts every completion made on behalf of one run, retries included."""

    def __init__(self, inner, stats: dict, log=None):
        self.inner, self.stats, self.log = inner, stats, log

    def complete(self, system: str, user: str, max_tokens: int = 1500, thinking: bool | None = None) -> str:
        self.stats["llm_calls"] += 1
        out = self.inner.complete(system, user, max_tokens, thinking)
        client = self.inner if hasattr(self.inner, "last_reasoning") else getattr(self.inner, "inner", None)
        reasoning = getattr(client, "last_reasoning", "") or ""
        if self.log and reasoning:   # the model's own reasoning text (calls with reasoning on), for audit and the demo viewer
            self.log("model.thinking", {"call": system[:60], "text": reasoning[:1500]})
            client.last_reasoning = ""
        return out


@dataclass
class Ctx:
    rid: str
    model: object = None                     # LLM client (required to understand a request or explore a UI)
    approver: Optional[Callable] = None      # fn(question:str, options:list[str]) -> str
    browser: object = None
    stats: dict = field(default_factory=lambda: {"llm_calls": 0, "explore_steps": 0, "compiled_steps": 0, "t0": time.time()})

    def __post_init__(self):
        if self.model is not None and not isinstance(self.model, CountingModel):
            self.model = CountingModel(self.model, self.stats, self.log)

    def log(self, kind: str, data: dict | None = None):
        store.event(self.rid, kind, data or {})

    def get_browser(self):
        if self.browser is None:
            from rung.explore.browser import Browser
            self.browser = Browser(store.artifacts_dir(self.rid))
        return self.browser

    def close(self):
        if self.browser is not None:
            self.browser.close()
            self.browser = None
