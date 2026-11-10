"""057 WS-B: a cut-off answer is re-run once with the reason named."""
import types

import pytest

from modules.llm.call_result import CallFacts
from agents.task.agent.core.output_truncation import (
    build_truncation_note, handle_truncated_output, truncation_retry_enabled,
)


class _Agent:
    user_id = "u1"
    session_id = "s1"
    model_name = "test-model"

    def __init__(self, reason, tokens=None, reasoning=None):
        self.last_call_facts = CallFacts(finish_reason=reason, output_tokens=tokens,
                                         reasoning_tokens=reasoning)
        self.state = types.SimpleNamespace(n_steps=3)
        self.retries = []

    async def _get_next_action_internal(self, messages, extra_messages=None):
        # The request is rebuilt from the manager; only extra_messages reach it.
        self.retries.append(list(extra_messages or []))
        return "RETRIED"


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.delenv("LLM_OUTPUT_TRUNCATION_RETRY", raising=False)
    monkeypatch.setenv("TELEMETRY_EVENT_LOG_PATH", str(tmp_path / "ev.db"))


def test_off_by_default():
    assert truncation_retry_enabled() is False


@pytest.mark.asyncio
async def test_a_clean_stop_is_untouched():
    agent = _Agent("stop", 120)
    assert await handle_truncated_output(agent, ["m"], 90.0, "FIRST") == "FIRST"
    assert agent.retries == []


@pytest.mark.asyncio
async def test_truncation_without_the_flag_keeps_the_answer(caplog):
    agent = _Agent("length", 16384)
    assert await handle_truncated_output(agent, ["m"], 90.0, "FIRST") == "FIRST"
    assert agent.retries == [], "no retry while the flag is off"


@pytest.mark.asyncio
async def test_truncation_retries_once_with_the_reason_named(monkeypatch):
    monkeypatch.setenv("LLM_OUTPUT_TRUNCATION_RETRY", "true")
    agent = _Agent("length", 16384)
    out = await handle_truncated_output(agent, ["m"], 90.0, "FIRST")
    assert out == "RETRIED"
    assert len(agent.retries) == 1
    injected = agent.retries[0][-1]
    text = str(getattr(injected, "content", injected))
    assert "CUT OFF" in text and "16,384" in text
    assert "two or more calls" in text, "the remedy must be named"


@pytest.mark.asyncio
async def test_a_failing_retry_falls_back_honestly(monkeypatch):
    monkeypatch.setenv("LLM_OUTPUT_TRUNCATION_RETRY", "true")
    agent = _Agent("length", 16384)

    async def _boom(messages, extra_messages=None):
        raise RuntimeError("provider down")

    agent._get_next_action_internal = _boom
    assert await handle_truncated_output(agent, ["m"], 90.0, "FIRST") == "FIRST"


def test_the_note_is_a_typed_control_message():
    from modules.llm.messages import MessageOrigin
    note = build_truncation_note(8192)
    assert getattr(note, "origin", None) == MessageOrigin.INTERVENTION
    assert "8,192" in note.content


# --- 2026-09-20 06:40Z: the cut is REASONING, not a long write --------------------------

def test_the_facts_are_this_sessions_own(monkeypatch):
    """S2: another session's call on the shared client cannot change what this step reads."""
    from modules.llm.call_result import output_was_truncated
    assert output_was_truncated(_Agent("length", 8192).last_call_facts) is True
    assert output_was_truncated(_Agent("stop", 8192).last_call_facts) is False
    no_call = types.SimpleNamespace()
    assert output_was_truncated(getattr(no_call, "last_call_facts", None)) is False


