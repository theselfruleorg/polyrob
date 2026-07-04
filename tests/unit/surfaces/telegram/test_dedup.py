"""P4: update_id dedup — atomic INSERT-OR-IGNORE CAS (Fusion finding).

Telegram redelivers the same update_id on webhook-ack timeout. Dedup MUST run
before identify()/route_inbound (which have write side effects). A check-then-act
(SELECT then INSERT) races under concurrent redelivery; the CAS is one atomic
statement: rowcount==1 => we won (process), 0 => already seen (drop). 5-min window;
a post-window redelivery is reprocessable (stale rows pruned first).
"""
from surfaces.telegram.dedup import UpdateDedup


def test_first_is_new_second_is_duplicate(tmp_path):
    d = UpdateDedup(str(tmp_path / "dedup.db"))
    assert d.seen("42", now=1000.0) is False   # new -> process
    assert d.seen("42", now=1000.5) is True     # redelivery -> drop


def test_distinct_ids_both_new(tmp_path):
    d = UpdateDedup(str(tmp_path / "dedup.db"))
    assert d.seen("1", now=1000.0) is False
    assert d.seen("2", now=1000.0) is False


def test_post_window_is_reprocessable(tmp_path):
    d = UpdateDedup(str(tmp_path / "dedup.db"), window_seconds=300)
    assert d.seen("42", now=1000.0) is False
    assert d.seen("42", now=1100.0) is True       # still within window -> dup
    assert d.seen("42", now=1000.0 + 301) is False  # window passed -> new again


def test_int_and_str_update_ids_are_same_key(tmp_path):
    d = UpdateDedup(str(tmp_path / "dedup.db"))
    assert d.seen(42, now=1000.0) is False
    assert d.seen("42", now=1000.1) is True       # int 42 == str "42"


def test_concurrent_same_id_only_one_wins(tmp_path):
    """The CAS is atomic: across many calls for one id, exactly one returns False."""
    d = UpdateDedup(str(tmp_path / "dedup.db"))
    results = [d.seen("99", now=1000.0 + i * 0.001) for i in range(10)]
    assert results.count(False) == 1   # exactly one processed
    assert results.count(True) == 9    # the rest dropped
