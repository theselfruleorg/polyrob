"""Task 14 (CO-F5/CO-F6): error-recovery routing.

CO-F6: LLMContextLengthError must checkpoint + emergency-prune (never a blind
provider-fallback retry with the same oversized history).
Only a typed LLM error gets a provider fallback or a permanent halt: an application
error's text decides neither.
"""
import logging
import sqlite3

import pytest
from unittest.mock import AsyncMock, MagicMock

from agents.task.agent.service import Agent
from core.exceptions import (
    LLMAuthenticationError,
    LLMConfigError,
    LLMContextLengthError,
    LLMError,
    LLMPermanentError,
)


def _build_agent():
    a = object.__new__(Agent)
    a.logger = logging.getLogger("test_error_recovery_routing")
    a.max_failures = 5

    st = MagicMock()
    st.consecutive_failures = 0
    st.stopped = False
    a.state = st

    mm = MagicMock()
    a.message_manager = mm
    a.model_name = "gpt-4o"

    a.controller = MagicMock()
    a.telemetry_manager = MagicMock()

    a._recover_from_error = AsyncMock()
    a._get_provider_from_model = MagicMock(return_value="openai")
    a._attempt_llm_fallback_in_handler = AsyncMock(return_value=True)
    return a


@pytest.mark.asyncio
async def test_context_length_error_checkpoints_and_prunes_not_fallback():
    a = _build_agent()

    error = LLMContextLengthError("context length exceeded")

    result = await a._handle_step_error(error)

    a.message_manager.checkpoint_history.assert_called_once()
    a.message_manager.emergency_context_prune.assert_called_once()
    a._attempt_llm_fallback_in_handler.assert_not_awaited()
    a._recover_from_error.assert_not_awaited()
    # Retry next step: empty ActionResult list (not a halt).
    assert result == []
    assert a.state.stopped is False


@pytest.mark.asyncio
async def test_context_length_error_increments_consecutive_failures_each_call():
    """CO-F5 follow-up: a repeatedly un-prunable oversized message must accumulate
    consecutive_failures on every entry to the LLMContextLengthError branch, so
    _too_many_failures()/the circuit breaker eventually trips instead of the
    no-op prune silently re-firing forever."""
    a = _build_agent()
    error = LLMContextLengthError("context length exceeded")

    for expected in range(1, 4):
        result = await a._handle_step_error(error)
        assert a.state.consecutive_failures == expected
        assert result == []

    # A handful of overflows shouldn't halt by themselves — only the
    # existing _too_many_failures()/circuit-breaker ceiling should do that.
    assert a.state.stopped is False


@pytest.mark.parametrize("error", [
    RuntimeError("tool returned 429 too many requests"),
    RuntimeError("rate limit exceeded, please retry"),
    RuntimeError("browser connection closed"),
    RuntimeError("timeout reading the llm notes file"),
    sqlite3.OperationalError("no such column: billing_total"),
    NameError("name 'billing' is not defined"),
    TypeError("insufficient_quota is not a str"),
], ids=lambda e: str(e)[:24])
@pytest.mark.asyncio
async def test_an_application_error_gets_no_provider_fallback_and_no_halt(error):
    a = _build_agent()
    a.state.consecutive_failures = 1
    a.max_failures = 5
    a.retry_delay = 0
    await a._handle_step_error(error)
    a._attempt_llm_fallback_in_handler.assert_not_awaited()
    a.state.request_stop.assert_not_called()


@pytest.mark.parametrize("error", [
    LLMAuthenticationError("bad key"),
    LLMPermanentError("model not found"),
    LLMConfigError("no key"),
    LLMError("Error code: 402 - insufficient_quota"),
], ids=lambda e: type(e).__name__)
@pytest.mark.asyncio
async def test_a_typed_permanent_error_halts(error, monkeypatch):
    monkeypatch.setenv("BILLING_FAILOVER_ENABLED", "0")
    a = _build_agent()
    a.max_failures = 5
    await a._handle_step_error(error)
    a.state.request_stop.assert_called_once()
