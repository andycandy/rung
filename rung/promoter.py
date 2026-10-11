"""Promoter: turn a verified UI success into a direct API tool -- and only trust it
once it has proven itself.

Lifecycle of a synthesized rung:
  synthesize (from captured traffic)  ->  lint  ->  sandbox test on a disposable
  entity (do -> verify -> undo -> verify)  ->  promoted
  ... later: verified failures / endpoint gone  ->  demoted (falls back to explore,
  which re-records and re-synthesizes a new version).

Tools that could not be sandbox-tested start on *probation*: they are used with a
verified fallback and promoted after PROBATION_WINS verified real uses.
"""
from __future__ import annotations

import importlib.util
import json
import re
import urllib.parse
from pathlib import Path

from rung import store
from rung.types import Delta
from sandbox.common import VAR

LEARNED = VAR / "learned"
WRITE = {"POST", "PUT", "PATCH", "DELETE"}
NOISE_URL = re.compile(r"telemetry|analytics|metrics|track|beacon|log", re.I)
SENSITIVE = re.compile(r"role|permission|password|admin|scope", re.I)
PROBATION_WINS = 2


# ------------------------------------------------------------------ helpers
def _eq(a, b) -> bool:
    return a is not None and b is not None and str(a).strip().lower() == str(b).strip().lower()


def _match_arg(v, values: dict, order: list[str]):
    for k in order:
        if k in values and not isinstance(values[k], (dict, list)) and _eq(v, values[k]):
            return k
    return None


def _objects(x):
    if isinstance(x, dict):
        yield x
        for v in x.values():
            yield from _objects(v)
    elif isinstance(x, list):
        for v in x:
            yield from _objects(v)


class Unresolvable(Exception):
    pass


class Synth:
    """Builds the code for one synthesized tool from a recording."""

    def __init__(self, values: dict, order: list[str], rec: list[dict], cookies: dict):
        self.values, self.order, self.rec, self.cookies = values, order, rec, cookies
        self.lines: list[str] = []
        self.deps = 0
        self.params: set[str] = set()

    def resolver(self, ident, before: list[dict]) -> str:
        """Find the earlier read whose response introduced `ident`, keyed by an argument value."""
        for r in reversed(before):
            if r["method"] != "GET" or r["status"] != 200 or r["response"] is None:
                continue
            for obj in _objects(r["response"]):
                id_field = next((k for k, v in obj.items() if k.endswith("id") and _eq(v, ident)), None)
                if not id_field:
                    continue
                for f, fv in obj.items():
                    arg = _match_arg(fv, self.values, self.order)
                    if arg and f != id_field:
                        url_expr = self.url_expr(r["url"], [])
                        name = f"_dep{self.deps}"
                        self.deps += 1
                        self.params.add(arg)
                        self.lines += [f"    r = http.get({url_expr})",
                                       f"    _check(r, 200)",
                                       f"    v[{name!r}] = _find(r.json(), {f!r}, v[{arg!r}])[{id_field!r}]"]
                        return name
        raise Unresolvable(f"value {ident!r} has no source in recorded reads")

    def url_expr(self, url: str, before) -> str:
        parsed = urllib.parse.urlsplit(url)
        segs = []
        for s in parsed.path.split("/"):
            arg = _match_arg(urllib.parse.unquote(s), self.values, self.order)
            if arg:
                self.params.add(arg)
                segs.append("{_q(v[%r])}" % arg)
            elif s.isdigit() and before is not None:
                segs.append("{v[%r]}" % self.resolver(int(s), before))
            else:
                segs.append(s.replace("{", "{{").replace("}", "}}"))
        q = urllib.parse.parse_qsl(parsed.query)
        qs = ""
        if q:
            parts = []
            for k, val in q:
                arg = _match_arg(val, self.values, self.order)
                if arg:
                    self.params.add(arg)
                parts.append(f"{k}=" + ("{_q(v[%r])}" % arg if arg else urllib.parse.quote(val)))
            qs = "?" + "&".join(parts)
        return 'f"' + "/".join(segs) + qs + '"'

    def body_expr(self, body, before):
        if body is None:
            return None
        if not isinstance(body, dict):
            raise Unresolvable("non-JSON body")
        out = []
        for k, val in body.items():
            arg = _match_arg(val, self.values, self.order)
            if arg:
                self.params.add(arg)
                out.append(f"{k!r}: v[{arg!r}]")
            elif isinstance(val, int) and not isinstance(val, bool):
                out.append(f"{k!r}: v[{self.resolver(val, before)!r}]")
            else:
                out.append(f"{k!r}: {val!r}")
        return "{" + ", ".join(out) + "}"

    def headers_expr(self, headers: dict) -> str:
        out = []
        for k, val in headers.items():
            lk = k.lower()
            cookie = next((c for c, cv in self.cookies.items() if cv and cv == val), None)
            if cookie:
                out.append(f"{k!r}: http.cookie({cookie!r})")
            elif lk in ("x-requested-with", "content-type"):
                out.append(f"{k!r}: {val!r}")
        return "{" + ", ".join(out) + "}"


