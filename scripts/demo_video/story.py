"""The demo story: five chapters of one real run on the model, against a fresh sandbox company.

  python scripts/demo_video/story.py --setup   # fresh company and agent state (run before recording starts)
  python scripts/demo_video/story.py           # the chapters (RUNG_MODEL must be set)
"""
from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from eval.run_eval import hr_add, reset  # noqa: E402
from rung import runtime, store  # noqa: E402
from sandbox.common import VAR, set_flags  # noqa: E402

FRAMES = Path(os.environ.get("RUNG_DEMO_FRAMES", VAR / "demo"))
REQUESTER = "arjun.mehta@acme.test"


def approver(kind, question, options):
    return "yes" if kind == "confirm" else ("deny" if kind == "approve" else None)


def chapter(title, subtitle="", pause=3.0):
    store.event("demo", "demo.chapter", {"title": title, "subtitle": subtitle})
    time.sleep(pause)


def main():
    if "--setup" in sys.argv:
        reset()
        shutil.rmtree(FRAMES, ignore_errors=True)
        FRAMES.mkdir(parents=True, exist_ok=True)
        return
    from rung.llm.client import from_env
    model = from_env()
    run = lambda req, answers=None: runtime.start(req, REQUESTER, approver, model, answers)  # noqa: E731
    drafted = None
    try:
        chapter("1 · Cold start", "Rung has never used Chatter: it reads the request, plans from evidence, explores the UI and learns its API")
        run("Please onboard Priya, she's joining Monday as a data analyst")

        chapter("2 · Warm run", "Same kind of task for the next hire: the learned API tools run with no model and no browser")
        run("Onboard Karan Shah please, starts Monday")

        chapter("3 · A task Rung has never seen", "No procedure exists: it looks for a written how-to, drafts a skill, and waits for approval")
        rid = run("Please give Kavya Reddy dashboard access")
        drafted = next((q.get("drafted") for q in runtime.load_goal(rid).questions if q.get("drafted")), None)
        if drafted:
            chapter("3 · Skill approved", "The requester replies 'approve_procedure: yes': the skill is saved and runs")
            run("Please give Kavya Reddy dashboard access", {"approve_procedure": "yes", "drafted": drafted})

        chapter("4 · Chatter ships API v2", "Internal update: the v1 endpoints the learned tools call now answer 410 Gone")
        set_flags(chat_api_version=2)
        hr_add("Tara Menon", "Data Analyst", "Analytics", "2026-10-12")
        run("Onboard Tara Menon, she starts Monday")

        chapter("5 · Relearned", "The next hire runs on the new v2 tool, fast again")
        hr_add("Omar Khan", "Data Analyst", "Analytics", "2026-10-12")
        run("Onboard Omar Khan, he starts Monday")
        chapter("Done", "Every step above was verified against the apps' real state", pause=5)
    finally:
        set_flags(chat_api_version=1)
        from rung.understand import PROC_DIR   # keep the repository's procedure set unchanged
        for f in ([PROC_DIR / f"{drafted}.yaml"] if drafted else []) + list((PROC_DIR / "drafts").glob("*.yaml")):
            f.unlink(missing_ok=True)
        (FRAMES / "done").write_text("done")


if __name__ == "__main__":
    main()
