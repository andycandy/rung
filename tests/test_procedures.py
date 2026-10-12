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
