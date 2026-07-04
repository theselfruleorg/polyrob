"""UpdateDedup.peek is a non-mutating read: it must NOT claim the update (else
process_update's authoritative seen() would drop the real message)."""
from surfaces.telegram.dedup import UpdateDedup


def test_peek_does_not_claim(tmp_path):
    d = UpdateDedup(str(tmp_path / "dedup.db"))
    # peek before any seen() -> not recorded, and peeking twice does NOT record it
    assert d.peek(100) is False
    assert d.peek(100) is False
    # the authoritative claim still works afterwards (proves peek didn't claim)
    assert d.seen(100) is False   # first claim -> new
    assert d.seen(100) is True    # second claim -> duplicate


def test_peek_true_after_seen(tmp_path):
    d = UpdateDedup(str(tmp_path / "dedup.db"))
    assert d.seen(200) is False   # claim it
    assert d.peek(200) is True    # now peek sees it
