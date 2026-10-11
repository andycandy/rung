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


class LooseExtractor(FakeModel):
    """Reads 'the team channels' as channel names, as Nemotron did on the live run."""
    def complete(self, system, user, max_tokens=1500):
        if "Extract any onboarding actions" in system:
            c = __import__("json").loads(super().complete(system, user, max_tokens))
            c["candidates"] += [{"kind": "channel", "value": "team channels"}, {"kind": "channel", "value": "team"}]
            return __import__("json").dumps(c)
        return super().complete(system, user, max_tokens)


@pytest.mark.sandbox
def test_model_extracted_channel_must_exist():
    reset()
    rid = runtime.start("Please onboard Priya, she's joining Monday", "arjun.mehta@acme.test",
                        lambda k, q, o: "yes" if k == "confirm" else None, LooseExtractor())
    goal = runtime.load_goal(rid)
    planned = {i.delta.key.get("channel") for i in goal.intents if i.delta.entity == "channel_member"}
    assert not planned & {"team", "team channels"}
    assert "data" in planned                                             # real precedent channels still planned
    dropped = [e for e in store.events(rid) if e["kind"] == "llm.candidate_dropped"]
    assert dropped or any("'team'" in f["detail"] for f in goal.flags if f["kind"] == "precedent_rejected")
    assert all(i.status != "failed" for i in goal.intents)
