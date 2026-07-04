import os
from core.surfaces.idempotency import IdempotencyStore


def test_seen_is_atomic_and_dedups(tmp_path):
    s = IdempotencyStore(os.path.join(tmp_path, "idem.db"))
    assert s.seen("k1", now=1000.0) is False   # new
    assert s.seen("k1", now=1000.0) is True     # duplicate
    assert s.peek("k1") is True
    assert s.peek("k2") is False


def test_window_expiry_allows_reprocess(tmp_path):
    s = IdempotencyStore(os.path.join(tmp_path, "idem.db"), window_seconds=10.0)
    assert s.seen("k", now=1000.0) is False
    assert s.seen("k", now=1005.0) is True
    assert s.seen("k", now=1100.0) is False     # past window -> new again
