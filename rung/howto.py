"""Unknown task: find the how-to before asking for one.

Code gathers every handbook/templates document and recent ops@ mail (rung.precedent), the model
picks which (if any) explains the task (rung.llm.calls.find_howto), and code validates the pick:
only ids that were shown, never an untrusted mail (by the structured trust flag), and only a
confident "found". A valid pick is drafted into a procedure with the same validator as `sop:`
drafting, saved under drafts/, and shown to the requester for approval. Nothing runs before that.
"""
from __future__ import annotations

from rung import precedent
from rung.apps.docs import drive_path
from rung.types import Gap

MAX_PICK = 3


def validate_pick(pick, sources: list[dict]) -> tuple[bool, str, list[dict]]:
    """-> (usable, reason if not, the picked sources)."""
    by_id = {s["id"]: s for s in sources}
    unknown = [i for i in pick.source_ids if i not in by_id]
    if unknown:
        return False, f"the model picked sources that were never shown: {unknown}", []
    untrusted = [i for i in pick.source_ids if not by_id[i]["trusted"]]
    if untrusted:
        who = ", ".join(sorted({by_id[i].get("sender", i) for i in untrusted}))
        return False, f"the picked source {', '.join(untrusted)} comes from outside the company ({who}) and can't define a procedure", []
    if len(pick.source_ids) > MAX_PICK:
        return False, f"more than {MAX_PICK} sources picked", []
    if not pick.found or not pick.source_ids:
        return False, "no source explains this task", []
    if pick.confidence != "high":
        return False, "no source clearly explains this task (low confidence)", []
    return True, "", [by_id[i] for i in pick.source_ids]


def _full_text(s: dict) -> dict:
    """The picked source in full (the model chose from excerpts)."""
    if s["kind"] == "doc":
        p = drive_path(s["path"])
        return {"id": s["id"], "label": s["path"], "text": p.read_text(errors="replace")}
    return {"id": s["id"], "label": f"mail from {s['sender']}: {s['subject']}", "text": s["text"]}


def directions_question(procs: dict, why: str) -> dict:
    return {"key": "task_type", "q": f"I don't have a procedure for this yet, and {why}. Known tasks: {', '.join(procs)}. "
                                     "Point me to a doc describing how it's done (reply 'sop: <path on the Drive>') "
                                     "and I'll draft a procedure for your approval."}


def approval_question(proc: dict, found_in: list[str]) -> dict:
    steps = "\n".join(f"  {i + 1}. {st['description']} -> {st['delta']}" for i, st in enumerate(proc["steps"]))
    return {"key": "approve_procedure", "drafted": proc["task_type"],
            "q": f"I found how to do this in: {', '.join(found_in)}. Proposed procedure '{proc['task_type']}':\n{steps}\n"
                 "Reply 'approve_procedure: yes' to save and run it, or 'sop: <path>' to point me elsewhere."}


def find_and_draft(ctx, goal, request: str, understanding, procs: dict) -> None:
    """Adds to `goal` either an approval question (a validated draft exists under drafts/) or a
    request for directions. Never plans or runs anything."""
    from rung import procedures as P
    from rung.llm.calls import find_howto
    sources = precedent.gather_howto_sources(ctx)
    pick = find_howto(ctx.model, request, understanding.model_dump(), sources)
    ctx.log("howto.pick", pick.model_dump())
    ok, why, picked = validate_pick(pick, sources)
    goal.gaps.append(Gap("howto_sources", f"{len(sources)} considered", "resolved",
                         f"model picked {pick.source_ids or 'none'} ({pick.confidence}): {pick.why}"
                         + ("" if ok else f" -- rejected in code: {why}")))
    goal.flags.append({"kind": "howto", "detail": f"Looked for a written how-to in {len(sources)} sources. Picked: "
                                                  f"{', '.join(pick.source_ids) or 'none'} ({pick.why}). "
                                                  + ("Accepted; drafted for approval." if ok else f"Not used: {why}.")})
    if not ok:
        goal.questions.append(directions_question(procs, why))
        return
    proc, errs = P.draft_from_sources(ctx, [_full_text(s) for s in picked])
    if not proc:
        goal.flags.append({"kind": "howto", "detail": f"The picked sources didn't make a valid procedure: {errs[:3]}"})
        goal.questions.append(directions_question(procs, "what I found didn't turn into a valid procedure"))
        return
    found_in = [s["path"] if s["kind"] == "doc" else f"mail '{s['subject']}' from {s['sender']}" for s in picked]
    goal.questions.append(approval_question(proc, found_in))
