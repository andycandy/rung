"""Chatter adapter. The agent has UI credentials only: no API docs, no token.

Ladder outcome: discovery finds no spec, so the first run explores the UI while
traffic is captured; after verified success the promoter synthesizes a direct
HTTP tool. Probes climb too: the DOM probe's own traffic yields a learned read
endpoint, which is used afterwards (and dropped back to DOM if it breaks).
"""
from __future__ import annotations

import secrets
import time
import urllib.parse

from rung import store
from rung.apps.base import App
from rung.ctx import vault
from rung.discover import discover_http
from rung.explore.explorer import explore_with_model
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
    HOME = "/"
    FIXTURE = {"username": "zz-sandbox", "display_name": "ZZ Sandbox", "email": f"zz-sandbox@{DOMAIN}", "title": "Sandbox fixture"}

    # ---------------------------------------------------------------- sessions
    def ab(self, ctx):
        ab = ctx.get_browser().app(self.name, url(self.name))
        if not getattr(ab, "_logged_in", False):
            self.login(ctx, ab)
        return ab

    def login(self, ctx, ab):
        from rung.apps.sso import ui_login
        ui_login(ctx, ab, "/login", vault()["chat"], self.name)
        CookieHTTP.save(self.name, ab.cookies())

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
        pg = ab.get_probe_page()     # probes use their own tab, so they never disturb an exploration in progress
        ab.start_recording("probe", pg)
        pg.goto(ab.base + f"/people/{username}", wait_until="networkidle")
        try:  # the page is an SPA: wait until the record rendered (or an error showed)
            pg.wait_for_function("() => { const f=document.getElementById('flash'); const dds=[...document.querySelectorAll('dd')];"
                                      " return (f && f.innerText) || (dds.length && dds.some(d => /Active|Deactivated/.test(d.innerText))); }", timeout=5000)
        except Exception:  # noqa: BLE001
            pass
        state = pg.evaluate(r"""() => {
            const alert=(document.getElementById('flash')||{}).innerText||'';
            if (/404/.test(alert)) return null;
            const out = {}; const dts=[...document.querySelectorAll('dt')];
            dts.forEach(dt => { const dd=dt.nextElementSibling; if(dd) out[dt.innerText.trim().toLowerCase()] = dd.innerText.trim(); });
            out.channels=[...document.querySelectorAll('main ul li')].map(li => (li.innerText.match(/#([\w-]+)/)||[])[1]).filter(Boolean);
            return out; }""")
        rec = ab.stop_recording(pg)
        if "/login" in pg.url:
            self.login(ctx, ab)
            return self.dom_probe_user(ctx, username)
        if state is None:
            return None
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
        return explore_with_model(ctx, self, delta, self.values(delta, args), shot_prefix=delta.entity)

    # ---------------------------------------------------------------- workspace reads (evidence + validation)
    def channels(self, ctx) -> list[str]:
        """Channel names as Chatter lists them, read from the link paths (/c/<name>) on the home page."""
        ab = self.ab(ctx)
        pg = ab.get_probe_page()
        pg.goto(ab.base + "/", wait_until="load")
        if "/login" in pg.url:
            self.login(ctx, ab)
            return self.channels(ctx)
        hrefs = pg.evaluate("() => [...document.querySelectorAll('a[href]')].map(a => a.getAttribute('href'))")
        return sorted({urllib.parse.unquote(h[3:]) for h in hrefs if h.startswith("/c/") and len(h) > 3})

    def recent_messages(self, ctx, per_channel: int = 10) -> list[dict]:
        """The latest messages of every channel, as posted (author, date, text)."""
        ab = self.ab(ctx)
        pg = ab.get_probe_page()
        out = []
        for name in self.channels(ctx):
            pg.goto(ab.base + "/c/" + urllib.parse.quote(name), wait_until="load")
            msgs = pg.evaluate("() => [...document.querySelectorAll('article')].map(a => ({author: (a.querySelector('b')||{}).innerText||'',"
                               " date: (a.querySelector('time')||{}).innerText||'', text: (a.querySelector('p')||{}).innerText||''}))")
            out += [{"channel": name, **m} for m in msgs[-per_channel:]]
        return out

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
            h = next(n for n in range(1, 10_000) if self.probe_user(ctx, f"zz-test-{n}") is None)  # deterministic: replays match
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
