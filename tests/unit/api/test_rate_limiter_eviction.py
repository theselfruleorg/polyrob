"""WS-4: ``api.middleware.RateLimiter`` must actually evict idle per-user state.

The original leak: ``user_buckets`` was a defaultdict that gained one entry per
distinct user_id forever; ``_cleanup_old_buckets`` existed but was NEVER CALLED.
F-1 (2026-07-17) rebuilt the limiter on the canonical ``core.rate_limit``
primitives (TokenBucket + two FixedWindowCounters), each of which carries the
WS-4 amortized idle-key sweep itself — an idle key is evicted once it is
semantically identical to a fresh one (bucket fully refilled / window expired).
These tests pin the same two invariants at the new seams: idle keys get dropped,
and an active key's mid-window state is never corrupted by a sweep.
"""
import time
from unittest.mock import patch

from api.middleware import RateLimiter


def _at(t):
    return patch("api.middleware.time.monotonic", return_value=t)


def test_idle_user_state_is_evicted_on_later_traffic():
    rl = RateLimiter()
    base = time.monotonic()
    with _at(base):
        rl.check_rate_limit("user-idle")
    assert "user-idle" in rl._bucket._state
    assert "user-idle" in rl._minute._state
    assert "user-idle" in rl._hour._state

    # Far in the future (> 1h idle: every window expired, bucket refilled),
    # other traffic arrives and triggers the amortized sweeps.
    with _at(base + 7200):
        rl.check_rate_limit("user-active")

    for prim in (rl._bucket, rl._minute, rl._hour):
        assert "user-idle" not in prim._state, (
            f"idle key survived in {type(prim).__name__} — the idle-key sweep is not firing"
        )
        assert "user-active" in prim._state


def test_sweep_does_not_disturb_fresh_keys():
    """A burst of distinct users inside the sweep interval all stay tracked."""
    rl = RateLimiter()
    base = time.monotonic()
    with _at(base):
        for i in range(50):
            rl.check_rate_limit(f"u{i}")
    assert len(rl._hour._state) == 50


def test_active_user_mid_window_count_survives_sweeps():
    """An hour-window count that is still live must never be lost to a sweep —
    eviction is only legal once the state is equivalent to fresh."""
    rl = RateLimiter(requests_per_minute=100, requests_per_hour=5, burst_size=10)
    base = time.monotonic()
    with _at(base):
        for _ in range(3):
            assert rl.check_rate_limit("user-a")[0]
    # 30 min later (past every sweep interval, hour window still open): 2 more.
    with _at(base + 1800):
        assert rl.check_rate_limit("user-a")[0]
        assert rl.check_rate_limit("user-a")[0]
        # The hour budget (5) is now exhausted — state from t=base survived.
        allowed, info = rl.check_rate_limit("user-a")
    assert not allowed
    assert info.window == "hour"
