"""098 W1 CO-24: a closed position stays closed. `untrust` quarantines only a
position still in play, `unquarantine` is a CAS from `quarantined`, and
`written_off` / `retired` are terminal in the POSITION table."""
import pytest

from core import open_positions as op
from core.state.machine import IllegalTransition
from core.wallet import token_pins
from core.wallet import token_provenance as tp
from core.wallet import token_trust as tt

FAKE = "0x3535353535353535353535353535353535353535"


@pytest.fixture(autouse=True)
def _stores(tmp_path, monkeypatch):
    tp._reset_for_tests()
    monkeypatch.setattr(tp, "_PROBES", {})
    monkeypatch.setattr("core.instance.resolve_owner_principal", lambda *a, **k: "alice")
    pins = str(tmp_path / "wallet" / "token_pins.db")
    monkeypatch.setattr(token_pins, "token_pins_db_path", lambda data_home=None: pins)
    monkeypatch.setattr(tp, "provenance_db_path",
                        lambda data_home=None: str(tmp_path / "wallet" / "prov.db"))
    book = str(tmp_path / "open_positions.db")
    monkeypatch.setattr(op, "open_positions_db_path", lambda data_dir=None: book)
    op.apply_delta("alice", op.PositionDelta(chain="robinhood", address=FAKE.lower(),
                                           symbol="TKN", qty=10.0, cost_usd=5.0))
    yield
    tp._reset_for_tests()


def _status():
    return op.get_position("alice", "robinhood", FAKE).status


def test_untrust_does_not_reopen_written_off():
    ctx = tt.owner_seat_ctx("alice")
    ok, _ = tt.write_off(ctx, "robinhood", FAKE, execute=True)
    assert ok and _status() == "written_off"
    ok, msg = tt.untrust(ctx, "robinhood", FAKE)
    assert ok and _status() == "written_off"
    assert "already closed" in msg


def test_unquarantine_cas_loses_to_writeoff(monkeypatch):
    ctx = tt.owner_seat_ctx("alice")
    tt.untrust(ctx, "robinhood", FAKE)
    assert _status() == "quarantined"
    real = tt._position
    seen = {}

    def stale(uid, chain, address, db):   # the undo read `quarantined`, then a write-off landed
        pos = real(uid, chain, address, db)
        if not seen:
            seen["x"] = 1
            op.set_status("alice", "robinhood", FAKE, "written_off", reason="owner")
        return pos
    monkeypatch.setattr(tt, "_position", stale)
    ok, msg = tt.unquarantine(ctx, "robinhood", FAKE, execute=True)
    assert not ok and _status() == "written_off"


def test_terminal_statuses_final():
    for t in op.POSITION.terminal:
        with pytest.raises(IllegalTransition):
            op.POSITION.check(t, "open")
    assert not op.POSITION.validate()
    op.set_status("alice", "robinhood", FAKE, "retired", reason="proof")
    assert not op.set_status("alice", "robinhood", FAKE, "dust")
    assert _status() == "retired"


def test_a_quarantine_written_during_a_buy_survives_it(monkeypatch):
    """A buy read the row, then wrote it back whole with the status it read: a
    quarantine set between the read and the write was lost. The read and the write
    are one write transaction now, so the quarantine lands after the buy and stays."""
    import threading
    real = op._status_after_buy
    done = []

    def _race(row):
        t = threading.Thread(target=lambda: done.append(op.set_status(
            "alice", "robinhood", FAKE, op.STATUS_QUARANTINED, reason="raced")))
        t.start()
        t.join(timeout=1.0)     # before the fix the quarantine lands here, inside the window
        _race.thread = t
        return real(row)
    monkeypatch.setattr(op, "_status_after_buy", _race)
    op.apply_delta("alice", op.PositionDelta(chain="robinhood", address=FAKE.lower(),
                                           symbol="TKN", qty=5.0, cost_usd=2.5))
    _race.thread.join(timeout=10)
    pos = op.get_position("alice", "robinhood", FAKE)
    assert pos.qty == 15.0
    assert pos.status == "quarantined"
