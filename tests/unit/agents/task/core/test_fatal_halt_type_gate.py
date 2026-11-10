"""An application exception must never halt the session by coincidental text.

The type decides `step._is_fatal_step_exc` (the halt before `_handle_step_error` runs).
`tests/unit/agents/test_error_recovery_routing.py` holds the same rule for the handler.
"""
import sqlite3

import pytest

from agents.task.agent.core.step import _is_fatal_step_exc
from core.exceptions import (
    InsufficientCreditsError,
    LLMAuthenticationError,
)


# Real application exceptions whose text trips the substring matchers. Each one is a
# shape this codebase can genuinely raise: `billing_failures` is a shipped table, and
# a bare 402 appears in ids, counts and URL paths.
APPLICATION_FALSE_POSITIVES = [
    sqlite3.OperationalError("no such column: billing_total"),
    KeyError("billing_failures"),
    NameError("name 'billing' is not defined"),
    ValueError("upstream returned 500 for /v1/items/402"),
    RuntimeError("processed 402 rows"),
    TypeError("api key must be a str"),
]


@pytest.mark.parametrize("exc", APPLICATION_FALSE_POSITIVES, ids=lambda e: type(e).__name__)
@pytest.mark.parametrize("failover", [True, False])
def test_application_exception_is_never_a_fatal_step_halt(exc, failover):
    assert _is_fatal_step_exc(exc, billing_failover_enabled=failover) is False, (
        f"{type(exc).__name__}({exc}) halted the session on coincidental text"
    )


@pytest.mark.parametrize("failover", [True, False])
def test_provider_exceptions_still_halt(failover):
    """The gate must not weaken real provider-fault handling."""
    assert _is_fatal_step_exc(
        LLMAuthenticationError("invalid api key provided"),
        billing_failover_enabled=failover,
    ) is True


def test_credit_death_still_routes_by_the_failover_flag():
    """Billing halts only when failover is off; with it on, it must reach
    _handle_step_error so a still-funded provider can be tried."""
    err = InsufficientCreditsError(
        "u1", required=100, available=0,
        message="insufficient_quota: billing hard limit reached",
    )
    assert _is_fatal_step_exc(err, billing_failover_enabled=False) is True
    assert _is_fatal_step_exc(err, billing_failover_enabled=True) is False


def test_a_typed_rate_limit_with_a_quota_text_is_not_fatal():
    from core.exceptions import LLMRateLimitError
    exc = LLMRateLimitError("Gemini: 429 quota exceeded for this minute")
    assert _is_fatal_step_exc(exc, billing_failover_enabled=False) is False
