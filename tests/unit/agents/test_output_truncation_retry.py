"""057 WS-B: a cut-off answer is re-run once with the reason named."""
import types

import pytest

from agents.task.agent.core.output_truncation import (
    build_truncation_note, handle_truncated_output, truncation_retry_enabled,
)


class _Client:
    def __init__(self, reason, tokens=None):
        usage = types.SimpleNamespace(completion_tokens=tokens)
        self.last_response = types.SimpleNamespace(
            choices=[types.SimpleNamespace(finish_reason=reason)], usage=usage)


class _Agent:
    user_id = "u1"
    session_id = "s1"
    model_name = "test-model"

    def __init__(self, reason, tokens=None):
        self.llm = types.SimpleNamespace(_client=_Client(reason, tokens))
        self.state = types.SimpleNamespace(n_steps=3)
        self.retries = []

    async def _get_next_action_internal(self, messages):
        self.retries.append(messages)
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

    async def _boom(messages):
        raise RuntimeError("provider down")

    agent._get_next_action_internal = _boom
    assert await handle_truncated_output(agent, ["m"], 90.0, "FIRST") == "FIRST"


def test_the_note_is_a_typed_control_message():
    from modules.llm.messages import MessageOrigin
    note = build_truncation_note(8192)
    assert getattr(note, "origin", None) == MessageOrigin.INTERVENTION
    assert "8,192" in note.content


# --- 2026-09-20 06:40Z: the cut is REASONING, not a long write --------------------------

class _ReasoningClient(_Client):
    """OpenRouter/DeepSeek shape: usage.completion_tokens_details.reasoning_tokens."""
    def __init__(self, reason, tokens, reasoning):
        super().__init__(reason, tokens)
        self.last_response.usage.completion_tokens_details = types.SimpleNamespace(
            reasoning_tokens=reasoning)


def test_reasoning_tokens_are_read_from_the_last_response():
    from modules.llm.output_budget import last_reasoning_tokens
    llm = types.SimpleNamespace(_client=_ReasoningClient("length", 8192, 7981))
    assert last_reasoning_tokens(llm) == 7981
    plain = types.SimpleNamespace(_client=_Client("length", 8192))
    assert last_reasoning_tokens(plain) is None               # not reported → unknown, never 0
    as_dict = types.SimpleNamespace(_client=types.SimpleNamespace(last_response={
        "choices": [{"finish_reason": "length"}],
        "usage": {"completion_tokens": 8192, "completion_tokens_details": {"reasoning_tokens": 8000}}}))
    assert last_reasoning_tokens(as_dict) == 8000


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
    agent = _Agent("length", 8192)
    agent.llm = types.SimpleNamespace(_client=_ReasoningClient("length", 8192, 7981))
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

