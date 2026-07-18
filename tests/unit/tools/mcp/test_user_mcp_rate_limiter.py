"""F-1 characterization: pin the ``tools.mcp.user_mcp_service.RateLimiter``
sliding-window semantics BEFORE consolidating it onto ``core.rate_limit``.

Public surface only (``check``/``remaining`` + ctor) so the tests pass unchanged
against both the legacy local class and the core-backed replacement. The clock is
controlled by patching ``time.time`` globally — both the legacy implementation
(function-local ``import time``) and the core primitive (live lookup when no
``time_fn`` is injected) read it at call time.
"""
from unittest.mock import patch

from tools.mcp.user_mcp_service import RateLimiter


def test_allows_up_to_max_then_denies():
    rl = RateLimiter(3, 60)
    with patch("time.time", return_value=1000.0):
        assert rl.check("u1")
        assert rl.check("u1")
        assert rl.check("u1")
        assert not rl.check("u1")


def test_window_expiry_frees_all_slots():
    rl = RateLimiter(2, 60)
    with patch("time.time", return_value=1000.0):
        assert rl.check("u2")
        assert rl.check("u2")
        assert not rl.check("u2")
    with patch("time.time", return_value=1061.0):
        assert rl.check("u2")


def test_partial_expiry_frees_only_expired_slots():
    rl = RateLimiter(2, 60)
    with patch("time.time", return_value=1000.0):
        assert rl.check("u3")
    with patch("time.time", return_value=1030.0):
        assert rl.check("u3")
        assert not rl.check("u3")
    # t=1061: the t=1000 call has expired, the t=1030 one has not.
    with patch("time.time", return_value=1061.0):
        assert rl.check("u3")
        assert not rl.check("u3")


def test_remaining_counts_down_and_does_not_consume():
    rl = RateLimiter(3, 60)
    with patch("time.time", return_value=1000.0):
        assert rl.remaining("u4") == 3
        rl.check("u4")
        assert rl.remaining("u4") == 2
        assert rl.remaining("u4") == 2  # calling remaining() again consumes nothing


def test_per_user_isolation():
    rl = RateLimiter(1, 60)
    with patch("time.time", return_value=1000.0):
        assert rl.check("u5")
        assert not rl.check("u5")
        assert rl.check("u6")


def test_denied_calls_are_not_recorded():
    rl = RateLimiter(2, 60)
    with patch("time.time", return_value=1000.0):
        assert rl.check("u7")
        assert rl.check("u7")
    with patch("time.time", return_value=1010.0):
        for _ in range(3):
            assert not rl.check("u7")
    # t=1061: both RECORDED calls (t=1000) have expired. If the t=1010 denials
    # had been recorded they would still be in-window and this would deny.
    with patch("time.time", return_value=1061.0):
        assert rl.check("u7")
