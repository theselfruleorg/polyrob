"""085 W6 (B-1, B-2, B-3, bridge L-6): an arrival proof binds to THIS bridge.

Relay's destination hashes are a third party's claim. Each hash counts once,
only when it is not older than the bridge, and only for ONE bridge row.
"""
import sqlite3
import time
from types import SimpleNamespace

import pytest

from core.wallet import bridge_guard as bg
from core.wallet import bridge_status

SOL_RECIPIENT = "So1anaRecipient111111111111111111111111111"
EVM_RECIPIENT = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
FLOOR = 100


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "bridges.db")


def _quote(request_id="0xreq", dest=8453, recipient=EVM_RECIPIENT):
    return SimpleNamespace(
        request_id=request_id, origin_chain_id=1, dest_chain_id=dest,
        recipient=recipient, currency_out="0x0000000000000000000000000000000000000000",
        amount_in_raw=10 ** 9, min_out_raw=FLOOR)


def _row(db, request_id):
    bid = bg.record_pending(user_id="owner", quote=_quote(request_id),
                            amount_usd=1.0, balance_before=0, db_path=db)
    return bid, bg.created_at_of(bid, db_path=db)


def _svm_proof(monkeypatch, *, hashes, inflow, bid=None, created_at=None, db=None):
    monkeypatch.setattr(bridge_status, "read_out_txs", lambda rid: list(hashes))
    monkeypatch.setattr("core.wallet.solana_onchain.tx_inflow_at",
                        lambda sig, owner, mint=None, **k: inflow[sig])
    return bg.ArrivalProof(
        request_id="0xreq", recipient=SOL_RECIPIENT, currency_out=None,
        chain_name="solana", svm=True, bridge_id=bid, dest_chain_id=792703809,
        created_at=created_at, min_out_raw=FLOOR, db_path=db)


# -- B-1 -------------------------------------------------------------------

def test_a_repeated_hash_proves_ONE_inflow(monkeypatch):
    """`[h, h]` where h paid 60% of the floor must not read as 120%."""
    now = time.time()
    prove = _svm_proof(monkeypatch, hashes=["h", "h"], inflow={"h": (60, int(now))},
                       created_at=now)
    assert prove() == 60
    assert prove() < FLOOR


def test_an_old_hash_from_before_the_bridge_does_not_count(monkeypatch):
    """An earlier delivery (or any older transfer to us) is not this bridge."""
    now = time.time()
    prove = _svm_proof(monkeypatch, hashes=["old"],
                       inflow={"old": (FLOOR * 5, int(now - 3600))}, created_at=now)
    assert prove() == 0


def test_a_hash_inside_the_clock_skew_still_counts(monkeypatch):
    now = time.time()
    prove = _svm_proof(monkeypatch, hashes=["h"],
                       inflow={"h": (FLOOR, int(now - bg.PROOF_SKEW_SEC + 5))},
                       created_at=now)
    assert prove() == FLOOR


def test_a_hash_claimed_by_another_bridge_row_is_refused(monkeypatch, db):
    now = time.time()
    first, c1 = _row(db, "0xfirst")
    second, c2 = _row(db, "0xsecond")
    inflow = {"h": (FLOOR, int(now))}
    assert _svm_proof(monkeypatch, hashes=["h"], inflow=inflow, bid=first,
                      created_at=c1, db=db)() == FLOOR
    # The same delivering tx offered as the SECOND bridge's arrival.
    assert _svm_proof(monkeypatch, hashes=["h"], inflow=inflow, bid=second,
                      created_at=c2, db=db)() == 0
    # ...while the first row may re-prove its own claim (a later watcher pass).
    assert _svm_proof(monkeypatch, hashes=["h"], inflow=inflow, bid=first,
                      created_at=c1, db=db)() == FLOOR


def test_the_claim_table_is_unique_per_chain_and_hash(db):
    bid, _ = _row(db, "0xa")
    other, _ = _row(db, "0xb")
    assert bg.claim_proof(bid, 8453, ["0xABC"], db_path=db)
    assert not bg.claim_proof(other, 8453, ["0xabc"], db_path=db), \
        "0x-hex folds case: the same tx in another case is the same tx"
    assert bg.claim_proof(other, 10, ["0xabc"], db_path=db), "another chain"


