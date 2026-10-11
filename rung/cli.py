"""Command line.

  python -m rung run "Onboard Priya, she's joining Monday" --as arjun.mehta@acme.test
  python -m rung inbox            # handle requests emailed to ops@acme.test, reply in thread
  python -m rung tick             # wake scheduled / crashed runs
  python -m rung clock 2026-10-12T09:30:00+05:30
  python -m rung runs | report <id> | events <id> | rungs
  python -m rung correct onboarding --scope "Data Analyst" --add-channel finance
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from rung import runtime, store


def cli_approver(auto: str | None):
    def ask(kind, question, options):
        if auto == "yes" and kind == "confirm":   # --yes never approves elevated/irreversible gates
            print(f"\n[auto-yes] {question}")
            return "yes"
        if auto == "no":
            print(f"\n[auto-no] {question}")
            return None
        print(f"\n?? {question}")
        if not sys.stdin.isatty():
            return None
        return input("> ").strip() or None
    return ask


def model_from_env():
    from rung.llm.client import from_env
    return from_env()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="rung")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("request")
    r.add_argument("--as", dest="requester", default="arjun.mehta@acme.test")
    r.add_argument("--yes", action="store_const", const="yes", dest="auto")
    r.add_argument("--answer", action="append", default=[], help="key=value, pre-answers a question")
    sub.add_parser("tick")
    sub.add_parser("inbox")
    c = sub.add_parser("clock")
    c.add_argument("when")
    sub.add_parser("runs")
    sub.add_parser("rungs")
    for n in ("report", "events", "resume"):
        p = sub.add_parser(n)
        p.add_argument("rid")
    co = sub.add_parser("correct")
    co.add_argument("task_type")
    co.add_argument("--scope", default="")
    co.add_argument("--add-channel", action="append", default=[])
    co.add_argument("--remove-channel", action="append", default=[])
    a = ap.parse_args(argv)
    model = model_from_env()

    if a.cmd == "run":
        answers = dict(x.split("=", 1) for x in a.answer)
        rid = runtime.start(a.request, a.requester, cli_approver(a.auto), model, answers)
        print(store.get_run(rid)["report"])
    elif a.cmd == "tick":
        print("woke:", runtime.tick(cli_approver("yes"), model))
    elif a.cmd == "inbox":
        from rung.inbox import process
        for rid in process(model):
            print(store.get_run(rid)["report"])
    elif a.cmd == "clock":
        from sandbox.common import set_clock
        set_clock(datetime.fromisoformat(a.when))
        print("clock set to", a.when)
    elif a.cmd == "runs":
        for x in store.list_runs():
            print(x["id"], x["state"], x["created"][:16], x["request"][:70])
    elif a.cmd == "rungs":
        for x in store.all_rungs():
            print(f"{x['status']:<14} v{x['version']} {x['kind']:<16} {x['signature']:<60} ok={x['successes']} fail={x['failures']}")
    elif a.cmd == "report":
        print(store.get_run(a.rid)["report"])
    elif a.cmd == "events":
        for e in store.events(a.rid):
            print(e["seq"], e["ts"][11:19], e["kind"], json.dumps(e["data"])[:300])
    elif a.cmd == "resume":
        runtime.resume(a.rid, cli_approver("yes"), model)
        print(store.get_run(a.rid)["report"])
    elif a.cmd == "correct":
        store.add_correction(a.task_type, a.scope, {"add_channels": a.add_channel, "remove_channels": a.remove_channel})
        print("correction stored; cached procedure invalidated")


if __name__ == "__main__":
    main()
