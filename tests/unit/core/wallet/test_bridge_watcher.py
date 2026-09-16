"""The bridge reconciliation ticker (039 Unit C).

`await_arrival` parks an unresolved bridge `in_flight` and returns — which is the
right answer, and until this ticker it was also the LAST thing that ever happened
to that row. Nothing re-measured the destination. The owner's only route to an
answer was to run `polyrob wallet bridges` and know to ask.
"""
import asyncio
import time
from types import SimpleNamespace

import pytest

from core.wallet import bridge_guard, bridge_watcher as bw

RECIPIENT = "0xcAda546f6A6ddDE31B71aB21eF63d3EBF09Fa553"
BASE_ID = 8453
FLOOR = 35_900_000_000_000_000
BEFORE = 10 ** 15


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "bridges.db")


def _quote(request_id="0xreq"):
    return SimpleNamespace(
        request_id=request_id, origin_chain_id=792703809, dest_chain_id=BASE_ID,
        recipient=RECIPIENT, currency_out="0x0000000000000000000000000000000000000000",
        amount_in_raw=10 ** 9, min_out_raw=FLOOR)


def _row(db, *, request_id="0xreq", created_at=None):
    bid = bridge_guard.record_pending(
        user_id="owner", quote=_quote(request_id), amount_usd=94.0,
        balance_before=BEFORE, db_path=db)
    if created_at is not None:
        from core.sqlite_util import execute_retry
        execute_retry(db, "UPDATE bridges SET created_at=? WHERE id=?",
                      (created_at, bid))
    bridge_guard.settle(bid, state=bridge_guard.STATE_IN_FLIGHT,
                        detail="not confirmed within 300s", tx_ref="0xsig",
                        db_path=db)
    return bid


class _Provider:
    def __init__(self, state="pending", detail="waiting"):
        self._answer = (state, detail)

    def status(self, request_id):
        return self._answer


def _run(coro):
    return asyncio.run(coro)


def _collect_notices():
    seen = []

    async def _notify(container, user_id, notice):
        seen.append((user_id, notice))
    return seen, _notify


# --------------------------------------------------------------------------

def test_a_late_arrival_is_measured_and_settled(db):
    bid = _row(db)
    seen, notify = _collect_notices()
    out = _run(bw.tick(db_path=db, provider=_Provider(),
                       read_balance=lambda a, c: BEFORE + FLOOR, notify=notify))
    assert out.arrived == 1
    rows = bridge_guard.open_bridges("owner", db_path=db)
    assert rows == [], "a settled bridge must leave the open list"
    assert seen and seen[0][0] == "owner"
    assert seen[0][1].state == "arrived"
    assert "resolved by the watcher" in seen[0][1].detail


def test_an_unreadable_balance_is_unknown_never_an_arrival(db):
    """A dead RPC returning nothing must not settle a row either way."""
    _row(db)
    seen, notify = _collect_notices()
    out = _run(bw.tick(db_path=db, provider=_Provider(),
                       read_balance=lambda a, c: None, notify=notify))
    assert out.unreadable == 1
    assert out.arrived == 0 and out.failed == 0
    assert bridge_guard.open_bridges("owner", db_path=db), "the row must stay open"


def test_a_shortfall_is_not_an_arrival(db):
    _row(db)
    out = _run(bw.tick(db_path=db, provider=_Provider(),
                       read_balance=lambda a, c: BEFORE + FLOOR - 1,
                       notify=_collect_notices()[1]))
    assert out.arrived == 0
    assert bridge_guard.open_bridges("owner", db_path=db)


def test_a_refund_with_no_inflow_is_the_one_case_that_may_be_called_failed(db):
    _row(db)
    seen, notify = _collect_notices()
    out = _run(bw.tick(db_path=db, provider=_Provider("failure", "refunded"),
                       read_balance=lambda a, c: BEFORE, notify=notify))
    assert out.failed == 1
    assert seen[0][1].state == "failed"
    assert "refund" in seen[0][1].detail


def test_a_provider_failure_WITH_a_measured_arrival_is_not_believed(db):
    """One of the two is wrong and we do not get to pick which — so the row is
    settled by the MEASUREMENT, which is ours, not by the claim, which is not."""
    _row(db)
    out = _run(bw.tick(db_path=db, provider=_Provider("failure", "refunded"),
                       read_balance=lambda a, c: BEFORE + FLOOR,
                       notify=_collect_notices()[1]))
    assert out.arrived == 1 and out.failed == 0


def test_a_stuck_bridge_escalates_once_not_every_tick(db):
    """An alert that repeats is an alert that gets muted."""
    old = time.time() - (bw.ESCALATE_AFTER_SEC + 60)
    _row(db, created_at=old)
    seen, notify = _collect_notices()
    first = _run(bw.tick(db_path=db, provider=_Provider(),
                         read_balance=lambda a, c: BEFORE, notify=notify))
    second = _run(bw.tick(db_path=db, provider=_Provider(),
                          read_balance=lambda a, c: BEFORE, notify=notify))
    assert first.escalated == 1
    assert second.escalated == 0, "the second pass must not re-alert"
    assert len(seen) == 1
    assert "Do NOT re-send" in seen[0][1].detail


def test_a_young_unresolved_bridge_is_not_escalated(db):
    _row(db)
    seen, notify = _collect_notices()
    out = _run(bw.tick(db_path=db, provider=_Provider(),
                       read_balance=lambda a, c: BEFORE, notify=notify))
    assert out.escalated == 0 and not seen


