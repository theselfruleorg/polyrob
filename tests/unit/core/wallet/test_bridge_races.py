"""098 W1 CO-23: two drivers settle one bridge row — every write is a guarded
transition, and the watcher leaves a row the verb still drives alone."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from core.state.machine import IllegalTransition
from core.sqlite_util import execute_retry
from core.wallet import bridge_guard as bg
from core.wallet import bridge_watcher as bw

BASE_ID = 8453
RECIPIENT = "0x" + "11" * 20
FLOOR = 1000
BEFORE = 5000


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "bridges.db")


def _quote(rid="0xreq"):
    return SimpleNamespace(request_id=rid, origin_chain_id=792703809, dest_chain_id=BASE_ID,
                           recipient=RECIPIENT,
                           currency_out="0x0000000000000000000000000000000000000000",
                           amount_in_raw=10 ** 9, min_out_raw=FLOOR)


def _record(db, rid="0xreq"):
    return bg.record_pending(user_id="owner", quote=_quote(rid), amount_usd=1.0,
                             balance_before=BEFORE, db_path=db)


def _state(db, bid):
    return bg.state_of(bid, db_path=db)


class _Provider:
    def status(self, rid):
        return "success", "filled"


def _tick(db, seen):
    async def notify(container, user, notice):
        seen.append(notice)
    return asyncio.run(bw.tick(db_path=db, provider=_Provider(), notify=notify,
                               read_balance=lambda a, c: BEFORE + FLOOR))


def test_verb_timeout_does_not_overwrite_watcher_arrived(db):
    bid = _record(db)
    assert bg.settle(bid, state=bg.STATE_ARRIVED, detail="watcher proved it",
                     balance_after=BEFORE + FLOOR, db_path=db)
    # the verb's own wait timed out a moment later
    assert not bg.settle(bid, state=bg.STATE_IN_FLIGHT, detail="timeout", db_path=db,
                         from_states=(bg.STATE_PENDING, bg.STATE_IN_FLIGHT))
    row = _state(db, bid)
    assert row["state"] == bg.STATE_ARRIVED and row["detail"] == "watcher proved it"


def test_watcher_skips_row_with_live_driver_lease(db):
    bid = _record(db)
    seen = []
    assert _tick(db, seen).checked == 0
    assert _state(db, bid)["state"] == bg.STATE_PENDING and seen == []
    execute_retry(db, "UPDATE bridges SET driver_until=? WHERE id=?", (time.time() - 1, bid))
    _tick(db, seen)
    assert _state(db, bid)["state"] == bg.STATE_ARRIVED and len(seen) == 1


def test_dead_driver_lease_is_void(db):
    bid = _record(db)
    execute_retry(db, "UPDATE bridges SET driver=? WHERE id=?", ("999999999|x|y", bid))
    seen = []
    _tick(db, seen)
    assert _state(db, bid)["state"] == bg.STATE_ARRIVED


def test_settle_none_balance_keeps_measured_value(db):
    bid = _record(db)
    assert bg.settle(bid, state=bg.STATE_IN_FLIGHT, balance_after=6000, db_path=db)
    assert bg.settle(bid, state=bg.STATE_IN_FLIGHT, balance_after=None, db_path=db)
    assert str(_state(db, bid)["balance_after"]) == "6000"


def test_single_arrived_notice_when_both_drivers_see_arrival(db, monkeypatch):
    bid = _record(db)
    execute_retry(db, "UPDATE bridges SET driver_until=NULL WHERE id=?", (bid,))
    real = bg.settle
    # the verb settles arrived between the watcher's read and its write
    def racing(b, **kw):
        real(b, state=bg.STATE_ARRIVED, detail="verb", db_path=db)
        return real(b, **kw)
    monkeypatch.setattr(bg, "settle", racing)
    seen = []
    out = _tick(db, seen)
    assert seen == [] and out.arrived == 0
    assert _state(db, bid)["detail"] == "verb"


def test_unsent_sweep_keeps_row_whose_tx_stamp_failed(db, monkeypatch):
    bid = _record(db)
    execute_retry(db, "UPDATE bridges SET created_at=? WHERE id=?",
                  (time.time() - bg.UNSENT_STALE_SEC - 60, bid))
    calls = {"n": 0}
    real_exec = execute_retry

    def flaky(path, sql, params=(), **kw):
        if sql.startswith("UPDATE bridges SET tx_ref"):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("database is locked")
            # the second attempt cannot stamp the tx either, only the hold
            return real_exec(path, "UPDATE bridges SET driver_until=? WHERE id=?",
                             (params[1], params[2]), **kw)
        return real_exec(path, sql, params, **kw)
    monkeypatch.setattr("core.sqlite_util.execute_retry", flaky)
    bg.mark_sent(bid, "0xsig", db_path=db)
    monkeypatch.setattr("core.sqlite_util.execute_retry", real_exec)
    assert bg.close_stale_unsent(status=lambda r: ("pending", "waiting"), db_path=db) == 0
    assert _state(db, bid)["state"] == bg.STATE_PENDING


def test_terminal_states_are_final():
    for t in bg.BRIDGE.terminal:
        with pytest.raises(IllegalTransition):
            bg.BRIDGE.check(t, bg.STATE_IN_FLIGHT)
    assert not bg.BRIDGE.validate()
