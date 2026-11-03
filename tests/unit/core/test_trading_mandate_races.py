"""098 CO-30: mandate status edges are CAS on the state the caller checked."""
import pytest

from core import trading_mandate as tm
from core.trading_mandate import (MandateChanged, MandateError, OWNER_SEAT, create, get, latch,
                                  release, set_status)
import tests.trading_fixture  # noqa: F401  (registers the test preset)


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "trading.db")


def _mk(db):
    return create("u1", actor=OWNER_SEAT, budget_usd=150.0, chains=["robinhood"], path=db,
                  now=1000.0, preset="longtail")


def _stale_read(monkeypatch, stale):
    """The next _require returns ``stale`` (the read before a racing write)."""
    real = tm._require
    calls = {"n": 0}

    def fake(mid, path):
        calls["n"] += 1
        return stale if calls["n"] == 1 else real(mid, path)

    monkeypatch.setattr(tm, "_require", fake)


def test_latch_after_stop_does_not_revive(db, monkeypatch):
    m = _mk(db)
    stale = get(m.id, path=db)                      # the breaker read: active
    set_status(m.id, "stopped", actor=OWNER_SEAT, path=db)  # the owner taps Stop
    _stale_read(monkeypatch, stale)
    out = latch(m.id, "breaker:drawdown", path=db)
    assert out.status == "stopped"
    assert get(m.id, path=db).status == "stopped"


def test_pause_does_not_overwrite_concurrent_latch(db, monkeypatch):
    m = _mk(db)
    stale = get(m.id, path=db)                      # the pause read: active
    latch(m.id, "breaker:drawdown", path=db)        # the breaker latches first
    _stale_read(monkeypatch, stale)
    with pytest.raises(MandateChanged, match="now halted"):
        set_status(m.id, "paused", actor="agent", path=db)
    assert get(m.id, path=db).status == "halted"


def test_resume_from_halted_requires_release(db):
    m = _mk(db)
    latch(m.id, "breaker:drawdown", path=db)
    with pytest.raises(MandateError):
        set_status(m.id, "active", actor=OWNER_SEAT, path=db)
    out = release(m.id, actor=OWNER_SEAT, path=db)
    assert out.status == "active" and out.released_at is not None


def test_release_loses_to_concurrent_stop(db, monkeypatch):
    m = _mk(db)
    latch(m.id, "breaker:drawdown", path=db)
    stale = get(m.id, path=db)                      # the release read: halted
    set_status(m.id, "stopped", actor=OWNER_SEAT, path=db)
    _stale_read(monkeypatch, stale)
    with pytest.raises(MandateChanged, match="now stopped"):
        release(m.id, actor=OWNER_SEAT, path=db)
    assert get(m.id, path=db).status == "stopped"
