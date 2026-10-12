"""Model clients. Default backend: NVIDIA NIM (OpenAI-compatible, non-streaming).

  RUNG_MODEL=nim        NVIDIA_API_KEY=nvapi-...   [RUNG_MODEL_NAME=nvidia/nemotron-3-super-120b-a12b]
  RUNG_MODEL=openai     OPENAI_API_KEY=...  OPENAI_BASE_URL=...  RUNG_MODEL_NAME=...   (Kimi/Moonshot, OpenRouter, vLLM...)
  RUNG_MODEL=anthropic  ANTHROPIC_API_KEY=...  RUNG_MODEL_NAME=claude-...
  RUNG_MODEL=replay     RUNG_REPLAY=eval/model_run.jsonl   (deterministic replay of a recorded run; unknown prompts fail)
  unset                 `rung run` refuses: Rung needs a model

  RUNG_MAX_RPM=18       client-side cap on HTTP requests per rolling minute (NIM's limit is 20/min;
                        every attempt counts, retries included)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from collections import deque

import httpx

# a date with a time of day, or a bare time, as rendered by Django or ISO ("2026-10-11 17:41:03", "17:41:03")
WALL_CLOCK = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?([+-]\d{2}:\d{2})?|\b\d{2}:\d{2}:\d{2}\b")


class ModelError(Exception):
    pass


class RateLimiter:
    """At most `per_minute` requests in any rolling 60 s window: waits before sending, never drops."""

    def __init__(self, per_minute: int, clock=time.monotonic, sleep=time.sleep):
        self.per_minute, self.clock, self.sleep = per_minute, clock, sleep
        self.sent: deque = deque()

    def acquire(self):
        while True:
            now = self.clock()
            while self.sent and now - self.sent[0] >= 60:
                self.sent.popleft()
            if len(self.sent) < self.per_minute:
                self.sent.append(now)
                return
            self.sleep(60 - (now - self.sent[0]) + 0.05)


class OpenAICompatible:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 120, thinking_toggle: bool = False):
        self.base, self.key, self.model, self.timeout = base_url.rstrip("/"), api_key, model, timeout
        self.thinking_toggle = thinking_toggle   # NIM's Nemotron chat template accepts enable_thinking
        self.calls = 0
        self.limiter = RateLimiter(int(os.environ.get("RUNG_MAX_RPM", "18")))

    def complete(self, system: str, user: str, max_tokens: int = 1500, thinking: bool | None = None) -> str:
        body = {"model": self.model, "stream": False, "temperature": 0.2, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if thinking is not None and self.thinking_toggle:
            body["chat_template_kwargs"] = {"enable_thinking": thinking}
        last = None
        for attempt in range(6):
            try:
                self.limiter.acquire()   # every attempt counts against the provider's per-minute cap
                r = httpx.post(f"{self.base}/chat/completions", json=body, timeout=self.timeout,
                               headers={"Authorization": f"Bearer {self.key}"})
                if r.status_code in (429, 500, 502, 503, 504):
                    ex = ModelError(f"HTTP {r.status_code}: {r.text[:200]}")
                    ex.status = r.status_code
                    raise ex
                r.raise_for_status()
                self.calls += 1
                msg = r.json()["choices"][0]["message"]
                return msg.get("content") or ""
            except (httpx.HTTPError, ModelError) as ex:
                last = ex
                rate_limited = getattr(ex, "status", None) == 429   # back off much longer when throttled
                time.sleep(min(60, 5 * 2 ** attempt) if rate_limited else 1.5 * (attempt + 1))
        raise ModelError(f"model call failed after retries: {last}")


class Anthropic:
    def __init__(self, api_key: str, model: str, timeout: float = 90):
        self.key, self.model, self.timeout, self.calls = api_key, model, timeout, 0

    def complete(self, system: str, user: str, max_tokens: int = 1500, thinking: bool | None = None) -> str:
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

    def complete(self, system: str, user: str, max_tokens: int = 1500, thinking: bool | None = None) -> str:
        t0 = time.time()
        out = self.inner.complete(system, user, max_tokens, thinking)
        self.calls += 1
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"system": system, "user": user, "thinking": thinking, "output": out, "t": round(t0, 2),
                                "s": round(time.time() - t0, 2), "model": getattr(self.inner, "model", "?")}) + "\n")
        return out


class ReplayMiss(ModelError):
    """The run asked something the recording never saw: it diverged. Never answered."""


def replay_key(system: str, user: str, thinking) -> str:
    """Prompt identity for replay: the exact prompt and reasoning mode, except that wall-clock timestamps
    are masked. The sandbox's stock Django admin shows real 'date joined' times, which differ between a
    recorded run and its replay; nothing else is normalised."""
    return hashlib.sha1(f"{thinking}|{WALL_CLOCK.sub('<time>', system + user)}".encode()).hexdigest()


class Replay:
    """Replays a recorded session deterministically (same prompts -> same outputs).
    A prompt that isn't in the recording fails loudly; nothing is ever synthesized."""

    def __init__(self, path: str):
        self.calls, self.by_key, self.misses = 0, {}, []
        models = set()
        for line in open(path, encoding="utf-8"):
            r = json.loads(line)
            self.by_key.setdefault(replay_key(r["system"], r["user"], r.get("thinking")), []).append(r["output"])
            models.add(r.get("model", "?"))
        self.model = "replay of " + ", ".join(sorted(models))

    def complete(self, system: str, user: str, max_tokens: int = 1500, thinking: bool | None = None) -> str:
        k = replay_key(system, user, thinking)
        if not self.by_key.get(k):
            self.misses.append({"key": k, "system": system[:80], "user": user[:300]})
            print(f"REPLAY MISS {k}: prompt not in recording (the run diverged): {user[:160]!r}", file=sys.stderr, flush=True)
            raise ReplayMiss(f"prompt {k} not in recording (the run diverged from the recorded one)")
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
                                name or "nvidia/nemotron-3-super-120b-a12b", thinking_toggle=True)
    if kind == "openai":
        return OpenAICompatible(os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"), os.environ["OPENAI_API_KEY"], name)
    if kind == "replay":
        return Replay(os.environ["RUNG_REPLAY"])
    if kind == "anthropic":
        return Anthropic(os.environ["ANTHROPIC_API_KEY"], name or "claude-sonnet-5-5")
    return None
