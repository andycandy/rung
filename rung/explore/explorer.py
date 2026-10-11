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
# UI variant 3 and the stock Django admin use labels chosen independently of this list.
SYN = {"email": ["mail"], "username": ["user", "login"], "title": ["title", "role", "position", "job"],
       "display_name": ["name"], "first_name": ["first", "given"], "last_name": ["last", "surname", "family"],
       "active": ["active"], "channel": ["channel"]}
LEX = {
    "nav": ["people", "member", "members", "user", "users", "team", "staff", "employees", "person", "accounts"],
    "create": ["add", "new", "invite", "create"],
    "submit": ["save", "create", "submit", "add", "invite", "send", "ok"],
    "active=False": ["deactivate", "disable", "suspend"],
    "active=True": ["reactivate", "enable", "activate"],
    "exists=False": ["remove", "leave", "delete"],
    "avoid": ["logout", "log out", "sign out", "delete", "history", "password", "change password"],
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


def field_score(field: str, label: str) -> float:
    """How well a form label fits a delta field: synonyms first, then the field's own words."""
    lab = norm(label)
    if not lab:
        return 0
    words = SYN.get(field, []) + field.replace("is_", "").split("_")
    hits = sum(1 for w in set(words) if w and w in lab)
    return hits + (0.5 if any(lab.startswith(w) for w in SYN.get(field, [])) else 0)


class HeuristicExplorer:
    """Offline UI policy. Generic over deltas: it never branches on entity names.
      1. fill any form whose inputs fit the fields still to change, then submit it;
      2. pick a select option that equals a target value;
      3. press a button whose verb matches a boolean/existence change;
      4. otherwise navigate (target-named links > entity words > create words > breadth-first).
    After every action the caller's probe decides whether the goal holds."""
    name = "explore:heuristic"

    def __init__(self, budget: int = 14):
        self.budget = budget

    @staticmethod
    def _wanted(delta: Delta, values: dict) -> dict:
        w = {k: v for k, v in delta.expect.items() if k != "exists"}
        if delta.expect.get("exists") is True:
            w.update({k: v for k, v in delta.key.items()})
        return {k: v for k, v in w.items() if v is not None}

    def fill_form(self, ab, snap, delta, values, tr, used) -> bool:
        if re.search(r"login|signin|sign-in", snap["url"], re.I):
            return False
        wanted = self._wanted(delta, values)
        by_form: dict[int, list] = {}
        for e in snap["elements"]:
            by_form.setdefault(e.get("form", -1), []).append(e)
        for fi, els in by_form.items():
            if fi < 0:
                continue
            inputs = [e for e in els if e["tag"] in ("input", "textarea") and e.get("type") in ("text", "email", "checkbox", "", None)]
            pwd = [e for e in els if e.get("type") == "password"]
            pairs = sorted(((field_score(f, e.get("label", "")), f, e) for e in inputs for f in wanted), key=lambda x: -x[0])
            matched, taken = {}, set()
            for sc, f, e in pairs:
                if sc <= 0 or f in matched or e["id"] in taken:
                    continue
                if (e.get("type") == "checkbox") != isinstance(wanted[f], bool):
                    continue
                matched[f] = e
                taken.add(e["id"])
            left_inputs = [e for e in inputs if e["id"] not in taken and e.get("type") != "checkbox"]
            left_wanted = [f for f in wanted if f not in matched and not isinstance(wanted[f], bool)]
            if len(left_inputs) == 1 and len(left_wanted) == 1:
                matched[left_wanted[0]] = left_inputs[0]
                taken.add(left_inputs[0]["id"])

            def differs(f, e):
                if isinstance(wanted[f], bool):
                    return bool(e.get("value")) != wanted[f]
                return str(e.get("value") or "") != str(wanted[f])
            todo = {f: e for f, e in matched.items() if differs(f, e)}
            if not todo or (snap["url"], fi, tuple(sorted(todo))) in used:
                continue
            if delta.expect.get("exists") is True and not all(k in matched for k in delta.key):
                continue  # a create form must carry the entity's identity
            used.add((snap["url"], fi, tuple(sorted(todo))))
            for f, e in todo.items():
                if isinstance(wanted[f], bool):
                    ab.check(e["id"], wanted[f])
                else:
                    ab.fill(e["id"], wanted[f])
                tr.actions.append(("fill", e["id"], f))
            if pwd and delta.expect.get("exists") is True:
                import secrets
                secret = secrets.token_urlsafe(14) + "!9a"
                for e in pwd:   # initial password: random, never logged; the person resets it on first login
                    ab.fill(e["id"], secret)
                tr.actions.append(("fill", "password", "<generated>"))
            subs = [e for e in els if (e["tag"] == "button" and e.get("type", "submit") != "button") or e.get("type") == "submit"]
            subs.sort(key=lambda e: -(3 if e.get("name") == "_save" else score(e.get("text", ""), LEX["submit"])))
            if subs:
                ab.click(subs[0]["id"])
                tr.actions.append(("click", subs[0]["id"], subs[0].get("text")))
                return True
        return False

    def pick_option(self, ab, snap, delta, values, tr, used) -> bool:
        if delta.expect.get("exists") is not True:
            return False
        targets = {norm(v).lstrip("#") for v in delta.key.values()}
        for e in snap["elements"]:
            if e["tag"] != "select" or (snap["url"], e["id"]) in used:
                continue
            opt = next((o for o in e["options"] if norm(o).lstrip("#") in targets), None)
            if opt:
                used.add((snap["url"], e["id"]))
                ab.select(e["id"], opt)
                btn = ab.el(e["id"]).evaluate(
                    "e => { let p = e.parentElement; for (let i=0;i<3 && p;i++,p=p.parentElement){ const b=p.querySelector('button,[type=submit]'); if(b) return b.getAttribute('data-rung-id'); } return null; }")
                if btn:
                    ab.click(btn)
                    tr.actions.append(("select+click", e["id"], opt))
                    return True
        return False

    def press_verb(self, ab, snap, delta, values, tr, used) -> bool:
        verbs = []
        for f, v in delta.expect.items():
            if isinstance(v, bool):
                verbs += LEX.get(f"{f}={v}", [])
        if not verbs:
            return False
        # when the delta's key names a sub-thing (e.g. a channel), the button must mention it
        subs = [norm(v).lstrip("#") for k, v in delta.key.items() if k not in ("user", "username", "id")]
        for e in snap["elements"]:
            if e["tag"] not in ("button", "input") or (e["tag"] == "input" and e.get("type") not in ("submit", "button")):
                continue
            text = norm(e.get("text", ""))
            if not score(text, verbs) or (snap["url"], e["id"]) in used:
                continue
            ctx_text = text + " " + norm(e.get("context", ""))
            if subs and not all(re.search(rf"#?{re.escape(s)}\b", ctx_text) for s in subs):
                continue
            used.add((snap["url"], e["id"]))
            ab.click(e["id"])
            tr.actions.append(("click", e["id"], e.get("text")))
            return True
        return False

    def next_link(self, snap, delta: Delta, values: dict, visited: set):
        targets = [norm(v) for k, v in delta.key.items() if isinstance(v, str)] + \
                  [norm(values.get(k)) for k in ("username", "user", "display_name") if values.get(k)]
        ent_words = LEX["nav"] + delta.entity.split("_")
        best, bs, fallback = None, 0.0, None
        for e in snap["elements"]:
            href = e.get("href") or ""
            if e["tag"] != "a" or not href.startswith("/") or href in visited or href.startswith("/c/"):
                continue
            t = norm(e.get("text"))
            if score(t, LEX["avoid"]) or not t:
                continue
            fallback = fallback or e
            s = 0.0
            if any(x and (t == x or href.rstrip("/").endswith("/" + x)) for x in targets):
                s = 10
            s = max(s, 2 * score(t, ent_words))
            if delta.expect.get("exists") is True:
                s = max(s, 2.5 * score(t, LEX["create"]) + (1 if score(t, ent_words) else 0))
            if s > bs:
                best, bs = e, s
        return best if bs > 0 else fallback

    def run(self, ab, delta: Delta, values: dict, check=None, start: str = "/") -> Trace:
        tr = Trace()
        visited: set = set()
        used: set = set()
        ab.goto(start)
        for _ in range(self.budget):
            snap = ab.snapshot()
            tr.pages.append(snap["url"])
            visited.add(snap["url"])
            acted = any(f(ab, snap, delta, values, tr, used) for f in (self.fill_form, self.pick_option, self.press_verb))
            if acted:
                ab.settle()
                if check is None or check():
                    return tr
                continue          # partly done (e.g. a two-step create): keep going from here
            link = self.next_link(snap, delta, values, visited)
            if not link:
                if snap["url"] != start:
                    ab.goto(start)
                    continue
                tr.gave_up = "no relevant navigation left"
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

    def run(self, ab, delta: Delta, values: dict, check=None, start: str = "/") -> Trace:
        from rung.llm.calls import explore_step

        tr = Trace()
        ab.goto(start)
        history: list[str] = []
        for _ in range(self.budget):
            snap = ab.snapshot()
            tr.pages.append(snap["url"])
            step = explore_step(self.model, delta, values, snap, history[-6:])
            tr.llm_calls += 1
            if step.action == "done":
                if check is None or check():
                    return tr
                history.append("you said done, but the independent check says the goal does NOT hold yet")
                continue
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