def test_an_existing_store_gains_the_proof_table_in_place(tmp_path):
    """A prod db written before 085 upgrades through `init_schema`."""
    from core.sqlite_util import wal_connect
    db = str(tmp_path / "legacy.db")
    conn = wal_connect(db)
    conn.execute("""CREATE TABLE bridges (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL, request_id TEXT NOT NULL,
        origin_chain_id INTEGER NOT NULL, dest_chain_id INTEGER NOT NULL,
        recipient TEXT NOT NULL, currency_out TEXT NOT NULL,
        amount_in_raw TEXT NOT NULL, min_out_raw TEXT NOT NULL,
        amount_usd REAL, state TEXT NOT NULL DEFAULT 'pending', tx_ref TEXT,
        balance_before TEXT, balance_after TEXT, detail TEXT,
        created_at REAL NOT NULL, settled_at REAL)""")
    conn.commit()
    conn.close()
    bg.open_bridges_all(db_path=db)       # a read migrates an existing store
    conn = sqlite3.connect(db)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    conn.close()
    assert "bridge_proofs" in names and "idx_bridge_proofs_hash" in names


# -- B-2: EVM proves from the destination tx AND the balance ---------------

class _Provider:
    def __init__(self, state="success"):
        self.state = state

    def status(self, rid):
        return self.state, "filled"


def _evm_await(monkeypatch, *, balances, paid, db, bid, created_at):
    monkeypatch.setattr(bridge_status, "read_out_txs", lambda rid: ["0x" + "1" * 64])
    monkeypatch.setattr(bg, "evm_tx_inflow",
                        lambda h, r, c, n, **k: (paid, int(time.time())))
    prove = bg.arrival_prover("0xreq", EVM_RECIPIENT, None, "base", bridge_id=bid,
                              dest_chain_id=8453, created_at=created_at,
                              min_out_raw=FLOOR, db_path=db)
    assert prove is not None and prove.requires_delta
    seq = list(balances)
    ticks = iter([0.0, 1.0, 999.0, 999.0])
    return bg.await_arrival(
        provider=_Provider(), request_id="0xreq", recipient=EVM_RECIPIENT,
        chain_name="base", min_out_raw=FLOOR, balance_before=0, deadline_sec=10,
        poll_sec=0, now=lambda: next(ticks), sleep=lambda s: None,
        read_balance=lambda a, c: seq.pop(0) if len(seq) > 1 else seq[0],
        prove=prove)


def test_evm_arrival_needs_the_destination_tx_and_the_balance(monkeypatch, db):
    bid, created = _row(db, "0xreq")
    out = _evm_await(monkeypatch, balances=[FLOOR], paid=FLOOR, db=db, bid=bid,
                     created_at=created)
    assert out.state == bg.STATE_ARRIVED


def test_an_unrelated_balance_rise_without_a_delivering_tx_is_not_arrival(
        monkeypatch, db):
    """A swap fill or an invoice payment raises the balance too (B-2)."""
    bid, created = _row(db, "0xreq")
    out = _evm_await(monkeypatch, balances=[FLOOR * 3], paid=0, db=db, bid=bid,
                     created_at=created)
    assert out.state == bg.STATE_IN_FLIGHT


def test_a_delivering_tx_without_the_balance_rise_is_not_arrival(monkeypatch, db):
    """The balance delta stays as the second check."""
    bid, created = _row(db, "0xreq")
    out = _evm_await(monkeypatch, balances=[0], paid=FLOOR, db=db, bid=bid,
                     created_at=created)
    assert out.state == bg.STATE_IN_FLIGHT


def test_evm_tx_inflow_sums_only_token_transfers_to_the_recipient(monkeypatch):
    monkeypatch.setattr("core.wallet.onchain.rpc_url_for_chain", lambda c: "https://rpc.example")
    to_us = "0x" + "0" * 24 + EVM_RECIPIENT[2:].lower()
    to_other = "0x" + "0" * 24 + "ab" * 20
    logs = [
        {"address": USDC_BASE, "topics": [TRANSFER, "0x" + "0" * 64, to_us],
         "data": hex(70)},
        {"address": USDC_BASE, "topics": [TRANSFER, "0x" + "0" * 64, to_other],
         "data": hex(999)},
        {"address": "0x" + "cd" * 20, "topics": [TRANSFER, "0x" + "0" * 64, to_us],
         "data": hex(999)},
        {"address": USDC_BASE, "topics": [TRANSFER, "0x" + "0" * 64, to_us],
         "data": hex(30)},
    ]

    def rpc(method, params):
        if method == "eth_getTransactionReceipt":
            return {"status": "0x1", "blockNumber": "0x10", "logs": logs}
        if method == "eth_getBlockByNumber":
            return {"timestamp": hex(1_700_000_000)}
        raise AssertionError(method)

    assert bg.evm_tx_inflow("0xh", EVM_RECIPIENT, USDC_BASE, "base", rpc=rpc) == (
        100, 1_700_000_000)


