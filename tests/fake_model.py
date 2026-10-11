"""A scripted stand-in for a real model, used to exercise the LLM code paths offline
(prompt -> JSON -> validated -> executed). It is NOT evidence of model quality."""
import json
import re


class FakeModel:
    def __init__(self):
        self.calls = 0
        self.log = []

    def complete(self, system, user, max_tokens=1500):
        self.calls += 1
        self.log.append(system[:60])
        if "extract structured fields" in system:
            words = re.findall(r"\b[A-Z][a-z]+\b", user.split("<data>")[1])
            person = next((w for w in words if w not in ("Please", "Onboard", "Monday")), None)
            task = "onboarding" if "onboard" in user.lower() else None
            date = "monday" if "monday" in user.lower() else None
            return f"<think>reasoning</think>```json\n{json.dumps({'task_type': task, 'person': person, 'date_text': date})}\n```"
        if "operate a web app UI" in system:
            els = json.loads(re.search(r"Elements: (\[.*?\])\n<data>", user, re.S).group(1))
            goal = re.search(r"Goal state: (.*)", user).group(1)
            vals = json.loads(re.search(r"Values to use: (\{.*?\})\n", user).group(1))
            if goal in getattr(self, "issued", set()):
                return json.dumps({"action": "done"})
            if "channel_member" in goal:
                sel = next((e for e in els if e["tag"] == "select"), None)
                if sel:
                    btn = next(e for e in els if e["tag"] == "button" and int(e["id"]) > int(sel["id"]))
                    opt = next(o for o in sel["options"] if o.lstrip("#") == vals["channel"])
                    self.issued = getattr(self, "issued", set()) | {goal}
                    return json.dumps({"action": "act", "actions": [{"action": "select", "id": sel["id"], "value": opt},
                                                                    {"action": "click", "id": btn["id"]}]})
                link = next((e for e in els if e["tag"] == "a" and e.get("href", "").endswith("/" + vals["user"])), None) or \
                    next(e for e in els if e["tag"] == "a" and e.get("text") in ("People", "Directory"))
                return json.dumps({"action": "act", "actions": [{"action": "click", "id": link["id"]}]})
            return json.dumps({"action": "give_up", "reason": "fake model only knows channel membership"})
        if "Extract any onboarding actions" in system:
            text = user.lower()
            c = [{"kind": "role", "value": "admin"}] if "admin" in text else []
            c += [{"kind": "channel", "value": x} for x in re.findall(r"#([a-z-]+)", text)]
            return json.dumps({"candidates": c})
        return "{}"
