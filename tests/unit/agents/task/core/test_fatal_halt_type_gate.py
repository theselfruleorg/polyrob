"""An application exception must never halt the session by coincidental text.

The fatal/permanent classifiers match bare substrings — "billing", "api key",
"authentication", and via `looks_like_credit_death` a bare \\b402\\b. That is correct
for a provider exception and actively wrong for anything else. The codebase already
found this class once and type-gated `_trip_sentinel_if_credit_death` for it (its
docstring names "a NameError naming a billing_total variable" and "an HTTP 500 on
/v1/items/402" as real triggers) — but the gate landed only on the sentinel latch, a
side effect, and not on either branch that actually stops the agent.

These pin both branches: `step._is_fatal_step_exc` (halts before _handle_step_error
ever runs) and `error_recovery._handle_step_error`'s `is_permanent`.
"""
import sqlite3

import pytest

from agents.task.agent.core.step import _is_fatal_step_error, _is_fatal_step_exc
from core.exceptions import (
    InsufficientCreditsError,
    LLMAuthenticationError,
    LLMPermanentError,
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
    assert _is_fatal_step_exc(
        LLMPermanentError("authentication failed"),
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


def test_the_string_helper_truth_table_is_unchanged():
    """The type gate lives at the exception boundary; the pure string function keeps
    its pinned behaviour so its existing characterization tests stay meaningful."""
    assert _is_fatal_step_error("invalid api key provided", billing_failover_enabled=False) is True
    assert _is_fatal_step_error("authentication failed", billing_failover_enabled=False) is True
    assert _is_fatal_step_error("quota exceeded for this account", billing_failover_enabled=False) is True
    assert _is_fatal_step_error("billing issue on account", billing_failover_enabled=True) is False
    assert _is_fatal_step_error("connection reset by peer", billing_failover_enabled=False) is False


@pytest.mark.parametrize("exc", APPLICATION_FALSE_POSITIVES, ids=lambda e: type(e).__name__)
def test_is_permanent_branch_ignores_application_exception_text(exc):
    """`_handle_step_error`'s permanent-halt branch must apply the same type gate.

    Exercised through the module's real predicate rather than a full Agent: the branch
    is `isinstance(...) or (type_gated and substring...)`, so re-deriving it here would
    just restate the code. Instead assert the gate's own membership test, which is what
    the branch consults.
    """
    from agents.task.agent.core.error_recovery import ErrorRecoveryMixin

    assert not isinstance(exc, ErrorRecoveryMixin._CREDIT_DEATH_EXCEPTION_TYPES), (
        f"{type(exc).__name__} would be allowed to decide a permanent halt by text"
    )


def test_is_permanent_source_gates_its_substring_arms():
    """Structural: the substring arms must sit behind the type gate, not beside it.

    Guards against a future edit re-flattening the `or` chain (the shape the bug had).
    """
    import inspect

    from agents.task.agent.core import error_recovery

    src = inspect.getsource(error_recovery)
    idx = src.find("is_permanent = (")
    assert idx > 0, "is_permanent branch not found — test needs updating"
    block = src[idx:idx + 700]
    assert "_text_may_decide" in block, (
        "is_permanent's substring arms are no longer behind a type gate — an "
        "application exception whose text contains 'billing' will halt the session"
    )
