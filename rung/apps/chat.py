"""Chatter adapter. The agent has UI credentials only: no API docs, no token.

Ladder outcome: discovery finds no spec, so the first run explores the UI while
traffic is captured; after verified success the promoter synthesizes a direct
HTTP tool. Probes climb too: the DOM probe's own traffic yields a learned read
endpoint, which is used afterwards (and dropped back to DOM if it breaks).
"""
from __future__ import annotations

import secrets
import time

from rung import store
from rung.apps.base import App
from rung.ctx import vault
from rung.discover import discover_http
from rung.explore.explorer import HeuristicExplorer, LLMExplorer
from rung.http import AuthExpired, CookieHTTP, EndpointGone
from rung.types import Delta, ExecResult
from sandbox.common import DOMAIN, url


class ChatApp(App):
    name = "chat"
    kind = "web"
    explorable = True
    projection = {
        "user": {"key": ["username"], "compare": ["display_name", "email", "title", "role", "active"],
                 "ignore": ["last_seen_at", "created_at", "id"]},
        "channel_member": {"key": ["channel", "user"], "compare": ["exists"]},
    }
    FIXTURE = {"username": "zz-sandbox", "display_name": "ZZ Sandbox", "email": f"zz-sandbox@{DOMAIN}", "title": "Sandbox fixture"}

    # ---------------------------------------------------------------- sessions
    def ab(self, ctx):
        ab = ctx.get_browser().app(self.name, url(self.name))
        if not getattr(ab, "_logged_in", False):
            self.login(ctx, ab)
        return ab

    def login(self, ctx, ab):
        """Generic login: find the username/password fields by label and submit."""
        creds = vault()["chat"]
        ab.goto("/login")
        snap = ab.snapshot()
        for e in snap["elements"]:
            lab = (e.get("label") or "").lower()
            if e["tag"] == "input" and e.get("type") == "password":
                ab.fill(e["id"], creds["password"])
            elif e["tag"] == "input" and "user" in lab:
                ab.fill(e["id"], creds["username"])
        btn = next(e for e in snap["elements"] if e["tag"] == "button")
        ab.click(btn["id"])
        ab._logged_in = True
        CookieHTTP.save(self.name, ab.cookies())
        ctx.log("auth.login", {"app": self.name, "via": "ui", "identity": creds["username"]})

    def http(self, ctx):
        if not CookieHTTP.has(self.name):
            self.ab(ctx)
        return CookieHTTP(self.name, url(self.name))

    def relogin(self, ctx):
        ab = ctx.get_browser().app(self.name, url(self.name))
        self.login(ctx, ab)

    # ---------------------------------------------------------------- probes
    def probe(self, ctx, entity, key):
        if entity == "user":
            return self.probe_user(ctx, key["username"]), "verified"
        if entity == "channel_member":
            u = self.probe_user(ctx, key["user"])
            ch = key["channel"].lstrip("#").lower()
            return ({"exists": True} if u and ch in [c.lower() for c in u.get("channels", [])] else None), "verified"
        raise KeyError(entity)

    def probe_user(self, ctx, username):
        from rung.promoter import load_tool
        for r in store.rungs_for(self.name, "probe:user"):
            t0 = time.time()
            try:
                out = load_tool(r["path"]).run(self.http(ctx), {"username": username})
                store.record_rung_use(r["id"], True, int((time.time() - t0) * 1000))
                return self._norm(out) if out else None
            except AuthExpired:
                self.relogin(ctx)
                return self.probe_user(ctx, username)
            except Exception as ex:  # noqa: BLE001  -- learned probe broke: fall back to DOM, demote
                store.update_rung(r["id"], status="demoted", note=f"probe failed: {ex}")
                ctx.log("rung.demoted", {"rung": r["id"], "reason": str(ex)})
        return self.dom_probe_user(ctx, username)

    @staticmethod
    def _norm(u: dict) -> dict:
        return {k: u.get(k) for k in ("username", "display_name", "email", "title", "role", "active", "channels")}

    def dom_probe_user(self, ctx, username):
        ab = self.ab(ctx)
        ab.start_recording("probe")
        ab.goto(f"/people/{username}")
        try:  # the page is an SPA: wait until the record rendered (or an error showed)
            ab.page.wait_for_function("() => { const f=document.getElementById('flash'); const dds=[...document.querySelectorAll('dd')];"
                                      " return (f && f.innerText) || (dds.length && dds.some(d => /Active|Deactivated/.test(d.innerText))); }", timeout=5000)
        except Exception:  # noqa: BLE001
            pass
        state = ab.page.evaluate(r"""() => {
            const alert=(document.getElementById('flash')||{}).innerText||'';
            if (/404/.test(alert)) return null;
            const out = {}; const dts=[...document.querySelectorAll('dt')];
            dts.forEach(dt => { const dd=dt.nextElementSibling; if(dd) out[dt.innerText.trim().toLowerCase()] = dd.innerText.trim(); });
            out.channels=[...document.querySelectorAll('main ul li')].map(li => (li.innerText.match(/#([\w-]+)/)||[])[1]).filter(Boolean);
            return out; }""")
        rec = ab.stop_recording()
        if state is None:
            return None
        if "/login" in ab.page.url:
            self.login(ctx, ab)
            return self.dom_probe_user(ctx, username)
        out = {"username": username, "display_name": state.get("name"), "title": state.get("title"),
               "role": state.get("role"), "active": state.get("status") == "Active", "channels": state.get("channels", []),
               "email": state.get("email")}
        if not state.get("status"):
            return None
        # probes climb the ladder too: learn a read endpoint from what the page fetched
        from rung.promoter import learn_probe
        learn_probe(ctx, self, "probe:user", {"username": username}, rec, out)
        return out

    # ---------------------------------------------------------------- explore
    def values(self, delta: Delta, args: dict) -> dict:
        v = {**delta.key, **{k: x for k, x in delta.expect.items() if k != "exists"}, **args}
        if delta.entity == "channel_member":
            v.setdefault("username", v.get("user"))
        return v

    def explore(self, ctx, delta: Delta, args: dict) -> ExecResult:
        ab = self.ab(ctx)
        explorer = LLMExplorer(ctx.model) if ctx.model else HeuristicExplorer()
        tag = f"explore-{secrets.token_hex(3)}"
        ab.start_recording(tag)
        t0 = time.time()
        try:
            tr = explorer.run(ab, delta, self.values(delta, args))
        except Exception as ex:  # noqa: BLE001 -- model unavailable/invalid: fall back to the offline policy
            ctx.log("llm.error", {"call": "explore_step", "error": str(ex)})
            explorer = HeuristicExplorer()
            tr = explorer.run(ab, delta, self.values(delta, args))
        if tr.gave_up and isinstance(explorer, LLMExplorer):
            ctx.log("explore.fallback", {"from": explorer.name, "reason": tr.gave_up})
            ctx.stats["llm_calls"] += tr.llm_calls
            explorer = HeuristicExplorer()
            tr = explorer.run(ab, delta, self.values(delta, args))
        rec = ab.stop_recording()
        shot = ab.screenshot(f"{delta.entity}-{tag}")
        ctx.stats["explore_steps"] += 1
        ctx.stats["llm_calls"] += tr.llm_calls
        return ExecResult(ok=not tr.gave_up, rung=explorer.name, detail=tr.gave_up or f"{len(tr.actions)} UI actions via {tr.pages}",
                          ms=int((time.time() - t0) * 1000), llm_calls=tr.llm_calls, recording=rec,
                          data={"actions": tr.actions, "pages": tr.pages, "screenshot": shot})

    # ---------------------------------------------------------------- lifecycle helpers
    def inverse(self, delta, before):
        if delta.entity == "user":
            if delta.expect.get("exists") is True:
                return Delta(self.name, "user", delta.key, {"active": False})
            if "active" in delta.expect:
                return Delta(self.name, "user", delta.key, {"active": not delta.expect["active"]})
            if "role" in delta.expect:
                return Delta(self.name, "user", delta.key, {"role": (before or {}).get("role", "member")})
        return super().inverse(delta, before)

    def test_args(self, ctx, delta: Delta, values: dict):
        if delta.entity == "user" and delta.expect.get("exists") is True:
            h = secrets.token_hex(3)
            v = {"username": f"zz-test-{h}", "display_name": f"ZZ Test {h}", "email": f"zz-test-{h}@{DOMAIN}", "title": "Sandbox test"}
            return Delta(self.name, "user", {"username": v["username"]},
                         {"exists": True, "display_name": v["display_name"], "email": v["email"], "title": v["title"]}), v
        if delta.entity == "channel_member":
            v = {**values, "user": self.FIXTURE["username"], "username": self.FIXTURE["username"],
                 "display_name": self.FIXTURE["display_name"]}
            return Delta(self.name, "channel_member", {"channel": values["channel"], "user": v["user"]}, dict(delta.expect)), v
        if delta.entity == "user" and "active" in delta.expect:
            v = dict(self.FIXTURE)
            return Delta(self.name, "user", {"username": v["username"]}, dict(delta.expect)), v
        return None

    def fixture_delta(self):
        f = self.FIXTURE
        return Delta(self.name, "user", {"username": f["username"]},
                     {"exists": True, "display_name": f["display_name"], "email": f["email"], "title": f["title"]})

    def discovery(self, ctx):
        d = discover_http(self.name, url(self.name))
        return d
