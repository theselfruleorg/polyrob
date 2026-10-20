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

RECIPIENT = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
BASE_ID = 8453
FLOOR = 35_900_000_000_000_000
BEFORE = 10 ** 15


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "bridges.db")


@pytest.fixture(autouse=True)
def delivered(monkeypatch):
    """085 B-2: an EVM arrival is proven by Relay's destination tx (one per
    request id here) AND the measured balance rise. The tx pays the floor."""
    from core.wallet import bridge_status
    monkeypatch.setattr(bridge_status, "read_out_txs", lambda rid: [f"0xfill{rid}"])
    monkeypatch.setattr(bridge_guard, "evm_tx_inflow",
                        lambda h, r, c, n, **k: (FLOOR, int(time.time())))


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
    out = _run(bw.tick(db_path=db, provider=_Provider("success", "filled"),
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
    """One of the two is wrong and we do not get to pick which. Since 085 B-2 a
    balance rise alone is not an arrival (an unrelated inflow raises it too), so
    the row is neither failed nor arrived: it stays open for the owner."""
    _row(db)
    out = _run(bw.tick(db_path=db, provider=_Provider("failure", "refunded"),
                       read_balance=lambda a, c: BEFORE + FLOOR,
                       notify=_collect_notices()[1]))
    assert out.arrived == 0 and out.failed == 0
    assert bridge_guard.open_bridges("owner", db_path=db)


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
    _run(bw.tick(db_path=db, provider=_Provider("success", "filled"),
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

    out = _run(bw.tick(db_path=db, provider=_Provider("success", "filled"),
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


def test_a_solana_destination_is_measured_by_the_watcher(monkeypatch):
    """A bridge INTO Solana stores Relay's pseudo chain id. The watcher names it
    through the provider's alias and reads lamports, never `eth_getBalance`."""
    import asyncio
    from core.wallet import bridge_guard, bridge_status, bridge_watcher

    bridge_status.register_chain_alias(792703809, "solana")
    settled = {}
    row = {"id": "b1", "user_id": "owner", "dest_chain_id": 792703809,
           "origin_chain_id": 8453, "recipient": "So1anaRecipient111111111111111111111111111",
           "currency_out": "11111111111111111111111111111111",
           "balance_before": "100", "min_out_raw": "50", "created_at": 0.0,
           "request_id": "0xreq", "tx_ref": "0xabc"}
    monkeypatch.setattr(bridge_guard, "open_bridges_all", lambda **k: [row])
    monkeypatch.setattr(bridge_guard, "settle",
                        lambda bid, **k: settled.update(k, bid=bid))
    monkeypatch.setattr("core.wallet.solana_onchain.native_balance_raw",
                        lambda addr, **k: 200)
    monkeypatch.setattr(bridge_guard, "native_balance_raw",
                        lambda *a: (_ for _ in ()).throw(AssertionError("EVM read")))

    async def _notify(*a, **k):
        return None
    class _Ok:
        def status(self, rid):
            return "success", "filled"
    monkeypatch.setattr(bridge_status, "read_out_txs", lambda rid: ["5igSig"])
    monkeypatch.setattr("core.wallet.solana_onchain.tx_inflow_at",
                        lambda sig, owner, mint=None, **k: (100, 10))
    res = asyncio.run(bridge_watcher.tick(provider=_Ok(), notify=_notify,
                                          now=lambda: 10.0))
    assert res.arrived == 1
    assert settled["state"] == bridge_guard.STATE_ARRIVED

    # A balance rise with no delivering transaction (a lab swap fill) is not one.
    settled.clear()
    monkeypatch.setattr(bridge_status, "read_out_txs", lambda rid: [])
    res = asyncio.run(bridge_watcher.tick(provider=_Ok(), notify=_notify,
                                          now=lambda: 10.0))
    assert res.arrived == 0 and not settled


def test_a_row_closed_as_never_sent_tells_the_owner_once(db):
    """085 L-6: the stale-row sweep removes the row from every open view, so a
    rare funded-but-unrecorded order would vanish silently. The owner gets ONE
    notice that names the origin chain and the request id; a second tick is
    silent."""
    from core.sqlite_util import execute_retry
    from core.wallet import tx_notify
    quote = SimpleNamespace(
        request_id="0xstale", origin_chain_id=BASE_ID, dest_chain_id=42161,
        recipient=RECIPIENT, currency_out="0x0000000000000000000000000000000000000000",
        amount_in_raw=10 ** 9, min_out_raw=FLOOR)
    bid = bridge_guard.record_pending(user_id="owner", quote=quote, amount_usd=12.5,
                                      balance_before=BEFORE, db_path=db)
    execute_retry(db, "UPDATE bridges SET created_at=? WHERE id=?",
                  (time.time() - bridge_guard.UNSENT_STALE_SEC - 60, bid))
    seen, notify = _collect_notices()
    _run(bw.tick(db_path=db, provider=_Provider(), read_balance=lambda a, c: BEFORE,
                 notify=notify))
    assert len(seen) == 1
    user, notice = seen[0]
    assert user == "owner" and notice.state == tx_notify.STATE_NEVER_SENT
    assert notice.chain == "base" and notice.route == "base→arbitrum"
    assert notice.extra_refs == ("0xstale", bid) and notice.usd == 12.5
    text = tx_notify.render_settled(notice)
    assert "NEVER SENT" in text and "re-send" in text and "0xstale" in text
    _run(bw.tick(db_path=db, provider=_Provider(), read_balance=lambda a, c: BEFORE,
                 notify=notify))
    assert len(seen) == 1, "one notice per closed row, never repeated"


def test_a_stale_row_relay_reports_filled_sends_no_notice(db):
    from core.sqlite_util import execute_retry
    bid = bridge_guard.record_pending(user_id="owner", quote=_quote("0xfilled"),
                                      amount_usd=1.0, balance_before=BEFORE, db_path=db)
    execute_retry(db, "UPDATE bridges SET created_at=? WHERE id=?",
                  (time.time() - bridge_guard.UNSENT_STALE_SEC - 60, bid))
    seen, notify = _collect_notices()
    _run(bw.tick(db_path=db, provider=_Provider("success", "filled"),
                 read_balance=lambda a, c: None, notify=notify))
    assert [n.state for _, n in seen if n.state == "never_sent"] == []
    assert bid in {r["id"] for r in bridge_guard.open_bridges("owner", db_path=db)}
