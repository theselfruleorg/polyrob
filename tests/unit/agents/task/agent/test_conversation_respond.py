import pytest
from agents.task.agent.conversation import Conversation


class _StubAgent:
    def __init__(self): self.injected = []; self.runs = []; self.continue_flags = []
    def set_turn_input(self, text): self.injected.append(text)
    async def run(self, max_steps=100, _continue_session=False):
        self.runs.append(max_steps)
        self.continue_flags.append(_continue_session)
        class _R:
            is_done = True; extracted_content = f"reply to: {self.injected[-1]}"
        class _H:
            def __iter__(self_inner): return iter([_R()])
        return _H()


@pytest.mark.asyncio
async def test_respond_returns_answer_and_records_turn():
    a = _StubAgent()
    convo = Conversation(a)
    out = await convo.respond("hello", max_steps=4)
    assert out == "reply to: hello"
    assert a.runs == [4]                 # small per-turn budget, not 100
    assert len(convo.turns) == 1
    assert convo.turns[0].user == "hello" and convo.turns[0].assistant == out


@pytest.mark.asyncio
async def test_first_turn_starts_session_then_continues():
    a = _StubAgent()
    convo = Conversation(a)
    await convo.respond("one")
    await convo.respond("two")
    # First turn runs the session preamble; subsequent turns skip it.
    assert a.continue_flags == [False, True]


class _State:
    def __init__(self): self.consecutive_failures = 5


class _WedgedAgent(_StubAgent):
    """Simulates an agent left in a failed state by a prior turn."""
    def __init__(self):
        super().__init__()
        self.state = _State()
        self._cancelled = True


@pytest.mark.asyncio
async def test_respond_resets_failed_state_for_recovery():
    a = _WedgedAgent()
    convo = Conversation(a)
    out = await convo.respond("retry please")
    # respond() clears the wedged state so a new turn gets a fresh attempt.
    assert a.state.consecutive_failures == 0
    assert a._cancelled is False
    assert out == "reply to: retry please"
