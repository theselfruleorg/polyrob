"""TDD tests for the unified _translate_error classifier (Task 2.2).

RED first: billing/402/insufficient_quota → LLMPermanentError was only in
adapters.py, not in the base; several token sets were missing on the base.
"""
import logging

import pytest
from unittest.mock import MagicMock, patch

from core.exceptions import (
    LLMError,
    LLMRateLimitError,
    LLMAuthenticationError,
    LLMContextLengthError,
    LLMInvalidRequestError,
    LLMConnectionError,
    LLMPermanentError,
)
from modules.llm.llm_client import translate_llm_error


# ---------------------------------------------------------------------------
# Rate-limit signals
# ---------------------------------------------------------------------------

class TestRateLimitSignals:
    """All rate-limit token variants → LLMRateLimitError."""

    @pytest.mark.parametrize("signal", [
        "429 Too Many Requests",
        "rate limit exceeded",
        "rate_limit",
        "too many requests",
        "quota exceeded",          # from adapters.py 'quota' token
    ])
    def test_rate_limit_signals(self, signal):
        err = Exception(signal)
        result = translate_llm_error(err, "ctx")
        assert isinstance(result, LLMRateLimitError), (
            f"Expected LLMRateLimitError for signal {signal!r}, got {type(result).__name__}"
        )


# ---------------------------------------------------------------------------
# Auth / permission signals
# ---------------------------------------------------------------------------

class TestAuthSignals:
    """All auth token variants → LLMAuthenticationError."""

    @pytest.mark.parametrize("signal", [
        "authentication failed",
        "unauthorized",
        "401 Unauthorized",
        "api_key invalid",
        "invalid key",          # from adapters.py 'invalid key'
    ])
    def test_auth_signals(self, signal):
        err = Exception(signal)
        result = translate_llm_error(err, "ctx")
        assert isinstance(result, LLMAuthenticationError), (
            f"Expected LLMAuthenticationError for signal {signal!r}, got {type(result).__name__}"
        )


# ---------------------------------------------------------------------------
# Billing / quota / 402 signals  (THE KEY NEW BRANCH from adapters.py)
# ---------------------------------------------------------------------------

class TestBillingSignals:
    """Billing/quota/402/insufficient_quota → LLMPermanentError.

    These must NOT fall through to rate-limit or generic LLMError.
    """

    @pytest.mark.parametrize("signal", [
        "insufficient_quota",
        "billing error",
        "account_deactivated",
        "account suspended",
        "402 Payment Required",
    ])
    def test_billing_permanent_error(self, signal):
        err = Exception(signal)
        result = translate_llm_error(err, "ctx")
        assert isinstance(result, LLMPermanentError), (
            f"Expected LLMPermanentError for signal {signal!r}, got {type(result).__name__}"
        )

    def test_billing_not_rate_limit(self):
        """insufficient_quota must be LLMPermanentError, NOT LLMRateLimitError."""
        err = Exception("insufficient_quota")
        result = translate_llm_error(err, "ctx")
        assert not isinstance(result, LLMRateLimitError), (
            "insufficient_quota must NOT be classified as LLMRateLimitError"
        )


# ---------------------------------------------------------------------------
# Context-length signals
# ---------------------------------------------------------------------------

class TestContextLengthSignals:
    @pytest.mark.parametrize("signal", [
        "context_length exceeded",
        "context length too long",
        "maximum context",
    ])
    def test_context_length(self, signal):
        err = Exception(signal)
        result = translate_llm_error(err, "ctx")
        assert isinstance(result, LLMContextLengthError)


# ---------------------------------------------------------------------------
# Connection signals
# ---------------------------------------------------------------------------

class TestConnectionSignals:
    @pytest.mark.parametrize("signal", [
        "connection refused",
        "network error",
        "timeout",
        "unreachable",
    ])
    def test_connection(self, signal):
        err = Exception(signal)
        result = translate_llm_error(err, "ctx")
        assert isinstance(result, LLMConnectionError)


# ---------------------------------------------------------------------------
# Generic unknown error → base LLMError
# ---------------------------------------------------------------------------

class TestGenericError:
    def test_unknown_error_falls_back_to_llm_error(self):
        err = Exception("something completely unknown happened")
        result = translate_llm_error(err, "ctx")
        assert type(result) is LLMError

    def test_context_preserved_in_message(self):
        err = Exception("test error")
        result = translate_llm_error(err, "myctx")
        assert "myctx" in str(result)


# ---------------------------------------------------------------------------
# Finding 2: bare 'rate' token false-positive guard
# ---------------------------------------------------------------------------

