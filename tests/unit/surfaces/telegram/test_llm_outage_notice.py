"""Proposal 015 #2: static LLM-outage notice on the chat-surface deliver seam.

2026-07-16 incident: the owner asked "what did you spend money on?" via
Telegram, OpenRouter 402'd, retries exhausted ("ALL LLM PROVIDERS EXHAUSTED"),
session 4883c075 ended failed — zero outbound sends of any kind. The fix: when
an owner-facing chat-surface turn dies of total LLM-provider failure,
``_run_and_deliver`` sends ONE static, LLM-independent ⚠️ notice over the
originating surface — kill-switch ``LLM_OUTAGE_NOTICE`` (default ON), 30-min
per-surface+chat cooldown, and structurally never for goal/cron runs (those
call ``run_session`` directly, bypassing this seam).
"""
import pytest

from core.surfaces import llm_outage_notice
from core.surfaces.llm_outage_notice import (
    LLM_OUTAGE_COOLDOWN_SEC,
    OUTAGE_NOTICE_TEXT,
    looks_like_llm_outage,
    reset_llm_outage_notice_state,
    should_send_llm_outage_notice,
)
from surfaces.telegram import harness


@pytest.fixture(autouse=True)
def _reset_state():
    reset_llm_outage_notice_state()
    yield
    reset_llm_outage_notice_state()


EXHAUSTED_STATUS = "Session failed: All LLM providers exhausted: ['openrouter']"


class _FakeAgent:
    """Chat-surface-shaped TaskAgent double (mirrors test_run_and_deliver.py)."""

    def __init__(self, status=EXHAUSTED_STATUS, reply="", orch=None):
        self.status, self.reply, self.orch = status, reply, orch
        self.ran = []

    async def run_session(self, user_id, session_id):
        self.ran.append((user_id, session_id))
        return self.status

    def get_orchestrator(self, session_id):
        return self.orch

    def _extract_chat_reply(self, session_id):
        return self.reply


async def _drive(agent, session_id="sess-1", notice_key="agent:main:telegram:dm:42:rob"):
    sent = []

    async def deliver(text):
        sent.append(text)

    await harness._run_and_deliver(agent, "rob", session_id, deliver,
                                   notice_key=notice_key)
    return sent


# --- the four contract tests from the task/proposal test plan -----------------


@pytest.mark.asyncio
async def test_exhaustion_failure_sends_exactly_one_static_notice():
    """A chat-surface turn ending in provider exhaustion produces exactly one
    outbound static notice on the originating surface."""
    sent = await _drive(_FakeAgent())
    assert sent == [OUTAGE_NOTICE_TEXT]
    # Clearly NOT an agent reply: ⚠️-prefixed and self-labelled.
    assert sent[0].startswith("⚠️")
    assert "not a reply" in sent[0]


@pytest.mark.asyncio
async def test_second_failure_within_cooldown_sends_nothing():
    """A 402 storm must not spam: at most ONE notice per surface+chat per 30 min."""
    key = "agent:main:telegram:dm:42:rob"
    first = await _drive(_FakeAgent(), notice_key=key)
    second = await _drive(_FakeAgent(), notice_key=key)
    assert first == [OUTAGE_NOTICE_TEXT]
    assert second == []


@pytest.mark.asyncio
async def test_goal_run_failure_sends_nothing():
    """Goal/cron/background runs call run_session DIRECTLY (goals/dispatcher.py,
    cron/runner.py) — they never route through the chat-surface deliver seam, so
    an exhaustion failure there produces no notice and never touches the
    cooldown state."""
    agent = _FakeAgent()
    status = await agent.run_session("rob", "goal-sess-1")  # the goal-path shape
    assert status == EXHAUSTED_STATUS
    assert llm_outage_notice._last_notice_at == {}, (
        "the goal-run path must not touch the outage-notice seam"
    )


@pytest.mark.asyncio
async def test_flag_off_sends_nothing(monkeypatch):
    """LLM_OUTAGE_NOTICE=off is the kill switch: no static notice at all."""
    monkeypatch.setenv("LLM_OUTAGE_NOTICE", "off")
    sent = await _drive(_FakeAgent())
    assert sent == []


# --- classification coverage --------------------------------------------------


class _Result:
    def __init__(self, error):
        self.error = error


class _HistItem:
    def __init__(self, error):
        self.result = [_Result(error)]


class _Hist:
    def __init__(self, error):
        self.history = [_HistItem(error)]


class _ExecAgent:
    def __init__(self, error):
        self.history = _Hist(error)


class _Orch:
    """Unbound orch (no router) carrying the executor agent's ledger."""

    _message_router = None
    _chat_session_key = None

    def __init__(self, error):
        self.agents = {"executor_s": _ExecAgent(error)}


@pytest.mark.asyncio
async def test_unknown_error_status_classified_via_terminal_action_result():
    """The in-loop halt path surfaces as a bare 'Session failed: Unknown error'
    (the agent-result dict has no 'error' key) — classification must fall back
    to the ledger's terminal ActionResult error ('PERMANENT ERROR: … 402 …')."""
    orch = _Orch(
        "PERMANENT ERROR: OpenRouter generation failed: Error code: 402 - "
        "This request requires more credits. Session halted."
    )
    sent = await _drive(_FakeAgent(status="Session failed: Unknown error", orch=orch))
    assert sent == [OUTAGE_NOTICE_TEXT]


@pytest.mark.asyncio
async def test_non_outage_failure_keeps_legacy_generic_notice():
    """A failed run that is NOT an LLM outage still gets the pre-existing generic
    'couldn't answer' notice (unchanged legacy behaviour) and never consumes the
    outage cooldown."""
    sent = await _drive(_FakeAgent(status="Session failed: Unknown error"))
    assert len(sent) == 1
    assert sent[0] != OUTAGE_NOTICE_TEXT
    assert "couldn't answer" in sent[0]
    assert llm_outage_notice._last_notice_at == {}


def test_classifier_matches_all_known_exhaustion_shapes():
    assert looks_like_llm_outage("Session failed: All LLM providers exhausted: []")
    assert looks_like_llm_outage("All LLM providers failed. Tried: ['openrouter']")
    assert looks_like_llm_outage("No fallback available after LLMPermanentError")
    assert looks_like_llm_outage("Session failed: Permanent LLM error: quota gone")
    assert looks_like_llm_outage("OpenRouter generation failed: Error code: 402")
    assert looks_like_llm_outage(
        "Session suspended: Insufficient credits. Add credits to resume.")
    assert not looks_like_llm_outage("Session failed: browser crashed")
    assert not looks_like_llm_outage(None, "", "Session completed successfully")


def test_cooldown_window_expires():
    key = "agent:main:telegram:dm:42:rob"
    assert should_send_llm_outage_notice(key, now=1000.0)
    assert not should_send_llm_outage_notice(key, now=1000.0 + 100)
    # A DIFFERENT chat has its own bucket.
    assert should_send_llm_outage_notice("agent:main:slack:dm:7:rob", now=1000.0 + 100)
    # The original chat frees up after the window.
    assert should_send_llm_outage_notice(key, now=1000.0 + LLM_OUTAGE_COOLDOWN_SEC + 1)
