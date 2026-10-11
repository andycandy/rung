"""discover_interface: look for an existing, cheaper interface before touching a UI.

Cheap, read-only probes only: well-known OpenAPI locations for web apps,
`--help` output for desktop tools. Results are cached per app and logged.
Discovery itself can't mutate anything: GET requests and --help only.
"""
from __future__ import annotations

import json
import shutil
import subprocess

import httpx

from sandbox.common import VAR

CACHE = VAR / "discovery"
OPENAPI_PATHS = ["/openapi.json", "/swagger.json", "/api/openapi.json", "/swagger/v1/swagger.json", "/.well-known/openapi.json"]


def _cache(app: str):
    return CACHE / f"{app}.json"


def discover_http(app: str, base: str, refresh=False) -> dict:
    p = _cache(app)
    if p.exists() and not refresh:
        return json.loads(p.read_text())
    found = {"app": app, "openapi": None, "tried": []}
    for path in OPENAPI_PATHS:
        try:
            r = httpx.get(base + path, timeout=3)
            found["tried"].append(f"GET {path} -> {r.status_code}")
            if r.status_code == 200 and "json" in r.headers.get("content-type", "") and "paths" in r.json():
                found["openapi"] = r.json()
                found["openapi_path"] = path
                break
        except Exception as ex:  # noqa: BLE001
            found["tried"].append(f"GET {path} -> {type(ex).__name__}")
    if found["openapi"] or all("->  4" in t or " 404" in t for t in found["tried"]):
        CACHE.mkdir(parents=True, exist_ok=True)   # cache definitive answers only, never outages
        p.write_text(json.dumps(found))
    return found


def discover_cli(app: str, binaries: list[str], refresh=False) -> dict:
    p = _cache(app)
    if p.exists() and not refresh:
        return json.loads(p.read_text())
    found = {"app": app, "cli": None, "tried": []}
    for b in binaries:
        path = shutil.which(b)
        if not path:
            found["tried"].append(f"which {b} -> missing")
            continue
        try:
            out = subprocess.run([path, "--help"], capture_output=True, text=True, timeout=30)
            text = out.stdout + out.stderr
            found["tried"].append(f"{b} --help -> {len(text)} chars")
            found["cli"] = {"binary": path, "help": text[:6000],
                            "capabilities": sorted({c for c in ["--convert-to", "--headless", "--print-to-file", "--cat"] if c in text})}
            break
        except Exception as ex:  # noqa: BLE001
            found["tried"].append(f"{b} --help -> {type(ex).__name__}")
    CACHE.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(found))
    return found


class OpenAPIClient:
    """Generic executor for a documented API: maps a state delta onto an operation
    using the spec alone (x-entity hints or path naming)."""

    def __init__(self, spec: dict, base: str, token: str):
        self.spec, self.base, self.token = spec, base, token

    def _ops(self, entity: str):
        for path, item in self.spec["paths"].items():
            for method, op in item.items():
                ent = op.get("x-entity") or path.strip("/").split("/")[-1 if "{" not in path else -2].rstrip("s")
                if ent == entity:
                    kind = op.get("x-op") or {"post": "create", "patch": "update", "put": "update", "get": "read", "delete": "delete"}[method]
                    yield path, method, kind, op

    def op(self, entity, kind, item: bool | None = None):
        for path, method, k, op in self._ops(entity):
            if k == kind and (item is None or ("{" in path) == item):
                return path, method, op
        return None

    def call(self, method, path, **kw):
        r = httpx.request(method.upper(), self.base + path, headers={"Authorization": f"Bearer {self.token}"}, timeout=10, **kw)
        r.raise_for_status()
        return r.json() if r.content else {}

    def find(self, entity: str, key: dict):
        if "id" in key:
            p = self.op(entity, "read", item=True)
            try:
                return self.call(p[1], p[0].replace("{id}", str(key["id"])))
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    return None
                raise
        p = self.op(entity, "read", item=False)
        params = {"q": next(iter(key.values()))}
        for row in self.call(p[1], p[0], params=params):
            if all(str(row.get(k, "")).lower() == str(v).lower() for k, v in key.items()):
                return row
        return None

    def list(self, entity: str, **params):
        p = self.op(entity, "read", item=False)
        return self.call(p[1], p[0], params={k: v for k, v in params.items() if v is not None})

    def apply(self, entity: str, key: dict, expect: dict):
        fields = {k: v for k, v in expect.items() if k != "exists"}
        cur = self.find(entity, key)
        if cur is None:
            p = self.op(entity, "create")
            return self.call(p[1], p[0], json={**key, **fields})
        p = self.op(entity, "update", item=True)
        return self.call(p[1], p[0].replace("{id}", str(cur["id"])), json=fields)
