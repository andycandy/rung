"""Procedure validation and the model-drafted-procedure flow (FakeModel)."""
import glob

import pytest
import yaml

from rung.procedures import validate


def test_committed_procedures_are_valid():
    for f in glob.glob("memory/procedures/*.yaml"):
        assert validate(yaml.safe_load(open(f))) == [], f


@pytest.mark.parametrize("bad, msg", [
    ({"app": "chat", "entity": "user", "key": {"username": "{username}"}, "expect": {"salary": 1}}, "not an observable field"),
    ({"app": "crm", "entity": "lead", "key": {}, "expect": {}}, "unknown app"),
    ({"app": "chat", "entity": "user", "key": {"id": "{x}"}, "expect": {"exists": True}}, "key must be exactly"),
])
def test_invalid_steps_are_rejected(bad, msg):
    p = {"task_type": "t", "match": ["t"], "derive": [{"name": "username", "fn": "username_for"}],
         "steps": [{"id": "s", "description": "d", "delta": bad}]}
    assert any(msg in e for e in validate(p))


@pytest.mark.sandbox
def test_unknown_task_learns_procedure_from_sop_with_approval():
    from eval import oracle as O
    from eval.run_eval import reset
    from rung import runtime, store
    from tests.fake_model import FakeModel
    reset()
    m = FakeModel()
    req, who = "Please give Kavya Reddy dashboard access", "arjun.mehta@acme.test"
    r1 = runtime.start(req, who, None, m)
    assert store.get_run(r1)["state"] == "waiting_input"             # no procedure: asks for a doc
    r2 = runtime.start(req, who, None, m, {"sop": "handbook/dashboards-access.md"})
    g = runtime.load_goal(r2)
    assert any(q["key"] == "approve_procedure" for q in g.questions)  # drafted (after one failed validation), waits for approval
    kinds = [e["data"].get("errors") for e in store.events(r2) if e["kind"] == "procedure.draft"]
    assert kinds[0] and kinds[-1] == []                               # first draft rejected by the validator, second accepted
    r3 = runtime.start(req, who, lambda k, q, o: "yes" if k == "confirm" else None, m,
                       {"sop": "handbook/dashboards-access.md", "approve_procedure": "yes", "drafted": "dashboards_access"})
    assert O.chat_user("kavya.reddy") and "dashboards" in O.chat_user("kavya.reddy")["channels"]
    assert O.mail_to("kavya.reddy@acme.test", "Dashboard access granted")
    import os
    os.remove("memory/procedures/dashboards_access.yaml")          # keep the repo's procedure set unchanged
