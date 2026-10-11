"""Library of checks and derivations that procedures compose in YAML.

Checks may add questions (the ask budget: only what context can't answer).
Derivations compute values from systems of record and precedent, and record
*where* each value came from as a Gap with a source.
"""
from __future__ import annotations

from datetime import timedelta

from rung import policy, precedent, store
from rung.apps import registry
from rung.types import Citation, Delta, Gap, Intent
from sandbox.common import now


def _username(emp: dict) -> str:
    from rung.understand import username_from_name
    return emp.get("chat_username") or username_from_name(emp["full_name"])


# ------------------------------------------------------------------ checks
def required_fields(ctx, goal, proc, scope, answers):
    emp = scope["employee"]
    for f in proc.get("requires", []):
        if emp.get(f):
            continue
        if answers.get(f):
            emp[f] = answers[f]
            goal.gaps.append(Gap(f, answers[f], "resolved", "answer from requester"))
        else:
            goal.questions.append({"key": f, "q": f"PeopleHub has no {f.replace('_', ' ')} for {emp['full_name']}. What is it?"})


def date_agreement(ctx, goal, proc, scope, answers):
    """A date stated in the request must agree with the system of record."""
    from rung.understand import resolve_date
    emp = scope["employee"]
    asked = resolve_date(scope.get("date_text"))
    if asked and emp.get("start_date") and asked != emp["start_date"]:
        if answers.get("start_date"):
            emp["start_date"] = answers["start_date"]
            goal.gaps.append(Gap("start_date", emp["start_date"], "resolved", "answer from requester"))
        else:
            goal.questions.append({"key": "start_date", "q": f"You said '{scope['date_text']}' ({asked}) but PeopleHub says "
                                                             f"{emp['full_name']} starts {emp['start_date']}. Which date is right (YYYY-MM-DD)?"})
    elif emp.get("start_date"):
        goal.gaps.append(Gap("start_date", emp["start_date"], "resolved",
                             "PeopleHub" + (f" (agrees with '{scope['date_text']}' in the request)" if asked else "")))


def existing_account(ctx, goal, proc, scope, answers):
    """Rehire detection: a deactivated account with the same username already exists."""
    emp = scope["employee"]
    username = _username(emp)
    u = registry.get("chat").probe_user(ctx, username)
    scope["reactivate"] = False
    if u and not u.get("active"):
        a = answers.get("rehire")
        if a == "reactivate":
            scope["reactivate"] = True
            goal.gaps.append(Gap("chat_account", "reactivate existing", "resolved", "answer from requester"))
        elif a == "new":
            scope["username_override"] = username + "2"
            goal.gaps.append(Gap("chat_account", scope["username_override"], "resolved", "answer from requester (new account)"))
        else:
            goal.questions.append({"key": "rehire", "q": f"There is a deactivated Chatter account '{username}' (a previous employee?). "
                                                         "Reactivate it, or create a new account? (reactivate/new)"})


CHECKS = {"required_fields": required_fields, "date_agreement": date_agreement, "existing_account": existing_account}


# ------------------------------------------------------------------ derivations
def username_for(ctx, goal, scope, employee):
    if scope.get("username_override"):
        return scope["username_override"]
    u = _username(employee)
    goal.gaps.append(Gap("username", u, "resolved" if employee.get("chat_username") else "default",
                         "PeopleHub" if employee.get("chat_username") else "SOP convention firstname.lastname"))
    return u


def manager_of(ctx, goal, scope, employee):
    if employee.get("manager_id"):
        m = registry.get("hr").probe(ctx, "employee", {"id": employee["manager_id"]})[0]
        if m:
            goal.gaps.append(Gap("manager", m["full_name"], "resolved", "PeopleHub manager_id"))
            return m
    goal.gaps.append(Gap("manager", None, "default", "PeopleHub has no manager on record; requester used"))
    return {"full_name": "your manager", "work_email": goal.requester}