class TestBareRateTokenRemoved:
    """Bare 'rate' token must NOT trigger LLMRateLimitError (Finding 2)."""

    def test_moderate_is_not_rate_limit(self):
        """'content was moderated' contains 'rate' as a substring — must NOT be rate-limit."""
        for msg in ("content was moderated", "moderate"):
            err = Exception(msg)
            result = translate_llm_error(err, "ctx")
            assert not isinstance(result, LLMRateLimitError), (
                f"False positive: {msg!r} was mis-classified as LLMRateLimitError"
            )

    def test_accurate_word_not_rate_limit(self):
        """'accurate' contains 'rate' — must NOT be rate-limit."""
        err = Exception("result is accurate")
        result = translate_llm_error(err, "ctx")
        assert not isinstance(result, LLMRateLimitError), (
            "'accurate' must NOT be classified as LLMRateLimitError"
        )

    def test_rate_limit_still_detected(self):
        """Specific rate-limit tokens must still fire after dropping bare 'rate'."""
        for signal in ("rate limit exceeded", "429 too many requests"):
            err = Exception(signal)
            result = translate_llm_error(err, "ctx")
            assert isinstance(result, LLMRateLimitError), (
                f"Expected LLMRateLimitError for {signal!r}, got {type(result).__name__}"
            )


# ---------------------------------------------------------------------------
# Finding 3: bare 'auth' token restored
# ---------------------------------------------------------------------------

class TestBareAuthTokenRestored:
    """Bare 'auth' token must trigger LLMAuthenticationError (Finding 3)."""

    def test_bare_auth_token_detected(self):
        """'auth error' (no 'entication') must be caught as LLMAuthenticationError."""
        err = Exception("auth error")
        result = translate_llm_error(err, "ctx")
        assert isinstance(result, LLMAuthenticationError), (
            f"Expected LLMAuthenticationError for 'auth error', got {type(result).__name__}"
        )


# ---------------------------------------------------------------------------
# Plan-quota exhaustion dressed as a 429 (2026-08-13..16 z.ai storm)
# ---------------------------------------------------------------------------

class TestPlanQuotaExhaustion:
    """A 429 whose body says the PLAN quota is exhausted until a stated reset
    is credit death, not a transient rate limit: retrying it ground 63 dead
    sessions/day for days. Must classify LLMPermanentError."""

    ZAI_1310 = (
        "z.ai GLM Coding Plan API error: Error code: 429 - {'type': 'error', "
        "'error': {'type': 'rate_limit_error', 'code': '1310', 'message': "
        "'[1310][Weekly/Monthly Limit Exhausted. Your limit will reset at "
        "2026-08-18 18:01:49][202608161600034d3278b09a014e95]'}}"
    )

    def test_zai_1310_weekly_limit_is_permanent(self):
        result = translate_llm_error(Exception(self.ZAI_1310), "ctx")
        assert isinstance(result, LLMPermanentError), (
            f"z.ai 1310 quota death must be LLMPermanentError, got {type(result).__name__}"
        )

    def test_insufficient_balance_is_permanent(self):
        err = Exception("Error code: 429 - [1113] insufficient balance")
        result = translate_llm_error(err, "ctx")
        assert isinstance(result, LLMPermanentError)

    def test_transient_429_still_rate_limit(self):
        err = Exception(
            "Error code: 429 - {'type': 'rate_limit_error', "
            "'message': 'concurrency limit reached, please retry later'}")
        result = translate_llm_error(err, "ctx")
        assert isinstance(result, LLMRateLimitError), (
            "a transient 429 without quota-death text must stay retryable"
        )


def test_a_400_that_names_the_key_is_an_authentication_error():
    """Google answers a bad key with a 400."""
    from core.exceptions import LLMAuthenticationError

    class _Bad(Exception):
        status_code = 400

    assert isinstance(translate_llm_error(_Bad("API key not valid. Please pass a valid API key.")),
                      LLMAuthenticationError)
    assert isinstance(translate_llm_error(_Bad("invalid_api_key")), LLMAuthenticationError)


def test_an_unmatched_tool_call_is_an_invalid_request():
    from core.exceptions import LLMInvalidRequestError
    from modules.llm.openai_client import OpenAIClient
    client = OpenAIClient.__new__(OpenAIClient)
    client.logger = logging.getLogger("test.translate")
    calls = [{"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}]
    with pytest.raises(LLMInvalidRequestError):
        client._validate_tool_call_pairs([{"role": "assistant", "tool_calls": calls}])
    with pytest.raises(LLMInvalidRequestError):
        client._validate_tool_call_pairs([{"role": "assistant", "tool_calls": calls}, {"role": "user", "content": "x"}])


@pytest.mark.asyncio
async def test_a_manager_with_no_client_raises_a_configuration_error_with_the_one_message():
    from core.config import BotConfig
    from core.exceptions import LLMConfigError
    from modules.llm.llm_manager import LLMManager
    from modules.llm.profiles import no_key_message
    manager = LLMManager(name="llm", config=BotConfig())
    manager.llm_config = {}
    with pytest.raises(LLMConfigError) as raised:
        await manager._initialize()
    assert str(raised.value) == no_key_message()
