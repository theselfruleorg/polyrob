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


def test_permanent_claim_is_atomic_across_connections(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path = str(tmp_path / "permanent.db")
    IdempotencyStore(path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        claimed = list(pool.map(lambda _: IdempotencyStore(path).claim_permanent(
            ["grant", "payment"]), range(16)))
    assert sum(claimed) == 1
    assert not IdempotencyStore(path).claim_permanent(["new-grant", "payment"])
    assert IdempotencyStore(path).claim_permanent(["new-grant", "new-payment"])


def test_permanent_claim_store_error_propagates(tmp_path, monkeypatch):
    import pytest
    store = IdempotencyStore(str(tmp_path / "permanent.db"))
    def broken(*args, **kwargs):
        raise OSError("unreadable store")
    monkeypatch.setattr("core.surfaces.idempotency.wal_connect", broken)
    with pytest.raises(OSError):
        store.claim_permanent(["grant"])
