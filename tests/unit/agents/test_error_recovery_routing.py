"""Task 14 (CO-F5/CO-F6): error-recovery routing.

CO-F6: LLMContextLengthError must checkpoint + emergency-prune (never a blind
provider-fallback retry with the same oversized history).
CO-F5: a tool-originated '429'/'rate limit' string must NOT be classified as
fatal by `_is_fatal_step_error` — it should reach `_handle_step_error`'s
graceful rate-limit branch (circuit breaker / fallback / backoff) instead of
halting the session immediately.
"""
import logging

import pytest
from unittest.mock import AsyncMock, MagicMock

from agents.task.agent.service import Agent
from agents.task.agent.core.step import _is_fatal_step_error
from core.exceptions import LLMContextLengthError


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


def test_tool_429_is_not_fatal():
    assert _is_fatal_step_error("tool returned 429 too many requests", billing_failover_enabled=False) is False


def test_rate_limit_string_is_not_fatal():
    assert _is_fatal_step_error("rate limit exceeded, please retry", billing_failover_enabled=False) is False


def test_auth_error_is_still_fatal():
    assert _is_fatal_step_error("invalid api key provided", billing_failover_enabled=False) is True


def test_authentication_error_is_still_fatal():
    assert _is_fatal_step_error("authentication failed", billing_failover_enabled=False) is True


def test_quota_exceeded_is_still_fatal():
    assert _is_fatal_step_error("quota exceeded for this account", billing_failover_enabled=False) is True


def test_billing_without_failover_is_still_fatal():
    assert _is_fatal_step_error("billing issue on account", billing_failover_enabled=False) is True


def test_billing_with_failover_enabled_is_not_fatal():
    assert _is_fatal_step_error("billing issue on account", billing_failover_enabled=True) is False
