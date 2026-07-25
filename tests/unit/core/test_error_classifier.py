"""Truth-table tests for the structured error classifier (P0 error-taxonomy)."""
import pytest

from core.error_classifier import (
    ClassifiedError,
    FailoverReason,
    classify_error,
    classify_text,
)
from core.exceptions import (
    InsufficientCreditsError,
    LLMAuthenticationError,
    LLMConnectionError,
    LLMContextLengthError,
    LLMError,
    LLMPermanentError,
    LLMProviderExhaustedError,
    LLMRateLimitError,
)

# The verbatim prod 402 shape (mirrors test_error_recovery_sentinel.py's regression body).
_REAL_402 = ("Failed to generate agent response: OpenRouter generation failed: "
             "Error code: 402 - {'error': {'message': 'This request requires more "
             "credits, or fewer max_tokens.'}}")


def test_context_overflow_is_compress_not_fallback():
    c = classify_error(LLMContextLengthError("context length exceeded"))
    assert c.reason is FailoverReason.CONTEXT_OVERFLOW
    assert c.should_compress is True and c.should_fallback is False and c.retryable is True


def test_real_prod_402_is_credit_death():
    c = classify_error(LLMError(_REAL_402))
    assert c.reason is FailoverReason.CREDIT_DEATH
    assert c.should_fallback is True and c.matched_text is not None


def test_credit_death_detected_through_cause_chain():
    # llm_runner re-wraps the 402 as a provider-exhausted error with NO billing text.
    inner = LLMError(_REAL_402)
    try:
        raise inner
    except LLMError:
        outer = LLMProviderExhaustedError("No fallback available after LLMPermanentError")
        outer.__context__ = inner  # Python auto-chains this anyway; explicit for clarity
    c = classify_error(outer)
    assert c.reason is FailoverReason.CREDIT_DEATH  # chain walk finds the inner 402


def test_non_llm_error_with_402_in_text_is_not_credit_death():
    # A tool/parse error whose text merely contains "402" must NOT classify as billing.
    c = classify_error(ValueError("GET /v1/items/402 returned 500"))
    assert c.reason is not FailoverReason.CREDIT_DEATH


# ⚠ AMENDED (validation 2026-07-22): the original third case `LLMError("insufficient_quota
# exceeded")` expected AUTH_PERMANENT, but "insufficient_quota" is in _CREDIT_DEATH_MARKERS
# (core/credit_sentinel.py:36) so the chain-aware credit-death branch (2) fires first for the
# LLM exception family — the test would have FAILED. CREDIT_DEATH is also the faithful verdict:
# live error_recovery.py:245 attempts billing failover on insufficient_quota when the flag is on.
def test_auth_permanent_halts():
    for exc in (LLMAuthenticationError("bad key"),
                LLMPermanentError("account_deactivated"),
                LLMError("invalid_api_key rejected")):
        c = classify_error(exc)
        assert c.reason is FailoverReason.AUTH_PERMANENT
        assert c.retryable is False and c.should_fallback is False


def test_insufficient_quota_is_credit_death_not_auth():
    c = classify_error(LLMError("insufficient_quota exceeded"))
    assert c.reason is FailoverReason.CREDIT_DEATH
    assert c.should_fallback is True


def test_provider_exhausted_is_terminal():
    c = classify_error(LLMProviderExhaustedError("all failed", providers_tried=["a", "b"]))
    assert c.reason is FailoverReason.PROVIDER_EXHAUSTED and c.retryable is False


def test_rate_limit_is_retryable_fallback():
    c = classify_error(LLMRateLimitError("429 rate limit"))
    assert c.reason is FailoverReason.RATE_LIMIT
    assert c.retryable is True and c.should_fallback is True


def test_connection_is_retryable_fallback():
    c = classify_error(LLMConnectionError("connection reset"))
    assert c.reason is FailoverReason.CONNECTION and c.should_fallback is True


def test_unknown_fails_open_retryable():
    c = classify_error(RuntimeError("something weird"))
    assert c.reason is FailoverReason.UNKNOWN
    assert c.retryable is True and c.should_fallback is False and c.should_compress is False


def test_should_rotate_credential_reserved_false():
    assert classify_error(LLMError("boom")).should_rotate_credential is False


def test_classify_text_credit_death_and_exhaustion():
    assert classify_text(_REAL_402) is FailoverReason.CREDIT_DEATH
    assert classify_text("All LLM providers failed. Tried: [...]") is FailoverReason.PROVIDER_EXHAUSTED
    assert classify_text("") is FailoverReason.UNKNOWN


def test_classified_error_is_frozen():
    c = classify_error(LLMError("x"))
    with pytest.raises(Exception):
        c.reason = FailoverReason.UNKNOWN  # frozen dataclass


def test_chain_frame_coincidental_402_does_not_classify_credit_death():
    """Validation fix (2026-07-23): chain frames are marker-matched without a
    type gate, so a coincidental digit-run/hex "402" in ANY chained exception
    flipped a retryable step fatal when billing failover was off. With the
    boundary-aware 402 marker the coincidental class no longer matches; a
    genuine chained provider 402 still does."""
    e = LLMError("timeout after 30s")
    e.__context__ = RuntimeError("upstream call failed (request_id: req_9f402ab13c7e)")
    assert classify_error(e).reason is not FailoverReason.CREDIT_DEATH

    real = LLMError("provider call failed")
    real.__context__ = RuntimeError("Error code: 402 - requires more credits")
    assert classify_error(real).reason is FailoverReason.CREDIT_DEATH


def test_classify_text_budget_halt_is_not_credit_death():
    """A RUN_BUDGET_USD halt embeds dollar amounts ("$0.4020") that satisfied
    the bare-substring 402 marker — a budget halt must never read as provider
    credit death (it drove the wrong owner phrase and a provider_outage label)."""
    assert classify_text(
        "Session failed: run_budget_exhausted: session provider spend $0.4020 "
        "reached RUN_BUDGET_USD $0.40") is not FailoverReason.CREDIT_DEATH
