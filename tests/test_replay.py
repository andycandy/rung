"""Replay of a real recorded NIM run (RUNG_MODEL=replay): the first eval case, re-run against
the live sandbox with the model's recorded answers, must reach the same verified outcome.
A prompt that isn't in the recording fails the test (the run diverged); nothing is synthesized."""
import os
from pathlib import Path

import pytest

REC = Path(__file__).resolve().parent.parent / "eval" / "model_run.jsonl"


@pytest.mark.sandbox
@pytest.mark.skipif(not REC.exists(), reason="no recorded model run (eval/model_run.jsonl)")
def test_replayed_cold_onboarding_matches_the_recording():
    from eval import oracle as O
    from eval.run_eval import ANALYST_CHANNELS, reset
    from rung import runtime, store
    from rung.llm.client import Replay
    reset()
    m = Replay(str(REC))
    rid = runtime.start("Please onboard Priya, she's joining Monday as a data analyst", "arjun.mehta@acme.test",
                        lambda k, q, o: "yes" if k == "confirm" else ("deny" if k == "approve" else None), m)
    assert m.misses == [], "run diverged from the recording"
    u = O.chat_user("priya.sharma")
    assert u and u["active"] and set(u["channels"]) == ANALYST_CHANNELS and u["role"] == "member"
    assert store.get_run(rid)["state"] == "scheduled"
