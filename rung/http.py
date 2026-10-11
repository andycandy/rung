"""Authenticated HTTP session handle given to learned (synthesized) tools.

Tools never see credentials: they get this handle, which carries the cookie jar
established by a UI login and exposes cookie values (e.g. a CSRF token) by name
so templates can read them fresh on every call.
"""
from __future__ import annotations

import json

import httpx

from sandbox.common import VAR

SESS = VAR / "sessions"


class AuthExpired(Exception):
    pass


class EndpointGone(Exception):
    """The learned endpoint no longer exists (404/405/410) -- a demotion signal."""


class SchemaDrift(Exception):
    pass


class CookieHTTP:
    def __init__(self, app: str, base: str):
        self.app, self.base = app, base
        p = SESS / f"{app}.json"
        cookies = json.loads(p.read_text()) if p.exists() else {}
        self.client = httpx.Client(base_url=base, cookies=cookies, timeout=10, follow_redirects=False)

    @staticmethod
    def save(app: str, cookies: dict):
        SESS.mkdir(parents=True, exist_ok=True)
        (SESS / f"{app}.json").write_text(json.dumps(cookies))

    @staticmethod
    def has(app: str) -> bool:
        return (SESS / f"{app}.json").exists()

    def cookie(self, name: str) -> str:
        return self.client.cookies.get(name, "")

    def request(self, method: str, path: str, **kw) -> httpx.Response:
        r = self.client.request(method, path, **kw)
        if r.status_code == 401 or (r.status_code in (301, 302, 303) and "/login" in r.headers.get("location", "")):
            raise AuthExpired(f"{method} {path} -> {r.status_code}")
        if r.status_code in (404, 405, 410) and method != "GET":
            raise EndpointGone(f"{method} {path} -> {r.status_code}")
        if r.status_code == 410:
            raise EndpointGone(f"{method} {path} -> 410")
        return r

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def close(self):
        self.client.close()