def test_an_unreadable_store_reports_nothing_rather_than_all_clear(tmp_path):
    """Returning 'no bridges in flight' over an unreadable store is the
    confident-and-wrong failure this codebase keeps paying for."""
    out = _run(bw.tick(db_path=str(tmp_path / "does-not-exist.db"),
                       provider=_Provider(), read_balance=lambda a, c: 0,
                       notify=_collect_notices()[1]))
    assert out.checked == 0


def test_one_bad_row_does_not_abort_the_pass(db):
    _row(db, request_id="0xa")
    _row(db, request_id="0xb")

    class _Flaky:
        def __init__(self):
            self.n = 0

        def status(self, request_id):
            self.n += 1
            if self.n == 1:
                raise RuntimeError("relay down")
            return ("pending", "waiting")
    out = _run(bw.tick(db_path=db, provider=_Flaky(),
                       read_balance=lambda a, c: BEFORE,
                       notify=_collect_notices()[1]))
    assert out.checked == 2


def test_rows_are_read_across_tenants_but_notified_per_tenant(db):
    bridge_guard.record_pending(user_id="alice", quote=_quote("0xa"),
                                amount_usd=1.0, balance_before=BEFORE, db_path=db)
    bridge_guard.record_pending(user_id="bob", quote=_quote("0xb"),
                                amount_usd=1.0, balance_before=BEFORE, db_path=db)
    seen, notify = _collect_notices()
    _run(bw.tick(db_path=db, provider=_Provider(),
                 read_balance=lambda a, c: BEFORE + FLOOR, notify=notify))
    assert sorted(u for u, _ in seen) == ["alice", "bob"]


# --------------------------------------------------------------------------
# The pause exemption is a decision, not an oversight
# --------------------------------------------------------------------------

def test_the_watcher_is_deliberately_not_pause_gated():
    """031 stops the agent ACTING. This ticker signs nothing and starts no work —
    it reads balances and reports. An owner who has just stopped everything is
    exactly the owner who needs to know where their in-flight funds are.

    Pinned so a future reader does not 'fix' it into silence.
    """
    import inspect
    src = inspect.getsource(bw)
    assert "allows(" not in src
    assert "NOT pause-gated" in src

    from pathlib import Path
    ratchet = Path("tests/test_autonomy_control_ratchet.py").read_text()
    assert "bridge_watcher" not in ratchet, (
        "the watcher is not an activity STARTER; adding it to the 031 ratchet "
        "would force a pause gate onto a read-and-report loop")


def test_the_watcher_follows_the_bridge_flag(monkeypatch):
    monkeypatch.delenv("BRIDGE_WATCHER_ENABLED", raising=False)
    monkeypatch.delenv("DEFI_BRIDGE_ENABLED", raising=False)
    assert bw.enabled() is False
    monkeypatch.setenv("DEFI_BRIDGE_ENABLED", "true")
    assert bw.enabled() is True
    monkeypatch.setenv("BRIDGE_WATCHER_ENABLED", "false")
    assert bw.enabled() is False


def test_a_store_written_before_escalated_at_existed_is_still_readable(tmp_path):
    """Caught on the 2026-09-12 prod deploy.

    `escalated_at` was added inside `_init`, which only the WRITE path calls, so
    the readers selected a column that did not exist on an older store and the
    watcher logged "could not read the bridge store" every tick. Migrating on
    read is safe in a way CREATING on read is not: the file already exists.
    """
    from core.sqlite_util import wal_connect
    db = str(tmp_path / "legacy.db")
    conn = wal_connect(db)
    try:
        conn.execute("""CREATE TABLE bridges (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, request_id TEXT NOT NULL,
            origin_chain_id INTEGER NOT NULL, dest_chain_id INTEGER NOT NULL,
            recipient TEXT NOT NULL, currency_out TEXT NOT NULL,
            amount_in_raw TEXT NOT NULL, min_out_raw TEXT NOT NULL,
            amount_usd REAL, state TEXT NOT NULL DEFAULT 'pending', tx_ref TEXT,
            balance_before TEXT, balance_after TEXT, detail TEXT,
            created_at REAL NOT NULL, settled_at REAL)""")
        conn.execute(
            "INSERT INTO bridges (id,user_id,request_id,origin_chain_id,"
            "dest_chain_id,recipient,currency_out,amount_in_raw,min_out_raw,"
            "state,balance_before,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("old1", "owner", "0xold", 1, BASE_ID, RECIPIENT, "", "1", str(FLOOR),
             bridge_guard.STATE_IN_FLIGHT, str(BEFORE), time.time()))
        conn.commit()
    finally:
        conn.close()

    rows = bridge_guard.open_bridges_all(db_path=db)
    assert len(rows) == 1 and rows[0]["id"] == "old1"
    assert rows[0]["escalated_at"] is None
    assert bridge_guard.open_bridges("owner", db_path=db)

    out = _run(bw.tick(db_path=db, provider=_Provider(),
                       read_balance=lambda a, c: BEFORE + FLOOR,
                       notify=_collect_notices()[1]))
    assert out.checked == 1 and out.arrived == 1


def test_a_read_still_never_CREATES_the_store(tmp_path):
    """The migration must not become a creation: reporting 'no bridges in flight'
    from a store we just invented is the confident-and-wrong failure."""
    import os
    missing = str(tmp_path / "nope.db")
    assert bridge_guard.open_bridges_all(db_path=missing) == []
    assert bridge_guard.open_bridges("owner", db_path=missing) == []
    assert not os.path.exists(missing)
