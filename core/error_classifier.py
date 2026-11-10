"""What a raised LLM error or a finished run's status says about credit and exhaustion.

``credit_death_in_chain`` answers the sentinel's question for an error already raised.
``classify_text`` reads the terminal status string a finished run leaves, when no
exception is left. The classifier of a provider error is
``modules.llm.llm_client.translate_llm_error``. Lives in core/ so the agent loop and
the surfaces import it downward.
"""
from __future__ import annotations

import enum
from typing import Iterator, Optional

from core.credit_sentinel import looks_like_credit_death
from core.exceptions import InsufficientCreditsError, LLMError

_MAX_CHAIN_DEPTH = 8

# Marker sets for the terminal status string (classify_text).
_AUTH_PERMANENT_MARKERS = ("insufficient_quota", "invalid_api_key", "account_deactivated")
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
    """What a terminal status string says about the run. String-valued to match the
    AccessTier/SendDecision/RouteKind convention (core/surfaces/*)."""
    CREDIT_DEATH = "credit_death"
    AUTH_PERMANENT = "auth_permanent"
    PROVIDER_EXHAUSTED = "provider_exhausted"
    UNKNOWN = "unknown"


def _iter_chain(error: BaseException) -> Iterator[BaseException]:
    """The exception and what caused it, with Python's own display rule: an error
    raised ``from None`` has no cause to read."""
    seen: set = set()
    e: Optional[BaseException] = error
    while e is not None and id(e) not in seen and len(seen) < _MAX_CHAIN_DEPTH:
        seen.add(id(e))
        yield e
        e = e.__cause__ or (None if e.__suppress_context__ else e.__context__)


def credit_death_in_chain(error: BaseException) -> Optional[str]:
    """The frame of this error's chain that reads as credit death, or None.

    Only the LLM and credit family may be judged by its text: a tool or parse error
    whose message holds "402" is not billing."""
    if not isinstance(error, (LLMError, InsufficientCreditsError)):
        return None
    for e in _iter_chain(error):
        if looks_like_credit_death(str(e)):
            return str(e)
    return None


def classify_text(text: Optional[str]) -> FailoverReason:
    """String-only reading for the post-run outage notice: the exception is gone by the
    surface deliver stage, and only the terminal status string remains."""
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
