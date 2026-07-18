"""P4 finalization: shared exponential-backoff helper. The two historical call sites
cap at different points relative to jitter — both behaviors are preserved as a param.
"""
import random

from core.backoff import jittered_exponential_delay


def test_cap_before_jitter_may_exceed_cap():
    # robust_parse style: min(base, cap) * jitter — can exceed cap by up to hi.
    random.seed(0)
    d = jittered_exponential_delay(1, 10, multiplier=1.5, cap=5, cap_after_jitter=False)
    assert 5 * 0.8 <= d <= 5 * 1.2


def test_cap_after_jitter_never_exceeds_cap():
    # error_recovery style: min(base*jitter, cap) — hard ceiling.
    for seed in range(20):
        random.seed(seed)
        d = jittered_exponential_delay(2, 8, multiplier=2, cap=120, cap_after_jitter=True)
        assert d <= 120


def test_zero_failures_is_base_times_jitter():
    random.seed(3)
    d = jittered_exponential_delay(4, 0, multiplier=2, cap=1000)
    assert 4 * 0.8 <= d <= 4 * 1.2
