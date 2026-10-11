"""Model clients. Default backend: NVIDIA NIM (OpenAI-compatible, non-streaming).

  RUNG_MODEL=nim        NVIDIA_API_KEY=nvapi-...   [RUNG_MODEL_NAME=nvidia/nemotron-3-super-120b-a12b]
  RUNG_MODEL=openai     OPENAI_API_KEY=...  OPENAI_BASE_URL=...  RUNG_MODEL_NAME=...   (Kimi/Moonshot, OpenRouter, vLLM...)
  RUNG_MODEL=anthropic  ANTHROPIC_API_KEY=...  RUNG_MODEL_NAME=claude-...
  unset / offline       heuristic fallbacks only (no model)
"""
from __future__ import annotations

import json
import os
import time

import httpx


class ModelError(Exception):
    pass


class OpenAICompatible:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 90):
        self.base, self.key, self.model, self.timeout = base_url.rstrip("/"), api_key, model, timeout
        self.calls = 0

    def complete(self, system: str, user: str, max_tokens: int = 1500) -> str:
        body = {"model": self.model, "stream": False, "temperature": 0.2, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        last = None
        for attempt in range(3):
            try:
                r = httpx.post(f"{self.base}/chat/completions", json=body, timeout=self.timeout,
                               headers={"Authorization": f"Bearer {self.key}"})
                if r.status_code in (429, 500, 502, 503, 504):
                    raise ModelError(f"HTTP {r.status_code}: {r.text[:200]}")
                r.raise_for_status()
                self.calls += 1
                msg = r.json()["choices"][0]["message"]
                return msg.get("content") or ""
            except (httpx.HTTPError, ModelError) as ex:
                last = ex
                time.sleep(1.5 * (attempt + 1))
        raise ModelError(f"model call failed after retries: {last}")


class Anthropic:
    def __init__(self, api_key: str, model: str, timeout: float = 90):
        self.key, self.model, self.timeout, self.calls = api_key, model, timeout, 0

    def complete(self, system: str, user: str, max_tokens: int = 1500) -> str:
        r = httpx.post("https://api.anthropic.com/v1/messages", timeout=self.timeout,
                       headers={"x-api-key": self.key, "anthropic-version": "2023-06-01"},
                       json={"model": self.model, "max_tokens": max_tokens, "system": system,
                             "messages": [{"role": "user", "content": user}]})
        r.raise_for_status()
        self.calls += 1
        return "".join(b.get("text", "") for b in r.json()["content"])


class Recording:
    """Wraps any client; appends every prompt/response to a JSONL file (audit + replay)."""

    def __init__(self, inner, path: str):
        self.inner, self.path, self.calls = inner, path, 0

    def complete(self, system: str, user: str, max_tokens: int = 1500) -> str:
        t0 = time.time()
        out = self.inner.complete(system, user, max_tokens)
        self.calls += 1
        with open(self.path, "a") as f:
            f.write(json.dumps({"system": system, "user": user, "output": out, "s": round(time.time() - t0, 2),
                                "model": getattr(self.inner, "model", "?")}) + "\n")
        return out


class Replay:
    """Replays a recorded session deterministically (same prompts -> same outputs)."""

    def __init__(self, path: str):
        import hashlib
        self.calls, self.by_key = 0, {}
        for line in open(path):
            r = json.loads(line)
            self.by_key.setdefault(hashlib.sha1((r["system"] + r["user"]).encode()).hexdigest(), []).append(r["output"])
        self._h = hashlib

    def complete(self, system: str, user: str, max_tokens: int = 1500) -> str:
        k = self._h.sha1((system + user).encode()).hexdigest()
        if not self.by_key.get(k):
            raise ModelError("prompt not in recording (the run diverged from the recorded one)")
        self.calls += 1
        return self.by_key[k].pop(0)


def from_env():
    m = _from_env()
    if m is not None and os.environ.get("RUNG_RECORD"):
        return Recording(m, os.environ["RUNG_RECORD"])
    return m


def _from_env():
    kind = os.environ.get("RUNG_MODEL", "").lower()
    name = os.environ.get("RUNG_MODEL_NAME")
    if kind == "nim":
        return OpenAICompatible("https://integrate.api.nvidia.com/v1", os.environ["NVIDIA_API_KEY"],
                                name or "nvidia/nemotron-3-super-120b-a12b")
    if kind == "openai":
        return OpenAICompatible(os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"), os.environ["OPENAI_API_KEY"], name)
    if kind == "replay":
        return Replay(os.environ["RUNG_REPLAY"])
    if kind == "anthropic":
        return Anthropic(os.environ["ANTHROPIC_API_KEY"], name or "claude-sonnet-5-5")
    return None
