"""Acme SSO adapter -- stock Django admin, a UI Rung's author did not write.

Ladder outcome: discovery finds no API spec; the admin is classic server-rendered
HTML with form posts (no JSON calls), so exploration works but the promoter
correctly declines to synthesize a tool ("no state-changing JSON request captured").
This app stays on the UI rung by design -- the honest result for form-post apps.
"""
from __future__ import annotations

from rung.apps.base import App
from rung.ctx import vault
from rung.explore.explorer import explore_with_model
from rung.types import Delta, ExecResult
from sandbox.common import DOMAIN, url


def ui_login(ctx, ab, path: str, creds: dict, app: str):
    """Generic login, by form structure: the password input, and the text/e-mail input in the same form."""
    ab.goto(path)
    snap = ab.snapshot()
    pwd = next(e for e in snap["elements"] if e["tag"] == "input" and e.get("type") == "password")
    form = pwd.get("form")
    user = next(e for e in snap["elements"] if e["tag"] == "input" and e.get("form") == form and e.get("type") in ("text", "email"))
    ab.fill(user["id"], creds["username"])
    ab.fill(pwd["id"], creds["password"])
    btn = next(e for e in snap["elements"] if e.get("form") == form and (e["tag"] == "button" or e.get("type") == "submit"))
    ab.click(btn["id"])
    ab._logged_in = True
    ctx.log("auth.login", {"app": app, "via": "ui", "identity": creds["username"]})


class SSOApp(App):
    name = "sso"
    kind = "web"
    explorable = True
    HOME = "/admin/"
    projection = {"user": {"key": ["username"], "compare": ["email", "first_name", "last_name", "active"], "ignore": ["last_login"]}}

    def ab(self, ctx):
        ab = ctx.get_browser().app(self.name, url(self.name))
        if not getattr(ab, "_logged_in", False):
            ui_login(ctx, ab, "/admin/login/", vault()["sso"], self.name)
        return ab

    def relogin(self, ctx):
        ab = ctx.get_browser().app(self.name, url(self.name))
        ui_login(ctx, ab, "/admin/login/", vault()["sso"], self.name)

    def probe(self, ctx, entity, key):
        """Read through the admin's own search + change page (hand-written probe for this app)."""
        ab = self.ab(ctx)
        pg = ab.get_probe_page()
        pg.goto(ab.base + f"/admin/auth/user/?q={key['username']}", wait_until="load")
        if "/login" in pg.url:
            self.relogin(ctx)
            return self.probe(ctx, entity, key)
        href = pg.evaluate("(u) => { const a=[...document.querySelectorAll('#result_list a, table a')].find(a => a.innerText.trim()===u);"
                           " return a && a.getAttribute('href'); }", key["username"])
        if not href:
            return None, "verified"
        pg.goto(ab.base + href if href.startswith("/") else ab.base + "/admin/auth/user/" + href, wait_until="load")
        st = pg.evaluate("() => { const v=n=>{const e=document.querySelector(`[name=${n}]`); return e ? (e.type==='checkbox'? e.checked : e.value) : null;};"
                         " return {username: v('username'), email: v('email'), first_name: v('first_name'), last_name: v('last_name'), active: v('is_active')}; }")
        return (st if st.get("username") else None), "verified"

    def values(self, delta, args):
        return {**delta.key, **{k: v for k, v in delta.expect.items() if k != "exists"}, **args}

    def explore(self, ctx, delta: Delta, args: dict) -> ExecResult:
        return explore_with_model(ctx, self, delta, self.values(delta, args), shot_prefix=f"sso-{delta.entity}")

    def inverse(self, delta, before):
        if delta.expect.get("exists") is True:
            return Delta(self.name, "user", delta.key, {"active": False})
        if "active" in delta.expect:
            return Delta(self.name, "user", delta.key, {"active": not delta.expect["active"]})
        return super().inverse(delta, before)

    def test_args(self, ctx, delta, values):
        return None   # no API tool is ever synthesized for this app, so nothing to sandbox-test

    def http(self, ctx):
        raise RuntimeError("SSO has no API; it is operated through its UI only")