def _leaves(x):
    if isinstance(x, dict):
        for v in x.values():
            yield from _leaves(v)
    elif isinstance(x, list):
        for v in x:
            yield from _leaves(v)
    else:
        yield x


def is_noise(r: dict, values: dict, order) -> bool:
    """Telemetry/beacons and other writes unrelated to the task are excluded from the write set.
    A write is relevant if a body value or URL segment *exactly* equals a task value, or it
    references ids (resolved later); beacon-style URLs and 204-with-no-match are noise."""
    if NOISE_URL.search(r["url"].split("?")[0]):
        return True
    vals = [values[k] for k in order if isinstance(values.get(k), (str, int)) and not isinstance(values.get(k), bool)]
    leaves = list(_leaves(r.get("body"))) + [urllib.parse.unquote(s) for s in r["url"].split("?")[0].split("/")]
    if any(_eq(l, v) for l in leaves for v in vals):
        return False
    has_ids = any(isinstance(l, int) and not isinstance(l, bool) for l in _leaves(r.get("body"))) or \
        any(s.isdigit() for s in r["url"].split("?")[0].split("/"))
    return not has_ids


def synthesize(signature: str, values: dict, order: list[str], rec: list[dict], cookies: dict) -> tuple[str, dict]:
    writes = [i for i, r in enumerate(rec) if r["method"] in WRITE and r["status"] < 400 and not is_noise(r, values, order)]
    if not writes:
        raise Unresolvable("no state-changing JSON request captured")
    s = Synth(values, order, rec, cookies)
    expects, sensitive = [], []
    for i in writes:
        w = rec[i]
        before = rec[:i]
        u = s.url_expr(w["url"], before)
        b = s.body_expr(w["body"], before)
        h = s.headers_expr(w["headers"])
        if isinstance(w["body"], dict):
            sensitive += [k for k in w["body"] if SENSITIVE.search(k)]
        keys = sorted(w["response"].keys()) if isinstance(w["response"], dict) else []
        s.lines += [f"    r = http.request({w['method']!r}, {u}" + (f", json={b}" if b else "") + f", headers={h})",
                    f"    _check(r, {w['status']}, {keys!r})"]
        expects.append({"method": w["method"], "url": w["url"], "status": w["status"]})
    s.lines.append("    return r.json() if r.content else {}")
    meta = {"signature": signature, "params": sorted(s.params), "writes": expects, "sensitive": sensitive}
    code = TEMPLATE.format(meta=json.dumps(meta, indent=2), body="\n".join(s.lines))
    return code, meta


TEMPLATE = '''"""AUTO-GENERATED by rung.promoter from captured UI traffic. Do not edit by hand."""
import urllib.parse

META = {meta}


class Drift(Exception):
    pass


def _q(x):
    return urllib.parse.quote(str(x), safe="")


def _check(r, status, keys=()):
    if r.status_code != status:
        raise Drift(f"{{r.request.method}} {{r.request.url.path}} -> {{r.status_code}} (expected {{status}}): {{r.text[:200]}}")
    if keys:
        got = r.json()
        missing = [k for k in keys if k not in got]
        if missing:
            raise Drift(f"response schema drift: missing {{missing}}")


def _find(data, field, value):
    items = data if isinstance(data, list) else [data]
    for it in items:
        if str(it.get(field, "")).lower() == str(value).lower():
            return it
    raise Drift(f"no item with {{field}}={{value!r}}")


def run(http, args):
    v = dict(args)
{body}
'''

PROBE_TEMPLATE = '''"""AUTO-GENERATED read probe (learned from the DOM probe's own traffic)."""
import urllib.parse

META = {meta}


def _q(x):
    return urllib.parse.quote(str(x), safe="")


def run(http, args):
    v = dict(args)
    r = http.get({url})
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise RuntimeError(f"probe endpoint returned {{r.status_code}}")
    return r.json()
'''


