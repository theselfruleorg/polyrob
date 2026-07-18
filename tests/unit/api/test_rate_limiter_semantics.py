"""F-1 characterization: pin ``api.middleware.RateLimiter``'s observable allow/deny
semantics BEFORE consolidating it onto ``core.rate_limit`` primitives.

These tests deliberately assert only through the public surface
(``check_rate_limit(user_id) -> (allowed, RateLimitInfo)``) and the constructor
attributes — never internals — so they must pass unchanged against BOTH the legacy
self-contained implementation and the consolidated one.

Clock control: the class reads ``time.monotonic()`` via the module-level ``time``
import, so we patch ``api.middleware.time.monotonic`` (same pattern as the WS-4
eviction tests).
"""
import time
from unittest.mock import patch

from api.middleware import RateLimiter


def _at(t):
    return patch("api.middleware.time.monotonic", return_value=t)


def test_defaults():
    rl = RateLimiter()
    assert rl.rpm_limit == 60
    assert rl.rph_limit == 1000
    assert rl.burst_size == 10


def test_burst_denies_after_tokens_exhausted_and_labels_window():
    rl = RateLimiter(requests_per_minute=100, requests_per_hour=1000, burst_size=3)
    with _at(1000.0):
        for _ in range(3):
            allowed, _info = rl.check_rate_limit("u-burst")
            assert allowed
        allowed, info = rl.check_rate_limit("u-burst")
    assert not allowed
    assert info.window == "burst"
    assert info.remaining == 0
    assert info.limit == 3


def test_burst_refills_at_burst_size_per_second():
    """Legacy refill rate is burst_size tokens/sec — half a second refills 1.5 tokens."""
    rl = RateLimiter(requests_per_minute=100, requests_per_hour=1000, burst_size=3)
    with _at(1000.0):
        for _ in range(3):
            rl.check_rate_limit("u-refill")
        allowed, _ = rl.check_rate_limit("u-refill")
        assert not allowed
    with _at(1000.5):
        allowed, _ = rl.check_rate_limit("u-refill")
    assert allowed


def test_minute_window_denies_then_resets():
    rl = RateLimiter(requests_per_minute=3, requests_per_hour=100, burst_size=10)
    for i, t in enumerate([1000.0, 1001.0, 1002.0]):
        with _at(t):
            allowed, _ = rl.check_rate_limit("u-min")
            assert allowed, f"call {i} at t={t} should be allowed"
    with _at(1003.0):
        allowed, info = rl.check_rate_limit("u-min")
    assert not allowed
    assert info.window == "minute"
    assert info.limit == 3
    # Window started at first request (t=1000) — 60s later it resets.
    with _at(1060.0):
        allowed, _ = rl.check_rate_limit("u-min")
    assert allowed


def test_hour_window_denies_then_resets():
    rl = RateLimiter(requests_per_minute=100, requests_per_hour=3, burst_size=10)
    for t in [1000.0, 1061.0, 1122.0]:
        with _at(t):
            allowed, _ = rl.check_rate_limit("u-hour")
            assert allowed
    with _at(1180.0):
        allowed, info = rl.check_rate_limit("u-hour")
    assert not allowed
    assert info.window == "hour"
    assert info.limit == 3
    # Hour window started at t=1000 — resets at t=4600.
    with _at(4600.0):
        allowed, _ = rl.check_rate_limit("u-hour")
    assert allowed


def test_denied_calls_do_not_consume_or_count():
    """A denial must not consume a token nor increment the minute/hour counters."""
    rl = RateLimiter(requests_per_minute=2, requests_per_hour=4, burst_size=10)
    with _at(1000.0):
        assert rl.check_rate_limit("u-noc")[0]
        assert rl.check_rate_limit("u-noc")[0]
    # Five denials inside the same minute window.
    for t in [1001.0, 1002.0, 1003.0, 1004.0, 1005.0]:
        with _at(t):
            allowed, info = rl.check_rate_limit("u-noc")
            assert not allowed
            assert info.window == "minute"
    # Minute window resets at t=1060. If the denials had counted toward the hour
    # limit (2 + 5 = 7 > 4) these would deny with window="hour" instead.
    with _at(1061.0):
        assert rl.check_rate_limit("u-noc")[0]
        assert rl.check_rate_limit("u-noc")[0]  # hour count now 4
    # Next minute window: the hour limit (4) is now genuinely exhausted.
    with _at(1121.0):
        allowed, info = rl.check_rate_limit("u-noc")
    assert not allowed
    assert info.window == "hour"


def test_per_user_isolation():
    rl = RateLimiter(requests_per_minute=2, requests_per_hour=100, burst_size=10)
    with _at(1000.0):
        assert rl.check_rate_limit("u-a")[0]
        assert rl.check_rate_limit("u-a")[0]
        assert not rl.check_rate_limit("u-a")[0]
        assert rl.check_rate_limit("u-b")[0]


def test_allowed_call_reports_minute_remaining():
    rl = RateLimiter(requests_per_minute=5, requests_per_hour=100, burst_size=10)
    with _at(1000.0):
        allowed, info = rl.check_rate_limit("u-ok")
    assert allowed
    assert info.window == "minute"
    assert info.limit == 5
    assert info.remaining == 4  # post-increment count
