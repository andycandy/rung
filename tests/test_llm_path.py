"""Exercises the model code paths end to end against the live sandbox using a
scripted FakeModel (prompt -> JSON -> validation -> actions). Requires the sandbox."""
import pytest

from eval import oracle as O
from eval.run_eval import reset
from rung import runtime, store
from tests.fake_model import FakeModel


@pytest.mark.sandbox
def test_model_paths_drive_a_real_run():
    reset()
    m = FakeModel()
    rid = runtime.start("Please onboard Priya, she's joining Monday", "arjun.mehta@acme.test",
                        lambda k, q, o: "yes" if k == "confirm" else None, m)
    kinds = [e["kind"] for e in store.events(rid)]
    u = O.chat_user("priya.sharma")
    assert m.calls > 0
    assert any("extract structured fields" in s for s in m.log)          # resolve_goal used the model
    assert any("operate a web app UI" in s for s in m.log)              # explore_step used the model
    assert u and "data" in u["channels"] and u["role"] == "member"      # the model-driven UI actions worked
    assert "explore.fallback" in kinds                                   # unknown deltas fell back to the heuristic
    goal = runtime.load_goal(rid)
    assert any(f["kind"] == "suspicious_instruction" for f in goal.flags)
