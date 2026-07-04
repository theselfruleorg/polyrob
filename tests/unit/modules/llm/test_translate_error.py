"""TDD tests for the unified _translate_error classifier (Task 2.2).

RED first: billing/402/insufficient_quota → LLMPermanentError was only in
adapters.py, not in the base; several token sets were missing on the base.
"""
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
# LLMClient._translate_error delegates to translate_llm_error
# ---------------------------------------------------------------------------

class TestLLMClientTranslateError:
    """LLMClient._translate_error must produce same results as translate_llm_error."""

    def _make_client(self):
        """Build a minimal concrete LLMClient without full init."""
        from modules.llm.llm_client import LLMClient

        # Silence abstract enforcement so we can instantiate the base class directly.
        class _Concrete(LLMClient):
            async def _setup_client(self): pass
            async def _validate_connection(self): pass
            async def _make_validation_request(self): pass
            def _check_validation_response(self, r): pass
            async def _cleanup_client(self): pass
            async def _generate(self, *a, **kw): pass
            async def generate_response(self, *a, **kw): pass

        client = object.__new__(_Concrete)
        client.logger = MagicMock()
        return client

    def test_client_method_billing(self):
        client = self._make_client()
        result = client._translate_error(Exception("insufficient_quota"), "test")
        assert isinstance(result, LLMPermanentError)

    def test_client_method_rate_limit(self):
        client = self._make_client()
        result = client._translate_error(Exception("429 too many requests"), "test")
        assert isinstance(result, LLMRateLimitError)

    def test_client_method_generic(self):
        client = self._make_client()
        result = client._translate_error(Exception("oops"), "test")
        assert isinstance(result, LLMError)


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
