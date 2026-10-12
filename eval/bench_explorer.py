"""Layer benchmark: the model explorer alone (no learned tools, no promoter), on the UI actions the
end-to-end eval needs, starting cold. Each result is checked with the app's own probe (app.holds).

  RUNG_MODEL=nim NVIDIA_API_KEY=... python -m eval.bench_explorer
"""
from __future__ import annotations

import sys
import time

from eval.run_eval import reset
from rung.apps import registry
from rung.ctx import Ctx
from rung.llm.client import from_env
from rung.types import Delta
from sandbox.common import set_flags

U = "zz-bench-1"


def chat_user(name):
    return Delta("chat", "user", {"username": name}, {"exists": True, "display_name": "ZZ Bench", "email": f"{name}@acme.test", "title": "Bench"})


CASES = [
    (1, chat_user(U)),
    (1, Delta("chat", "channel_member", {"channel": "data", "user": U}, {"exists": True})),
    (1, Delta("chat", "channel_member", {"channel": "dashboards", "user": U}, {"exists": True})),
    (1, Delta("chat", "channel_member", {"channel": "data", "user": U}, {"exists": False})),
    (1, Delta("chat", "user", {"username": U}, {"active": False})),
    (1, Delta("sso", "user", {"username": U}, {"exists": True, "email": f"{U}@acme.test", "first_name": "ZZ", "last_name": "Bench"})),
    (1, Delta("sso", "user", {"username": U}, {"active": False})),
    (2, chat_user("zz-bench-2")),
    (2, Delta("chat", "channel_member", {"channel": "random", "user": "zz-bench-2"}, {"exists": True})),
    (3, chat_user("zz-bench-3")),
    (3, Delta("chat", "channel_member", {"channel": "random", "user": "zz-bench-3"}, {"exists": True})),
]


def main():
    model = from_env()
    if model is None:
        sys.exit("needs RUNG_MODEL")
    reset()
    ok_n, rows = 0, []
    for variant, delta in CASES:
        set_flags(chat_ui_variant=variant)
        app = registry.get(delta.app)
        ctx = Ctx("bench", model=model)
        t0 = time.time()
        try:
            res = app.explore(ctx, delta, app.values(delta, {}))
            held = app.holds(ctx, delta)[0]
            detail = res.detail
        except Exception as ex:  # noqa: BLE001
            held, detail = False, f"{type(ex).__name__}: {ex}"[:200]
        finally:
            calls = ctx.stats["llm_calls"]
            ctx.close()
        ok_n += held
        rows.append((held, time.time() - t0, calls))
        print(("ok   " if held else "FAIL ") + f"{time.time() - t0:6.1f}s {calls:2d} calls  v{variant} {delta.describe()}"
              + ("" if held else f"\n       {detail[:300]}"), flush=True)
    set_flags(chat_ui_variant=1)
    print(f"\n{ok_n}/{len(CASES)} UI changes verified | {sum(r[2] for r in rows)} model calls | {sum(r[1] for r in rows):.0f}s total")


if __name__ == "__main__":
    main()
