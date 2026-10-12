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


def test_synthesized_lookup_miss_is_not_a_hard_drift():
    rec = [
        {"method": "GET", "url": "/api/v1/channels", "status": 200, "body": None, "headers": {},
         "response": [{"id": 4, "name": "data"}]},
        {"method": "POST", "url": "/api/v1/channels/4/members", "status": 201, "body": {"user": "priya.sharma"},
         "headers": {}, "response": {"ok": True}},
    ]
    code, _ = synthesize("sig", {"channel": "data", "user": "priya.sharma"}, ["channel", "user"], rec, {})
    ns = {}
    exec(compile(code, "learned", "exec"), ns)
    import pytest
    with pytest.raises(ns["Drift"]) as ei:
        ns["_find"]([{"name": "data"}], "name", "team channels")
    from rung.ladder import tool_broken
    assert not tool_broken(ei.value)   # a lookup miss can be bad input: a failed attempt, not a broken tool


def test_unresolvable_id_is_rejected():
    import pytest
    from rung.promoter import Unresolvable
    rec = [{"method": "POST", "url": "/api/v1/channels/4/members", "status": 201, "body": {"user_id": 9}, "headers": {}, "response": {}}]
    with pytest.raises(Unresolvable):
        synthesize("sig", {"channel": "data", "user": "x"}, ["channel", "user"], rec, {})


def _tool_classes():
    code, _ = synthesize("sig", {"channel": "data", "user": "priya.sharma"}, ["channel", "user"], [
        {"method": "GET", "url": "/api/v1/channels", "status": 200, "body": None, "headers": {}, "response": [{"id": 4, "name": "data"}]},
        {"method": "POST", "url": "/api/v1/channels/4/members", "status": 201, "body": {"user": "priya.sharma"}, "headers": {},
         "response": {"ok": True}}], {})
    ns = {}
    exec(compile(code, "learned", "exec"), ns)
    return ns


class _Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body, self.text, self.content = status, body or {}, str(body), b"x"
        self.request = type("R", (), {"method": "POST", "url": type("U", (), {"path": "/api/v1/channels/4/members"})()})()

    def json(self):
        return self._body


import pytest


@pytest.mark.parametrize("status, broken", [(409, False), (422, False), (400, False), (500, False),
                                            (404, True), (405, True), (410, True)])
def test_only_a_gone_endpoint_or_schema_drift_demotes(status, broken):
    from rung.ladder import tool_broken
    ns = _tool_classes()
    with pytest.raises(ns["Drift"]) as ei:
        ns["_check"](_Resp(status), 201)
    assert ei.value.status == status
    assert tool_broken(ei.value) is broken


def test_schema_drift_demotes():
    from rung.ladder import tool_broken
    ns = _tool_classes()
    with pytest.raises(ns["SchemaDrift"]) as ei:
        ns["_check"](_Resp(201, {"something": "else"}), 201, ["ok"])
    assert tool_broken(ei.value)


def test_ladder_keeps_a_tool_that_got_a_409(tmp_path, monkeypatch):
    """#dashboards archived -> the learned add-member tool gets a 409. That is a failed attempt
    (the step fails honestly), not evidence that the tool is broken: it must stay promoted."""
    from rung import ladder, store
    from rung.apps import registry
    from rung.types import Delta
    monkeypatch.setattr(store, "DB", tmp_path / "agent.db")

    class App:
        name, explorable = "chat", False
        def holds(self, ctx, delta): return False, None, "verified"
        def values(self, delta, args): return {**delta.key, **args}
        def candidates(self, ctx, delta, args): return []
        def http(self, ctx): return None
    monkeypatch.setattr(registry, "get", lambda name: App())

    class Ctx:
        stats = {"compiled_steps": 0}
        def log(self, kind, data=None): pass

    d = Delta("chat", "channel_member", {"channel": "dashboards", "user": "kabir.joshi"}, {"exists": True})
    for status, after in ((409, "promoted"), (422, "promoted"), (410, "demoted")):
        tool = tmp_path / f"tool_{status}.py"
        tool.write_text(f"class Drift(Exception):\n    status = None\n\n\n"
                        f"def run(http, args):\n    ex = Drift('POST -> {status}')\n    ex.status = {status}\n    raise ex\n")
        rid = store.add_rung("chat", d.signature(), "synthesized_api", tool, "promoted")
        res = ladder.ensure(Ctx(), d)
        assert not res.ok
        assert [r for r in store.all_rungs() if r["id"] == rid][0]["status"] == after
        store.update_rung(rid, status="rejected")


def test_navigation_paths_are_learned_as_templates_over_task_values():
    from rung.explore.explorer import fill_templates, path_templates
    t = path_templates(["/", "/people", "/people/priya.sharma?tab=1", "/people/priya.sharma"], {"user": "priya.sharma", "channel": "data"})
    assert t == ["/", "/people", "/people/{user}"]
    assert fill_templates(t, {"user": "zz-sandbox", "channel": "dashboards"}) == ["/", "/people", "/people/zz-sandbox"]
    assert fill_templates(["/c/{channel}/x"], {"user": "u"}) == []          # a value the new task doesn't have: no hint


def test_explorer_guard_only_lets_the_model_use_values_the_task_gave():
    from rung.explore.explorer import as_bool, guard
    from rung.llm.calls import Action
    from rung.types import Delta
    add = Delta("chat", "channel_member", {"channel": "data", "user": "zz-dbg"}, {"exists": True})
    vals = {"channel": "data", "user": "zz-dbg", "username": "zz-dbg"}
    sel = {"id": "5", "tag": "select", "options": ["#data", "#finance-private"]}
    assert guard(Action(action="fill", id="4", value="zz-dbg2"), {"type": "text"}, add, vals)       # invented: refused
    assert guard(Action(action="fill", id="4", value="zz-dbg"), {"type": "text"}, add, vals) is None
    assert guard(Action(action="fill", id="9", value="anything"), {"type": "password"}, add, vals) is None
    assert guard(Action(action="select", id="5", value="#data"), sel, add, vals) is None
    assert guard(Action(action="select", id="5", value="#finance-private"), sel, add, vals)       # not the goal's channel
    assert guard(Action(action="select", id="5", value="#eng"), sel, add, vals)                   # not an option
    assert guard(Action(action="check", id="7", value=True), {"type": "checkbox"}, add, vals)      # goal sets no boolean
    off = Delta("sso", "user", {"username": "zz-dbg"}, {"active": False})
    assert guard(Action(action="check", id="7", value="false"), {"type": "checkbox"}, off, {"username": "zz-dbg"}) is None
    assert as_bool("false") is False and as_bool("True") is True and as_bool("maybe") is None   # bool("false") is True
    assert guard(Action(action="goto", value="https://evil.example/"), None, add, vals)
