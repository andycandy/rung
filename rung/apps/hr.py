"""PeopleHub adapter. Ladder outcome: discovery finds the OpenAPI spec, so the
documented API is used directly -- no UI, no exploration, no synthesis."""
from __future__ import annotations

from rung.apps.base import App, Candidate
from rung.ctx import vault
from rung.discover import OpenAPIClient, discover_http
from rung.types import Delta
from sandbox.common import url


class HRApp(App):
    name = "hr"
    kind = "web"
    projection = {"employee": {"key": ["id"], "compare": ["status", "title", "team", "work_email", "chat_username", "start_date", "manager_id"],
                               "ignore": ["updated_at"]}}

    def client(self, ctx=None) -> OpenAPIClient:
        d = discover_http(self.name, url("hr"))
        if ctx is not None and not getattr(self, "_logged", False):
            ctx.log("discover", {"app": self.name, "found": "openapi" if d.get("openapi") else None, "tried": d["tried"]})
            self._logged = True
        if not d.get("openapi"):
            raise RuntimeError("no documented API found for hr")
        return OpenAPIClient(d["openapi"], url("hr"), vault()["hr"]["token"])

    def probe(self, ctx, entity, key):
        return self.client(ctx).find(entity, key), "verified"

    def search(self, ctx, **params):
        return self.client(ctx).list("employee", **params)

    def candidates(self, ctx, delta: Delta, args: dict):
        c = self.client(ctx)
        return [Candidate("documented_api", lambda _ctx: c.apply(delta.entity, delta.key, delta.expect))]
