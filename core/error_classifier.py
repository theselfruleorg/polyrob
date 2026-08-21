"""Structured LLM-error classifier — the single source of truth for how the step loop
and the outage notice categorize a failure (P0, 2026-07-21 structural review).

Unifies the two top-string classification sites (agents/task/agent/core/step.py
::_is_fatal_step_error and error_recovery.py::_handle_step_error's inline substring
branches) behind ONE chain-aware verdict. Pure / side-effect-free; lives in core/ so
both the agent loop (agents.task) and the surface notice (core.surfaces) import it
downward. Reuses core.credit_sentinel.looks_like_credit_death as the credit-death SSOT
and walks the __cause__/__context__ chain (the sentinel already does; the control-flow
classification historically did not — that asymmetry is what this closes).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional

from pydantic import ValidationError

from core.credit_sentinel import looks_like_credit_death
from core.exceptions import (
    InsufficientCreditsError,
    LLMAuthenticationError,
    LLMConnectionError,
    LLMContextLengthError,
    LLMError,
    LLMPermanentError,
    LLMProviderExhaustedError,
    LLMRateLimitError,
    RateLimitError,
)

try:
    from google.api_core.exceptions import ResourceExhausted
except ImportError:  # google-api-core not installed (mirrors error_recovery.py:19-23)
    class ResourceExhausted(Exception):  # type: ignore[no-redef]
        pass

_MAX_CHAIN_DEPTH = 8

# Intrinsic permanent (non-credit) markers — mirrors error_recovery.py:231-238's
# non-credit permanent set. Credit-death is handled separately + chain-aware.
_AUTH_PERMANENT_MARKERS = ("insufficient_quota", "invalid_api_key", "account_deactivated")

# Terminal-status markers meaning "the run died from provider exhaustion" — migrated
# from core/surfaces/llm_outage_notice.py::_EXHAUSTION_MARKERS so both read one SSOT.
_EXHAUSTION_TEXT_MARKERS = (
    "all llm providers",
    "providers exhausted",
    "no fallback available",
    "permanent llm error",
    "permanent error",
    "llmpermanenterror",
    "llmproviderexhaustederror",
)


class FailoverReason(str, enum.Enum):
    """Why a step failed — the recovery-policy discriminant. String-valued to match the
    AccessTier/SendDecision/RouteKind convention (core/surfaces/*)."""
    CONTEXT_OVERFLOW = "context_overflow"
    CREDIT_DEATH = "credit_death"
    AUTH_PERMANENT = "auth_permanent"
    PROVIDER_EXHAUSTED = "provider_exhausted"
    RATE_LIMIT = "rate_limit"
    CONNECTION = "connection"
    VALIDATION = "validation"
    GENERIC_LLM = "generic_llm"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ClassifiedError:
    """A structured verdict + recovery hints (modeled on the reference implementation; local
    template: tools/controller/delegation.py::DelegationDecision). ``matched_text`` is the
    chain frame that decided a credit-death/permanent verdict (for logs / the sentinel)."""
    reason: FailoverReason
    retryable: bool                 # may retry the step (with backoff)
    should_compress: bool           # prompt too large — compact, do NOT fallback
    should_fallback: bool           # attempt a provider swap
    should_rotate_credential: bool  # reserved: POLYROB has no credential pool — always False today
    matched_text: Optional[str] = None


def _iter_chain(error: BaseException):
    seen: set = set()
    e: Optional[BaseException] = error
    while e is not None and id(e) not in seen and len(seen) < _MAX_CHAIN_DEPTH:
        seen.add(id(e))
        yield e
        e = e.__cause__ or e.__context__


def _credit_death_in_chain(error: BaseException) -> Optional[str]:
    for e in _iter_chain(error):
        if looks_like_credit_death(str(e)):
            return str(e)
    return None


def classify_error(error: BaseException) -> ClassifiedError:
    """Classify a step-loop exception into a structured recovery verdict. Chain-aware:
    walks __cause__/__context__ so a billing 402 re-wrapped as
    LLMProviderExhaustedError('No fallback available') is still seen as credit death.

    Branch order: context-overflow → credit-death → auth-permanent → provider-exhausted
    → rate-limit → connection → validation → generic-LLM → unknown.

    Fidelity note: only the CREDIT_DEATH verdict is characterization-locked to the
    consuming sites (step.py's fatal check and _handle_step_error's billing pre-check).
    The other verdicts are best-effort taxonomy, NOT yet behavior-locked to
    _handle_step_error's live branch order or string sets — e.g. the handler halts a
    bare-string 'billing' on ANY exception type, while this classifier type-gates
    credit-death to the LLM family. Do not re-point the handler's is_permanent /
    is_llm_error branches at these verdicts without first writing characterization
    tests against the handler's real outcomes (the planned string-set-unification
    follow-on).
    """
    # 1. Context overflow — needs a smaller prompt, never a provider swap.
    if isinstance(error, LLMContextLengthError):
        return ClassifiedError(FailoverReason.CONTEXT_OVERFLOW, retryable=True,
                               should_compress=True, should_fallback=False,
                               should_rotate_credential=False)

    # 2. Credit death (chain-aware) — gated on the LLM/credit exception family, matching
    #    _trip_sentinel_if_credit_death's type gate so a tool/parse error whose text merely
    #    contains "402" never classifies as billing.
    if isinstance(error, (LLMError, InsufficientCreditsError)):
        matched = _credit_death_in_chain(error)
        if matched is not None:
            return ClassifiedError(FailoverReason.CREDIT_DEATH, retryable=True,
                                   should_compress=False, should_fallback=True,
                                   should_rotate_credential=False, matched_text=matched)

    low = str(error).lower()

    # 3. Permanent auth/account — intrinsic halt, no fallback.
    if isinstance(error, (LLMPermanentError, LLMAuthenticationError)) or \
            any(m in low for m in _AUTH_PERMANENT_MARKERS):
        return ClassifiedError(FailoverReason.AUTH_PERMANENT, retryable=False,
                               should_compress=False, should_fallback=False,
                               should_rotate_credential=False, matched_text=str(error)[:300])

    # 4. All providers exhausted — terminal.
    if isinstance(error, LLMProviderExhaustedError):
        return ClassifiedError(FailoverReason.PROVIDER_EXHAUSTED, retryable=False,
                               should_compress=False, should_fallback=False,
                               should_rotate_credential=False)

    # 5. Rate limit / resource exhausted — retryable, try fallback.
    if isinstance(error, (LLMRateLimitError, RateLimitError, ResourceExhausted)) or \
            "rate_limit" in low or "rate limit" in low or "429" in low:
        return ClassifiedError(FailoverReason.RATE_LIMIT, retryable=True,
                               should_compress=False, should_fallback=True,
                               should_rotate_credential=False)

    # 6. Connection / timeout — retryable, try fallback.
    if isinstance(error, (LLMConnectionError, ConnectionError)) or \
            "connection" in low or "timeout" in low:
        return ClassifiedError(FailoverReason.CONNECTION, retryable=True,
                               should_compress=False, should_fallback=True,
                               should_rotate_credential=False)

    # 7. Validation / parse — retryable; compress if it's a token-limit parse error.
    if isinstance(error, (ValidationError, ValueError)):
        should_compress = "context length" in low or "max token" in low
        return ClassifiedError(FailoverReason.VALIDATION, retryable=True,
                               should_compress=should_compress, should_fallback=False,
                               should_rotate_credential=False)

    # 8. Generic LLM error — retryable, try fallback.
    if isinstance(error, LLMError) or "llm" in low:
        return ClassifiedError(FailoverReason.GENERIC_LLM, retryable=True,
                               should_compress=False, should_fallback=True,
                               should_rotate_credential=False)

    # 9. Unknown — FAIL OPEN (preserves error_recovery.py:432-442): retryable, no halt,
    #    no compress, no fallback. max_failures is the backstop for a genuinely stuck loop.
    return ClassifiedError(FailoverReason.UNKNOWN, retryable=True,
                           should_compress=False, should_fallback=False,
                           should_rotate_credential=False)


def classify_text(text: Optional[str]) -> FailoverReason:
    """String-only classification for the post-run OUTAGE-NOTICE seam (the exception is
    gone by the surface deliver stage — only the terminal status string remains). Unifies
    core/surfaces/llm_outage_notice.py's ad-hoc markers with this SSOT. No chain walk."""
    if not text:
        return FailoverReason.UNKNOWN
    low = str(text).lower()
    if looks_like_credit_death(low):
        return FailoverReason.CREDIT_DEATH
    if any(m in low for m in _AUTH_PERMANENT_MARKERS):
        return FailoverReason.AUTH_PERMANENT
    if any(m in low for m in _EXHAUSTION_TEXT_MARKERS):
        return FailoverReason.PROVIDER_EXHAUSTED
    return FailoverReason.UNKNOWN
