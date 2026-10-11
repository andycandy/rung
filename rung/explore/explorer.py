"""Explore: operate an app's UI to achieve a state delta when no trusted rung exists.

Two policies share one action vocabulary (goto/click/fill/select/check):
  * LLMExplorer      -- the model reads an accessibility snapshot and picks actions.
  * HeuristicExplorer -- offline fallback: label/role matching with a small lexicon.
Neither may declare success: the caller verifies with a probe afterwards.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from rung.types import Delta

# A small, app-agnostic vocabulary. Deliberately NOT tuned to any one app's labels:
# the eval's redesigned UI (variant 3) uses labels chosen before this list was written.
LEX = {
    "nav": ["people", "member", "members", "user", "users", "team", "staff", "employees", "person"],
    "create": ["add", "new", "invite", "create"],
    # field -> label tokens, checked in priority order (most specific first)
    "field_tokens": [("email", ["mail"]), ("username", ["user", "login", "username"]),
                     ("title", ["title", "role", "position", "job"]), ("display_name", ["name"])],
    "active=False": ["deactivate", "disable", "suspend"],
    "active=True": ["reactivate", "enable", "activate"],
    "member_remove": ["remove", "leave", "delete"],
    "role=admin": ["admin"],
    "avoid": ["logout", "sign out", "log out", "delete"],
}


def norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def score(text: str, words: list[str]) -> float:
    t = norm(text)
    best = 0.0
    for w in words:
        if t == w:
            best = max(best, 3)
        elif w in t:
            best = max(best, 2 if len(w) > 3 else 1)
    return best


@dataclass
class Trace:
    actions: list = field(default_factory=list)
    pages: list = field(default_factory=list)
    llm_calls: int = 0
    gave_up: str = ""


class HeuristicExplorer:
    name = "explore:heuristic"

    def __init__(self, budget: int = 12):
        self.budget = budget

    # ---------------------------------------------------------- affordances
    def _form_submit(self, ab, input_id):
        return ab.el(input_id).evaluate(
            "e => { const f = e.form; if (!f) return null; const b = f.querySelector('button,[type=submit]'); return b && b.getAttribute('data-rung-id'); }")

    def _sibling_button(self, ab, rid):
        return ab.el(rid).evaluate(
            "e => { let p = e.parentElement; for (let i=0;i<3 && p;i++,p=p.parentElement){ const b=p.querySelector('button'); if(b) return b.getAttribute('data-rung-id'); } return null; }")

    def affordance(self, ab, snap, delta: Delta, values: dict, tr: Trace) -> bool:
        els = snap["elements"]
        exp = delta.expect
        if delta.entity == "user" and exp.get("exists") is True:
            inputs = [e for e in els if e["tag"] in ("input", "textarea") and e.get("type", "text") in ("text", "email", "")]
            wanted = [f for f, _ in LEX["field_tokens"] if f in values]
            matched: dict[str, str] = {}
            for e in inputs:
                lab = norm(e.get("label", ""))
                if e.get("type") == "email" and "email" in wanted and "email" not in matched.values():
                    matched[e["id"]] = "email"
                    continue
                for f, toks in LEX["field_tokens"]:
                    if f in wanted and f not in matched.values() and any(t in lab for t in toks):
                        matched[e["id"]] = f
                        break
            rest_in = [e for e in inputs if e["id"] not in matched]
            rest_f = [f for f in wanted if f not in matched.values()]
            if len(rest_in) == 1 and len(rest_f) == 1:  # assign the last field by elimination
                matched[rest_in[0]["id"]] = rest_f[0]
            if len(matched) >= 2 and len(matched) == len(inputs):
                for rid, f in matched.items():
                    ab.fill(rid, values[f])
                    tr.actions.append(("fill", rid, f))
                sub = self._form_submit(ab, next(iter(matched)))
                if sub:
                    ab.click(sub)
                    tr.actions.append(("click", sub, "submit"))
                    return True
        if delta.entity == "user" and "active" in exp:
            words = LEX[f"active={exp['active']}"]
            for e in els:
                if e["tag"] == "button" and score(e.get("text", ""), words) >= 2:
                    ab.click(e["id"])
                    tr.actions.append(("click", e["id"], e["text"]))
                    return True
        if delta.entity == "user" and exp.get("role") == "admin":
            for e in els:
                if e.get("type") == "checkbox" and score(e.get("label", ""), LEX["role=admin"]):
                    ab.check(e["id"])
                    tr.actions.append(("check", e["id"], e["label"]))
                    return True
        if delta.entity == "channel_member":
            ch = norm(values["channel"]).lstrip("#")
            if exp.get("exists") is True:
                for e in els:
                    if e["tag"] == "select":
                        opt = next((o for o in e["options"] if norm(o).lstrip("#") == ch), None)
                        if opt:
                            ab.select(e["id"], opt)
                            btn = self._sibling_button(ab, e["id"])
                            if btn:
                                ab.click(btn)
                                tr.actions.append(("select+click", e["id"], opt))
                                return True
            else:
                for e in els:
                    if e["tag"] == "button" and score(e.get("text", ""), LEX["member_remove"]) and \
                            re.search(rf"#?{re.escape(ch)}\b", norm(e.get("text", "")) + " " + norm(e.get("context", ""))):
                        ab.click(e["id"])
                        tr.actions.append(("click", e["id"], e["text"]))
                        return True
        return False

    # ---------------------------------------------------------- navigation
    def next_link(self, snap, delta: Delta, values: dict, visited: set):
        """Prefer links that name the target entity, then entity-ish words, then create words
        (for creates); otherwise fall back to any unvisited in-app link (breadth-first)."""
        targets = [norm(values.get(k)) for k in ("username", "user", "display_name") if values.get(k)]
        best, bs, fallback = None, 0.0, None
        for e in snap["elements"]:
            href = e.get("href") or ""
            if e["tag"] != "a" or not href.startswith("/") or href in visited or href.startswith("/c/"):
                continue
            t = norm(e.get("text"))
            if score(t, LEX["avoid"]):
                continue
            fallback = fallback or e
            s = 0.0
            if any(t == x for x in targets) or any(x and href.rstrip("/").endswith("/" + x) for x in targets):
                s = 10
            s = max(s, 2 * score(t, LEX["nav"]))
            if delta.entity == "user" and delta.expect.get("exists") is True:
                s = max(s, 2.5 * score(t, LEX["create"]))
            if s > bs:
                best, bs = e, s
        return best if bs > 0 else fallback

    def run(self, ab, delta: Delta, values: dict) -> Trace:
        tr = Trace()
        visited: set = set()
        ab.goto("/")
        for _ in range(self.budget):
            snap = ab.snapshot()
            tr.pages.append(snap["url"])
            visited.add(snap["url"])
            if self.affordance(ab, snap, delta, values, tr):
                ab.settle()
                return tr
            link = self.next_link(snap, delta, values, visited)
            if not link:
                if snap["url"] != "/":
                    ab.goto("/")
                    continue
                tr.gave_up = "no relevant navigation from home"
                return tr
            visited.add(link["href"])
            ab.click(link["id"])
            tr.actions.append(("click", link["id"], link.get("text")))
        tr.gave_up = "budget exhausted"
        return tr


class LLMExplorer:
    """Model-driven exploration: one typed call per step, bounded scratchpad."""
    name = "explore:llm"

    def __init__(self, model, budget: int = 12):
        self.model, self.budget = model, budget

    def run(self, ab, delta: Delta, values: dict) -> Trace:
        from rung.llm.calls import explore_step

        tr = Trace()
        ab.goto("/")
        history: list[str] = []
        for _ in range(self.budget):
            snap = ab.snapshot()
            tr.pages.append(snap["url"])
            step = explore_step(self.model, delta, values, snap, history[-6:])
            tr.llm_calls += 1
            if step.action == "done":
                return tr
            if step.action == "give_up":
                tr.gave_up = step.reason
                return tr
            try:
                for a in step.actions or [step]:
                    if a.action == "click":
                        ab.click(a.id)
                    elif a.action == "fill":
                        ab.fill(a.id, a.value)
                    elif a.action == "select":
                        ab.select(a.id, a.value)
                    elif a.action == "check":
                        ab.check(a.id, bool(a.value))
                    elif a.action == "goto":
                        ab.goto(a.value)
                    tr.actions.append((a.action, a.id, a.value))
                    history.append(f"{a.action} {a.id} {a.value or ''} on {snap['url']}")
            except Exception as ex:  # noqa: BLE001
                history.append(f"error: {ex}")
        tr.gave_up = "budget exhausted"
        return tr
