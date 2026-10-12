"""Procedure validation. (The model-drafted-procedure flow runs on the real model in eval case C9.)"""
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
    p = {"task_type": "t", "description": "t", "derive": [{"name": "username", "fn": "username_for"}],
         "steps": [{"id": "s", "description": "d", "delta": bad}]}
    assert any(msg in e for e in validate(p))


def test_match_lists_are_gone():
    p = {"task_type": "t", "description": "t", "match": ["onboard"], "steps": []}
    assert any("'match' lists are not allowed" in e for e in validate(p))


def test_placeholders_must_be_defined():
    p = {"task_type": "t", "description": "t", "steps": [{"id": "s", "description": "Add to #{nope}",
         "delta": {"app": "chat", "entity": "channel_member", "key": {"channel": "{nope}", "user": "{employee.chat_username}"}, "expect": {"exists": True}}}]}
    assert any("'{nope}' is never defined" in e for e in validate(p))


def test_a_fixed_time_in_a_drafted_procedure_is_rejected():
    # what the model drafted from the dashboards SOP in eval S2: both steps parked until a hard-coded time
    step = {"id": "join", "description": "Add to #dashboards", "not_before": "2026-10-08T09:00:00Z",
            "delta": {"app": "chat", "entity": "channel_member", "key": {"channel": "dashboards", "user": "{username}"}, "expect": {"exists": True}}}
    p = {"task_type": "t", "description": "t", "derive": [{"name": "username", "fn": "username_for"}], "steps": [step]}
    assert any("fixed time" in e for e in validate(p))
    step["not_before"] = "{employee.start_date}T09:00:00+05:30"
    assert not any("fixed time" in e for e in validate(p))
