"""The credit-death chain reader and the terminal-status reader. The classifier itself:
tests/unit/modules/llm/test_translate_error.py and test_llm_error_sdk_shapes.py."""
from core.error_classifier import (
    FailoverReason,
    classify_text,
    credit_death_in_chain,
)
from core.exceptions import (
    InsufficientCreditsError,
    LLMError,
    LLMProviderExhaustedError,
)

# The verbatim prod 402 shape (mirrors test_error_recovery_sentinel.py's regression body).
_REAL_402 = ("Failed to generate agent response: OpenRouter generation failed: "
             "Error code: 402 - {'error': {'message': 'This request requires more "
             "credits, or fewer max_tokens.'}}")


def test_real_prod_402_is_credit_death():
    assert credit_death_in_chain(LLMError(_REAL_402)) == _REAL_402


def test_credit_death_detected_through_cause_chain():
    inner = LLMError(_REAL_402)
    outer = LLMProviderExhaustedError("All LLM providers exhausted")
    outer.__context__ = inner
    assert credit_death_in_chain(outer) == _REAL_402


def test_a_chain_raised_from_none_is_not_read():
    """llm_runner re-raises the primary's error after a failed fallback, from None: the
    fallback's own 402 says nothing of the primary's account."""
    primary = LLMError("connection reset")
    primary.__context__ = LLMError(_REAL_402)
    primary.__suppress_context__ = True
    assert credit_death_in_chain(primary) is None


def test_non_llm_error_with_402_in_text_is_not_credit_death():
    # A tool/parse error whose text merely contains "402" must NOT classify as billing.
    assert credit_death_in_chain(ValueError("GET /v1/items/402 returned 500")) is None


def test_insufficient_credits_is_in_the_family():
    assert credit_death_in_chain(InsufficientCreditsError("u", 5, 1)) is not None


def test_classify_text_credit_death_and_exhaustion():
    assert classify_text(_REAL_402) is FailoverReason.CREDIT_DEATH
    assert classify_text("All LLM providers failed. Tried: [...]") is FailoverReason.PROVIDER_EXHAUSTED
    assert classify_text("") is FailoverReason.UNKNOWN


def test_chain_frame_coincidental_402_does_not_classify_credit_death():
    """Validation fix (2026-07-23): chain frames are marker-matched without a
    type gate, so a coincidental digit-run/hex "402" in ANY chained exception
    flipped a retryable step fatal when billing failover was off. With the
    boundary-aware 402 marker the coincidental class no longer matches; a
    genuine chained provider 402 still does."""
    e = LLMError("timeout after 30s")
    e.__context__ = RuntimeError("upstream call failed (request_id: req_9f402ab13c7e)")
    assert credit_death_in_chain(e) is None

    real = LLMError("provider call failed")
    real.__context__ = RuntimeError("Error code: 402 - requires more credits")
    assert credit_death_in_chain(real) is not None


def test_classify_text_budget_halt_is_not_credit_death():
    """A RUN_BUDGET_USD halt embeds dollar amounts ("$0.4020") that satisfied
    the bare-substring 402 marker — a budget halt must never read as provider
    credit death (it drove the wrong owner phrase and a provider_outage label)."""
    assert classify_text(
        "Session failed: run_budget_exhausted: session provider spend $0.4020 "
        "reached RUN_BUDGET_USD $0.40") is not FailoverReason.CREDIT_DEATH
