"""AppInterface: one adapter per app. Adapters hold app-specific knowledge only
(how to log in, how to read state, what the state projection is); the ladder,
verification and promotion logic live in core."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from rung.types import Delta


@dataclass
class Candidate:
    """A non-learned way to execute a delta (documented API, existing CLI/script)."""
    name: str            # documented_api | script:<tool>
    run: Callable        # fn(ctx) -> dict (raises on failure)
    risk_note: str = ""


def same(a, b) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().lower() == b.strip().lower()
    return a == b


class App:
    name = "app"
    kind = "web"                     # web | desktop | service
    explorable = False               # has a UI the explorer can operate
    projection: dict = {}            # entity -> {"key": [...], "compare": [...], "ignore": [...]}

    def probe(self, ctx, entity: str, key: dict) -> tuple[Optional[dict], str]:
        """Return (state or None if absent, label) -- label is verified|judged|unverified."""
        raise NotImplementedError

    def holds(self, ctx, delta: Delta) -> tuple[bool, Optional[dict], str]:
        state, label = self.probe(ctx, delta.entity, delta.key)
        want_exists = delta.expect.get("exists", True)
        if state is None:
            return (not want_exists), None, label
        if not want_exists:
            return False, state, label
        for f, v in delta.expect.items():
            if f == "exists":
                continue
            if f.endswith("__contains"):
                if str(v).lower() not in str(state.get(f[:-10], "")).lower():
                    return False, state, label
            elif not same(state.get(f), v):
                return False, state, label
        return True, state, label

    def values(self, delta: Delta, args: dict) -> dict:
        return {**delta.key, **{k: v for k, v in delta.expect.items() if k != "exists"}, **args}

    def relogin(self, ctx):
        pass

    def candidates(self, ctx, delta: Delta, args: dict) -> list[Candidate]:
        return []

    def explore(self, ctx, delta: Delta, args: dict):
        raise NotImplementedError

    def inverse(self, delta: Delta, before: Optional[dict]) -> Optional[Delta]:
        """Compensating delta, or None if irreversible."""
        if delta.expect.get("exists") is True:
            return Delta(delta.app, delta.entity, delta.key, {"exists": False})
        if before:
            return Delta(delta.app, delta.entity, delta.key, {f: before.get(f) for f in delta.expect if f != "exists"})
        return None

    def http(self, ctx):
        raise NotImplementedError

    def test_args(self, ctx, delta: Delta, values: dict) -> Optional[tuple[Delta, dict]]:
        """Sandbox-test variant of a delta (operates on a disposable test entity)."""
        return None

    def snapshot(self, ctx, delta: Delta) -> dict:
        """Projected state relevant to a delta, for the scoped before/after diff."""
        state, _ = self.probe(ctx, delta.entity, delta.key)
        spec = self.projection.get(delta.entity, {})
        if state is None:
            return {}
        return {k: state.get(k) for k in spec.get("compare", state.keys())}
