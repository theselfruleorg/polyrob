from core.surfaces.rate_bucket import TokenBucket


def test_bucket_allows_burst_then_throttles():
    b = TokenBucket(rate_per_sec=1.0, burst=2)
    assert b.take("k", now=0.0)[0] is True
    assert b.take("k", now=0.0)[0] is True
    allowed, retry = b.take("k", now=0.0)
    assert allowed is False and retry > 0


def test_bucket_refills_over_time():
    b = TokenBucket(rate_per_sec=1.0, burst=1)
    assert b.take("k", now=0.0)[0] is True
    assert b.take("k", now=0.5)[0] is False
    assert b.take("k", now=1.0)[0] is True


# --- WS-4 (2026-07-16): per-key eviction — the canonical primitive must not leak ---

def test_idle_keys_are_pruned():
    """A key idle long enough to be fully refilled is byte-equivalent to a fresh key —
    it must be evicted rather than held forever (one entry per messaging destination)."""
    tb = TokenBucket(rate_per_sec=1.0, burst=5)
    tb.take("old-dest", now=1000.0)
    tb.take("new-dest", now=1000.0 + 3600.0)  # an hour later: old-dest fully refilled
    assert "old-dest" not in tb._state
    assert "new-dest" in tb._state


def test_prune_preserves_semantics_for_reused_key():
    """Evict-then-recreate must give the same take() results as never evicting."""
    kept = TokenBucket(rate_per_sec=1.0, burst=2)
    # Drain at t=0, wait until fully refilled (>= burst/rate + eviction margin).
    kept.take("k", now=0.0)
    kept.take("k", now=0.0)
    allowed_after_idle = kept.take("k", now=5000.0)
    fresh = TokenBucket(rate_per_sec=1.0, burst=2)
    allowed_fresh = fresh.take("k", now=5000.0)
    assert allowed_after_idle == allowed_fresh


def test_active_key_not_pruned():
    tb = TokenBucket(rate_per_sec=1.0, burst=5)
    tb.take("busy", now=100.0)
    tb.take("busy", now=101.0)
    tb.take("other", now=102.0)
    assert "busy" in tb._state