def test_evm_tx_inflow_native_direct_fill_and_reverted_tx(monkeypatch):
    monkeypatch.setattr("core.wallet.onchain.rpc_url_for_chain", lambda c: "https://rpc.example")
    status = {"v": "0x1"}

    def rpc(method, params):
        if method == "eth_getTransactionReceipt":
            return {"status": status["v"], "blockNumber": "0x10", "logs": []}
        if method == "eth_getBlockByNumber":
            return {"timestamp": hex(5)}
        if method == "eth_getTransactionByHash":
            return {"to": EVM_RECIPIENT.lower(), "value": hex(123)}
        raise AssertionError(method)

    assert bg.evm_tx_inflow("0xh", EVM_RECIPIENT, None, "base", rpc=rpc) == (123, 5)
    status["v"] = "0x0"
    assert bg.evm_tx_inflow("0xh", EVM_RECIPIENT, None, "base", rpc=rpc) is None


# -- B-3 -------------------------------------------------------------------

def test_a_reused_request_id_raises_instead_of_replacing_the_row(db):
    """`INSERT OR REPLACE` deleted the in-flight row; a plain INSERT refuses."""
    first = bg.record_pending(user_id="owner", quote=_quote("0xsame"),
                              amount_usd=1.0, balance_before=0, db_path=db)
    bg.settle(first, state=bg.STATE_IN_FLIGHT, detail="x", tx_ref="0xsig", db_path=db)
    with pytest.raises(sqlite3.IntegrityError):
        bg.record_pending(user_id="owner", quote=_quote("0xsame"),
                          amount_usd=1.0, balance_before=0, db_path=db)
    rows = bg.open_bridges("owner", db_path=db)
    assert [r["id"] for r in rows] == [first], "the first row must survive"


# -- L-6 -------------------------------------------------------------------

def _age(db, bid, seconds):
    from core.sqlite_util import execute_retry
    execute_retry(db, "UPDATE bridges SET created_at=? WHERE id=?",
                  (time.time() - seconds, bid))


def test_a_stale_pending_row_with_no_tx_is_closed_as_never_sent(db):
    stale = bg.record_pending(user_id="owner", quote=_quote("0xstale"),
                              amount_usd=1.0, balance_before=0, db_path=db)
    sent = bg.record_pending(user_id="owner", quote=_quote("0xsent"),
                             amount_usd=1.0, balance_before=0, db_path=db)
    young = bg.record_pending(user_id="owner", quote=_quote("0xyoung"),
                              amount_usd=1.0, balance_before=0, db_path=db)
    filled = bg.record_pending(user_id="owner", quote=_quote("0xfilled"),
                               amount_usd=1.0, balance_before=0, db_path=db)
    bg.mark_sent(sent, "0xsig", db_path=db)
    for bid in (stale, sent, filled):
        _age(db, bid, bg.UNSENT_STALE_SEC + 60)

    def status(rid):
        return ("success", "filled") if rid == "0xfilled" else ("pending", "waiting")

    assert bg.close_stale_unsent(status=status, db_path=db) == 1
    open_ids = {r["id"] for r in bg.open_bridges("owner", db_path=db)}
    assert stale not in open_ids
    assert {sent, young, filled} <= open_ids
    # It no longer blocks a new bridge into the same balance.
    assert bg.conflicting_open_bridge(dest_chain_id=8453, recipient=EVM_RECIPIENT,
                                      currency_out=None, db_path=db)["id"] != stale


def test_the_sweep_never_creates_the_store(tmp_path):
    import os
    db = str(tmp_path / "none.db")
    assert bg.close_stale_unsent(db_path=db) == 0
    assert not os.path.exists(db)


# -- the balance-only fallback (no destination hash from Relay) -------------

