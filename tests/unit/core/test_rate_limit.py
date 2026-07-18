"""Unit tests for the canonical rate-limit primitives (F-1, core/rate_limit.py)."""
from core.rate_limit import FixedWindowCounter, SlidingWindowLimiter, TokenBucket


# --------------------------- SlidingWindowLimiter ---------------------------

class TestSlidingWindowLimiter:
    def _mk(self, max_calls=3, window=60, **kw):
        clock = {"now": 1000.0}
        rl = SlidingWindowLimiter(max_calls, window, time_fn=lambda: clock["now"], **kw)
        return rl, clock

    def test_allows_up_to_limit_then_denies(self):
        rl, _ = self._mk(3)
        assert rl.check("k")
        assert rl.check("k")
        assert rl.check("k")
        assert not rl.check("k")

    def test_window_expiry_frees_slots(self):
        rl, clock = self._mk(2, 60)
        assert rl.check("k")
        assert rl.check("k")
        assert not rl.check("k")
        clock["now"] = 1061.0
        assert rl.check("k")

    def test_denied_calls_not_recorded(self):
        rl, clock = self._mk(2, 60)
        rl.check("k")
        rl.check("k")
        clock["now"] = 1010.0
        for _ in range(3):
            assert not rl.check("k")
        clock["now"] = 1061.0  # both recorded calls (t=1000) expired
        assert rl.check("k")

    def test_retry_after(self):
        rl, clock = self._mk(2, 60)
        assert rl.retry_after("k") == 0.0
        rl.check("k")
        clock["now"] = 1010.0
        rl.check("k")
        assert rl.retry_after("k") == 50.0  # oldest (t=1000) + 60 - 1010
        clock["now"] = 1061.0
        assert rl.retry_after("k") == 0.0

    def test_remaining_does_not_consume(self):
        rl, _ = self._mk(3)
        assert rl.remaining("k") == 3
        rl.check("k")
        assert rl.remaining("k") == 2
        assert rl.remaining("k") == 2

    def test_oldest(self):
        rl, clock = self._mk(3, 60)
        assert rl.oldest("k") is None
        rl.check("k")
        clock["now"] = 1010.0
        rl.check("k")
        assert rl.oldest("k") == 1000.0
        clock["now"] = 1061.0  # first ts out of window
        assert rl.oldest("k") == 1010.0

    def test_oldest_window_override(self):
        rl, clock = self._mk(3, 60)
        rl.check("k")
        clock["now"] = 1061.0
        # Instance window (60) has expired the t=1000 stamp; a wider override
        # window must still see it.
        rl2, clock2 = self._mk(3, 60)
        rl2.check("j")
        clock2["now"] = 1061.0
        assert rl2.oldest("j", window=900) == 1000.0
        assert rl.oldest("k") is None

    def test_per_call_overrides(self):
        rl, _ = self._mk(10, 60)
        assert rl.check("k", max_calls=1)
        assert not rl.check("k", max_calls=1)
        assert rl.remaining("k", max_calls=5) == 4
        # A shorter override window sees the old timestamp expire sooner.
        rl2, clock2 = self._mk(10, 3600)
        rl2.check("j")
        clock2["now"] = 1030.0
        assert rl2.remaining("j") == 9
        assert rl2.remaining("j", window=10) == 10

    def test_keys_lists_tracked_keys(self):
        rl, _ = self._mk(3)
        rl.check("a")
        rl.check("b")
        assert sorted(rl.keys()) == ["a", "b"]

    def test_idle_key_sweep(self):
        rl, clock = self._mk(3, 60)
        rl.check("idle")
        # Past the sweep interval AND the window: touching another key evicts it.
        clock["now"] = 1000.0 + 400.0
        rl.check("active")
        assert "idle" not in rl.keys()
        assert "active" in rl.keys()

    def test_max_keys_lru_bound(self):
        rl, _ = self._mk(3, 60, max_keys=2)
        rl.check("a")
        rl.check("b")
        rl.check("a")  # refresh a's LRU position
        rl.check("c")  # evicts b (least recently used)
        assert set(rl.keys()) == {"a", "c"}
        # Evicted key comes back fresh.
        assert rl.remaining("b") == 3

    def test_live_clock_lookup_when_no_time_fn(self):
        from unittest.mock import patch
        rl = SlidingWindowLimiter(1, 60)
        with patch("time.time", return_value=5000.0):
            assert rl.check("k")
            assert not rl.check("k")
        with patch("time.time", return_value=5061.0):
            assert rl.check("k")