def team_channels(ctx, goal, scope, employee, username):
    """Which channels does this team use? Precedent (peers, chat history, mail) filtered by
    written policy, plus human corrections."""
    chans, cites, stats = precedent.peer_channels(ctx, employee)
    ctx.log("precedent.peers", {"channels": chans, "stats": stats})
    goal.gaps.append(Gap("team_channels", list(chans), "resolved",
                         f"precedent: channel memberships of {stats['peers']} active {employee['title']}s in {employee['team']}"))
    try:
        cites += precedent.chat_announcements(ctx, employee["title"])
    except Exception as ex:  # noqa: BLE001
        ctx.log("precedent.chat_error", {"error": str(ex)})
    for cor in store.corrections(goal.task_type):
        if cor["scope"] and cor["scope"] not in (employee.get("title"), employee.get("team")):
            continue
        for c in cor["body"].get("add_channels", []):
            if c not in chans:
                chans.append(c)
                cites.append(Citation("correction", f"#{c}", f"correction #{cor['id']} on {cor['ts'][:10]}"))
        for c in cor["body"].get("remove_channels", []):
            if c in chans:
                chans.remove(c)
                goal.flags.append({"kind": "correction", "detail": f"#{c} not added, per correction #{cor['id']}"})
    for m in precedent.mail_instructions(ctx):
        for cand in m["candidates"]:
            d = Delta("chat", "user", {"username": username}, {"role": "admin"}) if cand["kind"] == "role" else \
                Delta("chat", "channel_member", {"channel": cand["value"], "user": username}, {"exists": True})
            it = Intent("", d)
            it.risk = policy.classify(it)
            why = [] if m["trusted_sender"] else [f"sender {m['sender']} is outside the company domain"]
            if not policy.precedent_allowed(it.risk):
                why.append(f"precedent may not authorize {it.risk} actions")
            why += [v["reason"] for v in policy.violations(it, employee)]
            if why:
                goal.flags.append({"kind": "suspicious_instruction" if not m["trusted_sender"] else "precedent_rejected",
                                   "detail": f"Mail '{m['subject']}' from {m['sender']} asks for {cand['kind']} = {cand['value']}. "
                                             f"Not done: {'; '.join(why)}.", "source": f"mail #{m['id']}"})
            elif cand["kind"] == "channel" and cand["value"] not in chans:
                chans.append(cand["value"])
                cites.append(Citation("precedent:mail", f"#{cand['value']}", f"mail #{m['id']}: {m['excerpt'][:120]}"))
    scope["_cites"] += cites
    scope["_precedent_items"] += chans
    return chans


def join_channels(ctx, goal, scope, items):
    allowed = [c for c in items if not policy.violations(Intent("", Delta("chat", "channel_member", {"channel": c, "user": "x"}, {"exists": True})),
                                                           scope.get("employee"))]
    return ", ".join(f"#{c}" for c in allowed) or "the team channels"


def user_channels(ctx, goal, scope, username, exclude=("general",)):
    u = registry.get("chat").probe_user(ctx, username) or {}
    chans = [c for c in u.get("channels", []) if c not in exclude]
    goal.gaps.append(Gap("current_channels", chans, "resolved", "Chatter (current memberships)"))
    return chans


def date_or_today(ctx, goal, scope, text=None, label="date"):
    from rung.understand import resolve_date
    d = resolve_date(text if text and not str(text).startswith("{") else None)
    goal.gaps.append(Gap(label, d or now().astimezone().date().isoformat(), "resolved" if d else "default",
                         "request" if d else "today (no date in the request)"))
    return d or now().astimezone().date().isoformat()


def month_start(ctx, goal, scope):
    return now().astimezone().date().replace(day=1).isoformat()


def today(ctx, goal, scope, offset_days=0):
    return (now().astimezone().date() + timedelta(days=int(offset_days))).isoformat()


def hr_query(ctx, goal, scope, label="records", exclude_status="terminated", **params):
    rows = [e for e in registry.get("hr").search(ctx, **params) if e.get("status") != exclude_status]
    goal.gaps.append(Gap(label, [e["full_name"] for e in rows], "resolved", f"PeopleHub query {params}"))
    return rows


def format_rows(ctx, goal, scope, items, fmt, empty="None."):
    return "\n".join(fmt.format(**{k: (v if v is not None else "") for k, v in it.items()}) for it in items) or empty


def count(ctx, goal, scope, items):
    return len(items)


def requester(ctx, goal, scope):
    goal.gaps.append(Gap("recipient", goal.requester, "default", "the requester"))
    return goal.requester


FNS = {f.__name__: f for f in [username_for, manager_of, team_channels, join_channels, user_channels, date_or_today,
                              month_start, today, hr_query, format_rows, count, requester]}
