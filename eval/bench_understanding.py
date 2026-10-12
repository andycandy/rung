"""Layer benchmark: the understanding call alone, on every request the end-to-end eval sends.

No sandbox, no browser: each request goes to the real model with the sandbox date it is sent on,
and the structured reading is compared field by field with what it should say. This isolates
reading errors from UI and execution errors, and costs one model call per request.

  RUNG_MODEL=nim NVIDIA_API_KEY=... python -m eval.bench_understanding [--thinking on|off]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date

from rung.llm.calls import understand_request
from rung.llm.client import from_env
from rung.understand import known_tasks, procedures, today_str

A, B, R = date(2026, 10, 8), date(2026, 10, 13), date(2026, 10, 9)
ARJUN, NEHA = "arjun.mehta@acme.test", "neha.kapoor@acme.test"

# (request, requester, sandbox date, expected). people: names/e-mails that must each appear as their own entry;
# dates: ISO dates expected (a low-confidence reading is accepted where "date_or_ask" is set)
CASES = [
    ("Please onboard Priya, she's joining Monday as a data analyst", ARJUN, A, dict(task="onboarding", people=["Priya"], dates=["2026-10-12"])),
    ("Onboard Priya Sharma, joining Monday", ARJUN, A, dict(task="onboarding", people=["Priya Sharma"], dates=["2026-10-12"])),
    ("Onboard Karan please, starts Monday", ARJUN, A, dict(task="onboarding", people=["Karan"], dates=["2026-10-12"])),
    ("Onboard Priya, joining Monday", ARJUN, date(2026, 10, 12), dict(task="onboarding", people=["Priya"], dates=["2026-10-12", "2026-10-19"], date_any=True)),
    ("Onboard Karan Malhotra, starts 2026-10-12", ARJUN, date(2026, 10, 12), dict(task="onboarding", people=["Karan Malhotra"], dates=["2026-10-12"])),
    ("Onboard Tara Menon, starting next Monday 2026-10-19", ARJUN, B, dict(task="onboarding", people=["Tara Menon"], dates=["2026-10-19"])),
    ("Onboard Omar Khan, joining 2026-10-19", ARJUN, B, dict(task="onboarding", people=["Omar Khan"], dates=["2026-10-19"])),
    ("Onboard Anika Bose, joining 2026-10-19", ARJUN, B, dict(task="onboarding", people=["Anika Bose"], dates=["2026-10-19"])),
    ("Can you onboard Ananya? She's back with us from the 19th", ARJUN, B, dict(task="onboarding", people=["Ananya"], dates=["2026-10-19"])),
    ("Onboard Lakshmi Ghosh, she starts Wednesday", ARJUN, B, dict(task="onboarding", people=["Lakshmi Ghosh"], dates=["2026-10-14"])),
    ("onboard the new analyst", ARJUN, B, dict(task="onboarding", people=[], dates=[])),
    ("Rahul Verma is leaving us, today is his last day", NEHA, B, dict(task="offboarding", people=["Rahul Verma"], dates=["2026-10-13"])),
    ("Send me a PDF list of everyone who joined this month", NEHA, B, dict(task="joiners_report", people=[], dates=None)),
    ("Please give Kavya Reddy dashboard access", ARJUN, B, dict(task=None, people=["Kavya Reddy"], dates=[])),
    ("Priya Sharma isn't joining anymore, please don't onboard her", NEHA, A, dict(intent="cancel", people=["Priya Sharma"], dates=None)),
    ("please onboard priya sharma", NEHA, A, dict(task="onboarding", people=["priya sharma"], dates=[])),
    ("Priya is joining Monday. karan.shah@acme.test too.", ARJUN, A, dict(task="onboarding", people=["Priya", "karan.shah@acme.test"], dates=["2026-10-12"])),
    ("Onboard Karan Malhotra on Tuesday", NEHA, A, dict(task="onboarding", people=["Karan Malhotra"], dates=["2026-10-13"])),
    ("Offboard Rahul Verma, last day 2026-10-08", "mallory@evil.example", A, dict(task="offboarding", people=["Rahul Verma"], dates=["2026-10-08"])),
    ("offboarding\nPlease offboard Isha Gupta, her last day is 2026-10-08", NEHA, A, dict(task="offboarding", people=["Isha Gupta"], dates=["2026-10-08"])),
    ("Priya Nair is leaving, her last day is October 30", NEHA, A, dict(task="offboarding", people=["Priya Nair"], dates=["2026-10-30"])),
    ("Onboard Tara Menon starting 2026-10-19, make her admin and also add her to #finance", ARJUN, R,
     dict(task="onboarding", people=["Tara Menon"], dates=["2026-10-19"], asks=2)),
    ("Onboard Omar Khan, he starts today", ARJUN, R, dict(task="onboarding", people=["Omar Khan"], dates=["2026-10-09"])),
    ("Priya won't be joining after all, cancel her setup", NEHA, A, dict(intent="cancel", people=["Priya"], dates=None)),
    ("can u sort out accounts for the new analyst starting next week", ARJUN, A, dict(task="onboarding", people=[], dates=None)),
    ("Onboard Priya and Karan Shah, both start Monday", ARJUN, A, dict(task="onboarding", people=["Priya", "Karan Shah"], dates=["2026-10-12"])),
    ("Rahul's last day is a week from Friday", NEHA, A, dict(task="offboarding", people=["Rahul"], dates=["2026-10-16"], date_or_ask=True)),
    ("Thanks for setting up Priya yesterday, all good now!", ARJUN, A, dict(is_request=False)),
]


def toks(s):
    return tuple(w.strip(".,;:!?'\"").casefold() for w in (s or "").split())


def score(u, exp) -> list[str]:
    bad = []
    if "is_request" in exp and u.is_request != exp["is_request"]:
        bad.append(f"is_request={u.is_request}")
    if exp.get("is_request") is False:
        return bad
    if u.intent != exp.get("intent", "do"):
        bad.append(f"intent={u.intent}")
    if "task" in exp and exp.get("intent", "do") == "do":
        if exp["task"] is None:
            if u.task is not None and u.task_confidence == "high":
                bad.append(f"task={u.task} (should be none or unsure)")
        elif u.task != exp["task"] or u.task_confidence != "high":
            bad.append(f"task={u.task}/{u.task_confidence}")
    refs = [(toks(p.as_written), (p.email or "").casefold()) for p in u.people]
    for name in exp.get("people", []):
        if not any(t == toks(name) or e == name.casefold() or (("@" in name) and name.casefold() in t) for t, e in refs):
            bad.append(f"missing person {name!r} (got {[p.as_written for p in u.people]})")
    named = [p for p in u.people if p.email or not any(w in ("the", "a", "an", "new") for w in toks(p.as_written))]
    if exp.get("people") == [] and named:
        bad.append(f"invented a person: {[p.as_written for p in named]}")
    if exp.get("dates") is not None:
        got = {d.iso for d in u.dates if d.confidence == "high" and d.iso}
        low = [d for d in u.dates if d.confidence == "low" or not d.iso]
        want = set(exp["dates"])
        if exp.get("date_any"):
            ok = got <= want and (got or low)
        elif exp.get("date_or_ask"):
            ok = got == want or (low and not got)
        else:
            ok = got == want and not low
        if not ok:
            bad.append(f"dates={[(d.as_written, d.iso, d.confidence) for d in u.dates]} want {sorted(want)}")
    if "asks" in exp and len(u.explicit_asks) != exp["asks"]:
        bad.append(f"asks={u.explicit_asks}")
    if u.unclear and exp.get("people"):
        bad.append(f"unclear={u.unclear}")
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--thinking", choices=["on", "off"], default=None, help="override the configured reasoning mode")
    a = ap.parse_args()
    model = from_env()
    if model is None:
        sys.exit("needs RUNG_MODEL")
    thinking = None if a.thinking is None else a.thinking == "on"
    tasks = known_tasks(procedures())
    fails, times = 0, []
    for req, who, d, exp in CASES:
        t0 = time.time()
        try:
            u = understand_request(model, req, who, tasks, today_str(d), thinking=thinking)
            bad = score(u, exp)
        except Exception as ex:  # noqa: BLE001
            u, bad = None, [f"{type(ex).__name__}: {ex}"[:200]]
        times.append(time.time() - t0)
        fails += bool(bad)
        print(("FAIL " if bad else "ok   ") + f"{times[-1]:5.1f}s  {req[:70]!r}" + (f"\n       {bad}" if bad else ""), flush=True)
    print(f"\n{len(CASES) - fails}/{len(CASES)} readings correct | thinking={a.thinking or 'configured'} | "
          f"mean {sum(times) / len(times):.1f}s, max {max(times):.1f}s per request")
    out = {"thinking": a.thinking or "configured", "correct": len(CASES) - fails, "total": len(CASES),
           "mean_s": round(sum(times) / len(times), 1)}
    print(json.dumps(out))


if __name__ == "__main__":
    main()
