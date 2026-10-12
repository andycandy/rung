"""The SSO admin's stylesheet is presentation only: on every admin page the agent uses, what the
explorer reads (main text, interactive elements, labels, visibility) is identical with and without it."""
import pytest


@pytest.mark.sandbox
def test_sso_skin_does_not_change_what_the_agent_reads():
    from rung.apps import registry
    from rung.ctx import Ctx
    from rung.explore.browser import SNAPSHOT_JS
    ctx = Ctx("skin-check")
    try:
        ab = registry.get("sso").ab(ctx)
        pages = ["/admin/", "/admin/auth/user/", "/admin/auth/user/?q=it-admin", "/admin/auth/user/add/",
                 "/admin/auth/user/2/change/", "/admin/auth/group/", "/admin/password_change/"]
        for path in pages:
            ab.goto(path)
            assert ab.page.locator("#acme-skin").count() == 1, path
            styled = ab.page.evaluate(SNAPSHOT_JS)
            ab.page.evaluate("() => document.getElementById('acme-skin').remove()")
            assert ab.page.evaluate(SNAPSHOT_JS) == styled, path
        ab.page.context.clear_cookies()
        ab.goto("/admin/login/")
        styled = ab.page.evaluate(SNAPSHOT_JS)
        ab.page.evaluate("() => document.getElementById('acme-skin').remove()")
        assert ab.page.evaluate(SNAPSHOT_JS) == styled
    finally:
        ctx.close()