@pytest.mark.asyncio
async def test_reasoning_bound_cut_gets_a_different_note_and_is_counted_apart(monkeypatch):
    """Prod 2026-09-20: SAFETY step 2 (a read_file) was cut at 8,192 with 7,981
    reasoning tokens; SCOUT step 4 at 8,192/8,192. The 'write the file in two
    calls' note is the wrong diagnosis when the model never reached its tool
    call — say what happened, and separate the count."""
    monkeypatch.setenv("LLM_OUTPUT_TRUNCATION_RETRY", "true")
    from agents.task.agent.core.output_truncation import build_truncation_note, reasoning_bound
    assert reasoning_bound(8192, 7981) is True
    assert reasoning_bound(8192, 3000) is False
    assert reasoning_bound(8192, None) is False               # unknown → the write note
    agent = _Agent("length", 8192, reasoning=7981)
    events = []
    monkeypatch.setattr("core.event_log.emit", lambda kind, **kw: events.append((kind, kw)))
    out = await handle_truncated_output(agent, ["m"], 90.0, "FIRST")
    assert out == "RETRIED"
    text = str(getattr(agent.retries[0][-1], "content", ""))
    assert "reasoning" in text.lower() and "two or more calls" not in text
    assert "decide" in text.lower() and "call the tool" in text.lower()
    assert events and events[0][0] == "llm_output_truncated"
    attrs = events[0][1]["attrs"]
    assert attrs["reasoning_bound"] is True and attrs["reasoning_tokens"] == 7981
    # the write note is unchanged for a genuinely long answer
    note = build_truncation_note(8192, reasoning_tokens=1200)
    assert "two or more calls" in note.content



# --- coherency review 2026-10-13 E-A8: with the retry OFF the cut was silent.

class _MM:
    def __init__(self):
        self.ephemeral = []

    def push_ephemeral_message(self, message):
        self.ephemeral.append(message)


@pytest.mark.asyncio
async def test_flag_off_still_tells_the_next_step_about_the_cut():
    agent = _Agent("length", 16384)
    agent.message_manager = _MM()
    assert await handle_truncated_output(agent, ["m"], 90.0, "FIRST") == "FIRST"
    assert agent.retries == []
    assert len(agent.message_manager.ephemeral) == 1
    note = agent.message_manager.ephemeral[0].content
    assert "CUT OFF at 16,384" in note
    assert "It was used as it was" in note
    assert "Nothing from it was used" not in note


@pytest.mark.asyncio
async def test_a_clean_stop_pushes_no_note():
    agent = _Agent("stop", 120)
    agent.message_manager = _MM()
    await handle_truncated_output(agent, ["m"], 90.0, "FIRST")
    assert agent.message_manager.ephemeral == []


@pytest.mark.asyncio
async def test_flag_on_retry_does_not_also_push_the_used_note(monkeypatch):
    monkeypatch.setenv("LLM_OUTPUT_TRUNCATION_RETRY", "true")
    agent = _Agent("length", 16384)
    agent.message_manager = _MM()
    assert await handle_truncated_output(agent, ["m"], 90.0, "FIRST") == "RETRIED"
    assert agent.message_manager.ephemeral == []


# --- prod 2026-11-07: four 16,384-token calls in a row, each spent on reasoning alone.

class _LevelAgent(_Agent):
    async def _get_next_action_internal(self, messages, extra_messages=None):
        from modules.llm.call_scope import session_reasoning_level
        self.retries.append(session_reasoning_level())
        return "RETRIED"


@pytest.mark.asyncio
async def test_a_reasoning_bound_cut_retries_with_thinking_off_for_that_call(monkeypatch):
    from modules.llm.call_scope import session_reasoning, session_reasoning_level
    monkeypatch.setenv("LLM_OUTPUT_TRUNCATION_RETRY", "true")
    agent = _LevelAgent("length", 16384, reasoning=16384)
    with session_reasoning("high"):
        assert await handle_truncated_output(agent, ["m"], 90.0, "FIRST") == "RETRIED"
        assert session_reasoning_level() == "high", "the owner's level returns after the call"
    assert agent.retries == ["off"]


@pytest.mark.asyncio
async def test_a_long_answer_cut_keeps_the_level_in_force(monkeypatch):
    from modules.llm.call_scope import session_reasoning
    monkeypatch.setenv("LLM_OUTPUT_TRUNCATION_RETRY", "true")
    agent = _LevelAgent("length", 16384, reasoning=100)
    with session_reasoning("high"):
        await handle_truncated_output(agent, ["m"], 90.0, "FIRST")
    assert agent.retries == ["high"]


def test_a_cut_call_is_not_reissued_as_an_empty_one():
    from agents.task.agent.core.call_quality import worth_empty_retry
    empty = types.SimpleNamespace(content="", tool_calls=None,
                                  call_facts=CallFacts(finish_reason="stop", output_tokens=0))
    cut = types.SimpleNamespace(content="", tool_calls=None,
                                call_facts=CallFacts(finish_reason="length", output_tokens=16384))
    assert worth_empty_retry(empty) is True
    assert worth_empty_retry(cut) is False
    assert worth_empty_retry(types.SimpleNamespace(content="", tool_calls=None)) is True