def _evm_await_hashes(monkeypatch, *, hashes, balances, paid, db, bid, created_at):
    monkeypatch.setattr(bridge_status, "read_out_txs", lambda rid: hashes)
    monkeypatch.setattr(bg, "evm_tx_inflow",
                        lambda h, r, c, n, **k: (paid, int(time.time())))
    prove = bg.arrival_prover("0xreq", EVM_RECIPIENT, None, "base", bridge_id=bid,
                              dest_chain_id=8453, created_at=created_at,
                              min_out_raw=FLOOR, db_path=db)
    seq = list(balances)
    ticks = iter([0.0, 1.0, 999.0, 999.0])
    return bg.await_arrival(
        provider=_Provider(), request_id="0xreq", recipient=EVM_RECIPIENT,
        chain_name="base", min_out_raw=FLOOR, balance_before=0, deadline_sec=10,
        poll_sec=0, now=lambda: next(ticks), sleep=lambda s: None,
        read_balance=lambda a, c: seq.pop(0) if len(seq) > 1 else seq[0],
        prove=prove)


def test_no_destination_hash_on_evm_accepts_the_fresh_balance_delta(monkeypatch, db):
    """Relay says success but names no tx: the rise over the in-reserve
    baseline is accepted, and the detail records the balance-only basis."""
    bid, created = _row(db, "0xreq")
    out = _evm_await_hashes(monkeypatch, hashes=[], balances=[FLOOR], paid=0,
                            db=db, bid=bid, created_at=created)
    assert out.state == bg.STATE_ARRIVED
    assert "balance-only" in out.detail
    # Below the floor it is still not an arrival.
    out = _evm_await_hashes(monkeypatch, hashes=[], balances=[FLOOR - 1], paid=0,
                            db=db, bid=bid, created_at=created)
    assert out.state == bg.STATE_IN_FLIGHT


def test_returned_hashes_keep_the_strict_rule(monkeypatch, db):
    """When Relay DOES name a tx, the tx must prove it; a balance rise does not
    rescue a hash that pays nothing."""
    bid, created = _row(db, "0xreq")
    out = _evm_await_hashes(monkeypatch, hashes=["0x" + "2" * 64],
                            balances=[FLOOR * 3], paid=0, db=db, bid=bid,
                            created_at=created)
    assert out.state == bg.STATE_IN_FLIGHT


def test_an_unreadable_hash_list_never_falls_back(monkeypatch, db):
    bid, created = _row(db, "0xreq")
    out = _evm_await_hashes(monkeypatch, hashes=None, balances=[FLOOR * 3], paid=0,
                            db=db, bid=bid, created_at=created)
    assert out.state == bg.STATE_IN_FLIGHT


def test_solana_never_falls_back_to_balance_only(monkeypatch):
    prove = _svm_proof(monkeypatch, hashes=[], inflow={}, created_at=time.time())
    assert prove() is None and prove.basis is None


def test_the_watcher_records_the_balance_only_basis(monkeypatch, db):
    import asyncio
    from core.wallet import bridge_watcher as bw
    bid = bg.record_pending(user_id="owner", quote=_quote("0xreq"), amount_usd=1.0,
                            balance_before=0, db_path=db)
    bg.settle(bid, state=bg.STATE_IN_FLIGHT, detail="x", tx_ref="0xsig", db_path=db)
    monkeypatch.setattr(bridge_status, "read_out_txs", lambda rid: [])

    async def _notify(*a, **k):
        return None
    out = asyncio.run(bw.tick(db_path=db, provider=_Provider(),
                              read_balance=lambda a, c: FLOOR, notify=_notify))
    assert out.arrived == 1
    from core.sqlite_util import execute_retry
    row = execute_retry(db, "SELECT detail FROM bridges WHERE id=?", (bid,), fetch="one")
    assert "balance-only" in row[0]


def test_relay_status_without_tx_hashes_reads_as_empty_not_unknown(monkeypatch):
    from tools.defi.providers import relay_bridge
    p = relay_bridge.RelayBridgeProvider()
    monkeypatch.setattr(p, "_get", lambda url, timeout=None: {"status": "success"})
    assert p.out_tx_hashes("0xreq") == []

    def _boom(url, timeout=None):
        raise OSError("down")
    monkeypatch.setattr(p, "_get", _boom)
    assert p.out_tx_hashes("0xreq") is None
