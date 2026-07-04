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