# --------------------------- TokenBucket (parity + peek/consume) ---------------------------

class TestTokenBucket:
    def test_allows_burst_then_throttles(self):
        b = TokenBucket(rate_per_sec=1.0, burst=2)
        assert b.take("k", now=0.0)[0] is True
        assert b.take("k", now=0.0)[0] is True
        allowed, retry = b.take("k", now=0.0)
        assert allowed is False and retry > 0

    def test_refills_over_time(self):
        b = TokenBucket(rate_per_sec=1.0, burst=1)
        assert b.take("k", now=0.0)[0] is True
        assert b.take("k", now=0.5)[0] is False
        assert b.take("k", now=1.0)[0] is True

    def test_peek_does_not_consume(self):
        b = TokenBucket(rate_per_sec=1.0, burst=1)
        assert b.peek("k", now=0.0) == (True, 0.0)
        assert b.peek("k", now=0.0) == (True, 0.0)  # still available
        assert b.take("k", now=0.0)[0] is True       # the one token is still there

    def test_consume_after_peek_matches_take(self):
        b = TokenBucket(rate_per_sec=1.0, burst=2)
        ok, _ = b.peek("k", now=0.0)
        assert ok
        b.consume("k", now=0.0)
        ok, _ = b.peek("k", now=0.0)
        assert ok
        b.consume("k", now=0.0)
        allowed, retry = b.take("k", now=0.0)
        assert allowed is False and retry > 0

    def test_idle_keys_are_pruned(self):
        tb = TokenBucket(rate_per_sec=1.0, burst=5)
        tb.take("old-dest", now=1000.0)
        tb.take("new-dest", now=1000.0 + 3600.0)
        assert "old-dest" not in tb._state
        assert "new-dest" in tb._state


# --------------------------- FixedWindowCounter ---------------------------

class TestFixedWindowCounter:
    def test_limit_within_window(self):
        c = FixedWindowCounter(2, 60.0)
        assert c.peek("k", now=1000.0)
        c.increment("k", now=1000.0)
        assert c.peek("k", now=1030.0)
        c.increment("k", now=1030.0)
        assert not c.peek("k", now=1059.0)

    def test_window_resets_from_first_event(self):
        c = FixedWindowCounter(2, 60.0)
        c.increment("k", now=1000.0)
        c.increment("k", now=1030.0)
        assert not c.peek("k", now=1059.0)
        assert c.peek("k", now=1060.0)  # 60s after window start
        assert c.remaining("k", now=1060.0) == 2

    def test_remaining_and_seconds_until_reset(self):
        c = FixedWindowCounter(3, 60.0)
        assert c.remaining("k", now=1000.0) == 3
        c.increment("k", now=1000.0)
        assert c.remaining("k", now=1010.0) == 2
        assert c.seconds_until_reset("k", now=1010.0) == 50.0

    def test_per_key_isolation(self):
        c = FixedWindowCounter(1, 60.0)
        c.increment("a", now=1000.0)
        assert not c.peek("a", now=1000.0)
        assert c.peek("b", now=1000.0)

    def test_sweep_drops_expired_keys(self):
        c = FixedWindowCounter(2, 60.0)
        c.increment("idle", now=1000.0)
        # Past _SWEEP_INTERVAL and the window: touching another key evicts it.
        c.peek("active", now=1000.0 + 400.0)
        assert "idle" not in c._state
        assert "active" in c._state

    def test_sweep_preserves_open_window_counts(self):
        """A key whose window is still open must survive a sweep with its count."""
        c = FixedWindowCounter(2, 3600.0)
        c.increment("k", now=1000.0)
        c.increment("k", now=1001.0)
        c.peek("other", now=1000.0 + 400.0)  # triggers sweep; k's hour window open
        assert not c.peek("k", now=1000.0 + 401.0)