def load_tool(path: str):
    spec = importlib.util.spec_from_file_location(Path(path).stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write(app: str, name: str, code: str) -> Path:
    d = LEARNED / app
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.py"
    p.write_text(code)
    return p


# ------------------------------------------------------------------ public API
def learn(ctx, app, delta: Delta, values: dict, rec: list[dict], depth: int = 0):
    """Called by the ladder after an explore step was *verified*."""
    from rung import ladder

    sig = delta.signature()
    if store.rungs_for(app.name, sig, statuses=("promoted", "candidate")):
        return None
    order = list(delta.key) + [k for k in delta.expect if k != "exists"] + [k for k in values if k not in delta.key]
    try:
        cookies = ctx.get_browser().apps[app.name].cookies()
        code, meta = synthesize(sig, values, order, rec, cookies)
    except Unresolvable as ex:
        ctx.log("promoter.skip", {"signature": sig, "reason": str(ex)})
        return None
    path = _write(app.name, f"{delta.entity}_{delta.sig_id()}_{len(store.rungs_for(app.name, sig, statuses=('promoted', 'candidate', 'demoted', 'rejected'))) + 1}", code)
    rid = store.add_rung(app.name, sig, "synthesized_api", path, "testing", note=json.dumps(meta["writes"]))
    ctx.log("promoter.synthesized", {"rung": rid, "signature": sig, "path": str(path), "meta": meta})
    if meta["sensitive"]:
        store.update_rung(rid, status="needs_approval", note=f"touches sensitive fields {meta['sensitive']}")
        ctx.log("promoter.held", {"rung": rid, "reason": "sensitive fields; human approval required to promote"})
        return rid
    if depth > 0:
        store.update_rung(rid, status="candidate", note="probation: learned during a sandbox test")
        return rid

    # ---- sandbox test: do -> verify -> undo -> verify on a disposable entity
    t = app.test_args(ctx, delta, values)
    if not t:
        store.update_rung(rid, status="candidate", note="probation: no sandbox variant for this delta")
        ctx.log("promoter.probation", {"rung": rid})
        return rid
    tdelta, tvals = t
    log = []
    try:
        if delta.entity == "channel_member" or "active" in delta.expect:
            ladder.ensure(ctx, app.fixture_delta(), depth=depth + 1)
            log.append("fixture ok")
        pre, before, _ = app.holds(ctx, tdelta)
        if pre:
            inv0 = app.inverse(tdelta, before)
            ladder.ensure(ctx, inv0, depth=depth + 1)
            pre, before, _ = app.holds(ctx, tdelta)
        load_tool(str(path)).run(app.http(ctx), {**tvals, **tdelta.key})
        ok, after, _ = app.holds(ctx, tdelta)
        log.append(f"do -> holds={ok}")
        if not ok:
            raise RuntimeError("tool ran but the expected state was not observed")
        inv = app.inverse(tdelta, before)
        if inv:
            ladder.ensure(ctx, inv, depth=depth + 1)
            undone, _, _ = app.holds(ctx, inv)
            log.append(f"undo -> holds={undone}")
            if not undone:
                raise RuntimeError("could not undo the sandbox change")
        store.update_rung(rid, status="promoted", note=json.dumps({"sandbox_test": log}))
        ctx.log("promoter.promoted", {"rung": rid, "signature": sig, "sandbox_test": log})
    except Exception as ex:  # noqa: BLE001
        store.update_rung(rid, status="rejected", note=f"sandbox test failed: {ex}; {log}")
        ctx.log("promoter.rejected", {"rung": rid, "reason": str(ex), "log": log})
    return rid


def learn_probe(ctx, app, sig: str, key: dict, rec: list[dict], state: dict):
    """Promote a read endpoint seen during a DOM probe, if it agrees with what the DOM showed."""
    if store.rungs_for(app.name, sig, statuses=("promoted",)):
        return
    for r in rec:
        if r["method"] != "GET" or r["status"] != 200 or not isinstance(r["response"], dict):
            continue
        if not all(_eq(r["response"].get(k), v) for k, v in key.items()):
            continue
        agree = all(_eq(r["response"].get(f), state.get(f)) for f in ("display_name", "role", "active") if state.get(f) is not None)
        if not agree:
            continue
        s = Synth(key, list(key), [], {})
        u = s.url_expr(r["url"], None)
        code = PROBE_TEMPLATE.format(meta=json.dumps({"signature": sig, "params": list(key), "url": r["url"]}), url=u)
        path = _write(app.name, f"probe_{sig.replace(':', '_')}_{len(store.rungs_for(app.name, sig, statuses=('promoted', 'demoted'))) + 1}", code)
        # self-test before trusting it: the learned read must reproduce what the DOM showed
        try:
            got = load_tool(str(path)).run(app.http(ctx), key)
            if not got or not all(_eq(got.get(f), state.get(f)) for f in ("display_name", "role", "active") if state.get(f) is not None):
                raise RuntimeError("learned read disagrees with DOM")
        except Exception as ex:  # noqa: BLE001
            ctx.log("promoter.probe_rejected", {"reason": str(ex)})
            path.unlink(missing_ok=True)
            return None
        rid = store.add_rung(app.name, sig, "learned_probe", path, "promoted", note=f"cross-checked against DOM for {key}")
        ctx.log("promoter.probe_promoted", {"rung": rid, "url_template": u})
        return rid
