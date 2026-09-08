"""HIGH-1: _is_fatal_step_error must route billing/quota to failover when enabled."""
from agents.task.agent.core.step import _is_fatal_step_error


def test_auth_always_fatal():
    for s in ("authentication failed", "invalid api key"):
        assert _is_fatal_step_error(s, billing_failover_enabled=False) is True
        assert _is_fatal_step_error(s, billing_failover_enabled=True) is True


def test_429_and_rate_limit_not_fatal():
    # CO-F5: bare 429/rate-limit strings are retryable, not fatal — they must reach
    # _handle_step_error's graceful rate-limit branch (circuit breaker / provider
    # fallback / backoff) instead of halting the session immediately.
    for s in ("error 429 too many requests", "rate limit exceeded"):
        assert _is_fatal_step_error(s, billing_failover_enabled=False) is False
        assert _is_fatal_step_error(s, billing_failover_enabled=True) is False


def test_billing_fatal_when_failover_disabled():
    for s in ("insufficient_quota", "billing hard limit reached", "error 402 billing"):
        assert _is_fatal_step_error(s, billing_failover_enabled=False) is True


def test_billing_not_fatal_when_failover_enabled():
    # The whole point of HIGH-1: with failover on, billing flows to _handle_step_error.
    for s in ("insufficient_quota", "billing hard limit reached"):
        assert _is_fatal_step_error(s, billing_failover_enabled=True) is False


def test_quota_exceeded_stays_fatal_even_with_failover():
    # _handle_step_error can't fail over a bare "quota exceeded" (no billing/insufficient_quota
    # substring), so it must remain fatal regardless of the flag.
    assert _is_fatal_step_error("monthly quota exceeded", billing_failover_enabled=True) is True


def test_unknown_error_not_fatal():
    assert _is_fatal_step_error("connection reset by peer", billing_failover_enabled=False) is False


# I-5: real prod shape (2026-07-09 outage) — contains "402"/"credits" but NEITHER
# "insufficient_quota" NOR "billing", so the old ad-hoc substring check missed it and
# billing failover never engaged. The SSOT detector (core.credit_sentinel.looks_like_credit_death,
# already unit-tested against this exact shape) matches on the "402" marker.
_REAL_OPENROUTER_402 = (
    '402 "This request requires more credits, or fewer max_tokens. '
    'visit https://openrouter.ai/settings/credits and add more credits"'
).lower()


def test_real_openrouter_402_is_fatal_billing_when_failover_off():
    # Today (substring 'insufficient_quota'/'billing' miss) this returns False -> the
    # error is NOT treated as billing-fatal and slips into generic retry; after the fix
    # it is correctly classified fatal-billing when failover is off.
    assert _is_fatal_step_error(_REAL_OPENROUTER_402, billing_failover_enabled=False) is True


def test_real_openrouter_402_flows_to_handler_when_failover_enabled():
    # With failover on, this must NOT be fatal *here* in the step-loop gate — it needs to
    # flow through to _handle_step_error (error_recovery.py), which attempts a provider
    # swap before halting.
    assert _is_fatal_step_error(_REAL_OPENROUTER_402, billing_failover_enabled=True) is False
