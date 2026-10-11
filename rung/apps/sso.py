"""Acme SSO adapter -- stock Django admin, a UI Rung's author did not write.

Ladder outcome: discovery finds no API spec; the admin is classic server-rendered
HTML with form posts (no JSON calls), so exploration works but the promoter
correctly declines to synthesize a tool ("no state-changing JSON request captured").
This app stays on the UI rung by design -- the honest result for form-post apps.
"""
from __future__ import annotations

import secrets
import time

from rung.apps.base import App
from rung.ctx import vault
from rung.explore.explorer import HeuristicExplorer, LLMExplorer
from rung.types import Delta, ExecResult
from sandbox.common import DOMAIN, url


def ui_login(ctx, ab, path: str, creds: dict, app: str):
    """Generic login: fill the password field and the user-ish field, submit the form."""
    ab.goto(path)
    snap = ab.snapshot()
    form = None
    for e in snap["elements"]:
        lab = (e.get("label") or "").lower()
        if e["tag"] == "input" and e.get("type") == "password":
            ab.fill(e["id"], creds["password"])
            form = e.get("form")
        elif e["tag"] == "input" and ("user" in lab or "login" in lab or "email" in lab):
            ab.fill(e["id"], creds["username"])
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
        ab = self.ab(ctx)
        explorer = LLMExplorer(ctx.model) if ctx.model else HeuristicExplorer()
        check = lambda: self.holds(ctx, delta)[0]  # noqa: E731
        tag = f"explore-{secrets.token_hex(3)}"
        ab.start_recording(tag)
        t0 = time.time()
        try:
            tr = explorer.run(ab, delta, self.values(delta, args), check=check, start=self.HOME)
        except Exception as ex:  # noqa: BLE001
            ctx.log("llm.error", {"call": "explore_step", "error": str(ex)})
            explorer = HeuristicExplorer()
            tr = explorer.run(ab, delta, self.values(delta, args), check=check, start=self.HOME)
        if tr.gave_up and isinstance(explorer, LLMExplorer):
            ctx.log("explore.fallback", {"from": explorer.name, "reason": tr.gave_up})
            ctx.stats["llm_calls"] += tr.llm_calls
            explorer = HeuristicExplorer()
            tr = explorer.run(ab, delta, self.values(delta, args), check=check, start=self.HOME)
        rec = ab.stop_recording()
        shot = ab.screenshot(f"sso-{delta.entity}-{tag}")
        ctx.stats["explore_steps"] += 1
        ctx.stats["llm_calls"] += tr.llm_calls
        return ExecResult(ok=not tr.gave_up, rung=explorer.name, detail=tr.gave_up or f"{len(tr.actions)} UI actions via {tr.pages}",
                          ms=int((time.time() - t0) * 1000), llm_calls=tr.llm_calls, recording=rec,
                          data={"actions": tr.actions, "pages": tr.pages, "screenshot": shot})

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
