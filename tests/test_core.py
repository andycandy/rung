"""Unit tests for deterministic core pieces (no sandbox needed)."""
from rung.promoter import Synth, is_noise, synthesize
from rung.types import Delta


def test_signature_ignores_free_text_but_not_targets():
    a = Delta("chat", "user", {"username": "a"}, {"exists": True, "display_name": "A"})
    b = Delta("chat", "user", {"username": "b"}, {"exists": True, "display_name": "B"})
    c = Delta("chat", "user", {"username": "b"}, {"active": False})
    d = Delta("chat", "user", {"username": "b"}, {"active": True})
    assert a.signature() == b.signature()
    assert c.signature() != d.signature()


def test_telemetry_is_noise_and_id_writes_are_not():
    vals = {"channel": "data", "user": "priya.sharma"}
    tel = {"method": "POST", "url": "/api/v1/telemetry", "body": {"path": "/people/priya.sharma"}, "status": 204}
    add = {"method": "POST", "url": "/api/v1/channels/4/members", "body": {"user_id": 9}, "status": 201}
    assert is_noise(tel, vals, list(vals))
    assert not is_noise(add, vals, list(vals))


def test_synthesis_resolves_ids_from_earlier_reads():
    rec = [
        {"method": "GET", "url": "/api/v1/users/priya.sharma", "status": 200, "body": None, "headers": {},
         "response": {"id": 9, "username": "priya.sharma"}},
        {"method": "GET", "url": "/api/v1/channels", "status": 200, "body": None, "headers": {},
         "response": [{"id": 4, "name": "data"}, {"id": 5, "name": "eng"}]},
        {"method": "POST", "url": "/api/v1/channels/4/members", "status": 201, "body": {"user_id": 9},
         "headers": {"x-csrf-token": "abc", "x-requested-with": "XMLHttpRequest"}, "response": {"ok": True}},
    ]
    code, meta = synthesize("sig", {"channel": "data", "user": "priya.sharma"}, ["channel", "user"], rec, {"csrf_token": "abc"})
    assert "http.cookie('csrf_token')" in code          # CSRF read fresh, never hard-coded
    assert "'abc'" not in code
    assert "_find(r.json(), 'name', v['channel'])" in code  # channel id resolved by name
    assert 'f"/api/v1/channels/{v[' in code                 # recorded ids replaced by resolved deps
    assert meta["params"] == ["channel", "user"]


def test_unresolvable_id_is_rejected():
    import pytest
    from rung.promoter import Unresolvable
    rec = [{"method": "POST", "url": "/api/v1/channels/4/members", "status": 201, "body": {"user_id": 9}, "headers": {}, "response": {}}]
    with pytest.raises(Unresolvable):
        synthesize("sig", {"channel": "data", "user": "x"}, ["channel", "user"], rec, {})
