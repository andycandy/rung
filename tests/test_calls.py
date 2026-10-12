"""Model-call plumbing: JSON extraction, validation, error feedback, and escalation instead of
guessing. The stub below returns the same canned text for every prompt; it understands nothing."""
import pytest

from rung.llm.calls import BadOutput, Understanding, _json, understand_request


class Canned:
    def __init__(self, text):
        self.text, self.prompts = text, []

    def complete(self, system, user, max_tokens=1500, thinking=None):
        self.prompts.append(user)
        return self.text


def test_json_is_found_after_think_blocks_and_fences():
    assert _json('<think>{"no": 1}</think>```json\n{"a": {"b": 2}}\n```') == {"a": {"b": 2}}
    with pytest.raises(BadOutput):
        _json("no json here")


def test_truncated_json_is_an_error_not_an_inner_fragment():
    cut = '{"steps": [{"description": "Add to #data", "delta": {"app": "chat", "entity": "channel_member"}, "citations": ["peer:x"'
    with pytest.raises(BadOutput, match="cut off"):
        _json(cut)            # used to return the inner delta object, which validated as an empty proposal


def test_empty_proposal_is_retried_then_escalates_when_the_procedure_needs_steps():
    from rung.llm.calls import propose_steps
    m = Canned('{"steps": []}')
    with pytest.raises(BadOutput):
        propose_steps(m, "onboarding", "channels", {}, {}, [], [{"id": "request"}], [], min_steps=1)
    assert len(m.prompts) == 3 and "at least 1 variable step" in m.prompts[1]
    assert propose_steps(Canned('{"steps": []}'), "t", "x", {}, {}, [], [{"id": "request"}], []) == []


GOOD = ('{"is_request": true, "intent": "do", "task": "%s", "task_confidence": "high", "people": [], '
        '"dates": [{"as_written": "Monday", "iso": "%s", "confidence": "high"}], "explicit_asks": [], "unclear": []}')


def test_valid_understanding_passes():
    m = Canned(GOOD % ("onboarding", "2026-10-12"))
    u = understand_request(m, "<request>", "a@acme.test", {"onboarding": "x"}, "2026-10-08 (Thursday)")
    assert isinstance(u, Understanding) and u.task == "onboarding" and len(m.prompts) == 1
    assert "<data><request></data>" in m.prompts[0] and "Today: 2026-10-08" in m.prompts[0]


@pytest.mark.parametrize("task, iso, err", [("payroll", "2026-10-12", "task must be one of"),
                                            ("onboarding", "next Monday", "iso must be YYYY-MM-DD")])
def test_invalid_answers_are_fed_back_then_escalate(task, iso, err):
    m = Canned(GOOD % (task, iso))
    with pytest.raises(BadOutput):
        understand_request(m, "<request>", "a@acme.test", {"onboarding": "x"}, "2026-10-08")
    assert len(m.prompts) == 3                                  # 1 try + 2 retries, then the run escalates
    assert err in m.prompts[1] and "previous answer was invalid" in m.prompts[1]


def test_unparseable_output_escalates():
    with pytest.raises(BadOutput):
        understand_request(Canned("I think she should be onboarded."), "<request>", "a@acme.test", {"onboarding": "x"}, "2026-10-08")


def test_rate_limiter_never_exceeds_the_cap_in_any_rolling_minute():
    from rung.llm.client import RateLimiter
    t = [0.0]
    sent = []
    lim = RateLimiter(18, clock=lambda: t[0], sleep=lambda s: t.__setitem__(0, t[0] + s))
    for _ in range(100):
        lim.acquire()
        sent.append(t[0])
        t[0] += 0.5                      # the caller tries to send every half second
    assert all(sum(1 for x in sent if a <= x < a + 60) <= 18 for a in sent)
    assert sent[18] >= sent[0] + 60      # the 19th request waited for the window to roll


def test_empty_answer_is_retried_with_a_clear_note():
    class EmptyThenGood(Canned):
        def complete(self, system, user, max_tokens=1500, thinking=None):
            self.prompts.append(user)
            return "" if len(self.prompts) == 1 else GOOD % ("onboarding", "2026-10-12")
    m = EmptyThenGood("")
    u = understand_request(m, "<request>", "a@acme.test", {"onboarding": "x"}, "2026-10-08")
    assert u.task == "onboarding" and "it was empty" in m.prompts[1]


def test_reasoning_mode_is_chosen_per_call_and_passed_to_the_client():
    from rung.llm.calls import REASONING
    seen = []

    class Spy(Canned):
        def complete(self, system, user, max_tokens=1500, thinking=None):
            seen.append((max_tokens, thinking))
            return GOOD % ("onboarding", "2026-10-12")
    understand_request(Spy(""), "<request>", "a@acme.test", {"onboarding": "x"}, "2026-10-08")
    assert seen == [(REASONING["understand"][1], REASONING["understand"][0])]


def test_replay_key_masks_wall_clock_times_only_and_includes_reasoning_mode():
    from rung.llm.client import replay_key
    a = replay_key("s", "Date joined: 2026-10-11 17:41:03 | starts 2026-10-12", False)
    assert a == replay_key("s", "Date joined: 2026-10-12 09:02:55 | starts 2026-10-12", False)   # wall clock masked
    assert a != replay_key("s", "Date joined: 2026-10-11 17:41:03 | starts 2026-10-19", False)   # a plain date is data
    assert a != replay_key("s", "Date joined: 2026-10-11 17:41:03 | starts 2026-10-12", True)
    salt1 = replay_key("s", "algorithm: pbkdf2_sha256 iterations: 1500000 salt: S2LCQK**** hash: EGceUZ****", False)
    assert salt1 == replay_key("s", "algorithm: pbkdf2_sha256 iterations: 1500000 salt: r6K45g**** hash: l5rO+3****", False)
