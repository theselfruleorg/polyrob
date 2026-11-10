"""098 close-out TS-23: a money tx booked before its receipt was known
(`receipt_state = pending_receipt`) gets ONE final state from the resolver —
confirmed, reverted (out of the rolling caps) or unknown after 24 h."""
import time

import pytest

from core.money.ledger import SpendLedger
from core.money.receipt_state import TX_RECEIPT, open_receipts, receipt_states
from core.wallet.receipt_resolver import resolve_pending_receipts

TX = "0x" + "ab" * 32


class _Journal:
    def unresolved(self):
        return []

    def mark_booked(self, *a, **k):
        pass


@pytest.fixture
def gate(monkeypatch):
    import core.money.hooks as hooks
    monkeypatch.setattr(hooks, "submission_journal", lambda: _Journal())
    monkeypatch.setattr("core.config_policy.AutonomyConfig.autonomy_halted",
                        staticmethod(lambda: False))
    g = SpendLedger(max_per_tx_usd=100, daily_cap_usd=50)
    g.record(venue="defi", action="swap", amount_usd=40, counterparty=None,
             idempotency_key="k1", result_ref=TX, chain="base",
             receipt_state="pending_receipt")
    return g


def _caller(receipt, calls=None):
    def call(chain, method, params):
        if calls is not None:
            calls.append(method)
        if method == "eth_chainId":
            return hex(8453)
        return receipt
    return call


@pytest.mark.asyncio
async def test_reverted_in_flight_tx_voids_cap_entry(gate):
    assert not gate.check(venue="defi", amount_usd=20, idempotency_key="k2").allowed
    out = await resolve_pending_receipts(gate, call=_caller(
        {"transactionHash": TX, "status": "0x0", "blockNumber": "0x10"}))
    assert [o["state"] for o in out] == ["reverted"]
    assert gate.rolling_24h_spend_usd() == 0.0
    assert gate.check(venue="defi", amount_usd=20, idempotency_key="k2").allowed


@pytest.mark.asyncio
async def test_confirmed_keeps_the_spend_and_resolver_is_idempotent(gate):
    call = _caller({"transactionHash": TX, "status": "0x1", "blockNumber": "0x10"})
    out = await resolve_pending_receipts(gate, call=call)
    assert [o["state"] for o in out] == ["confirmed"]
    assert await resolve_pending_receipts(gate, call=call) == []      # second tick
    assert gate.note_receipt(gate.audit_log[0], "reverted") is False   # race: one wins
    assert receipt_states(gate.audit_log) == {TX: "confirmed"}
    assert gate.rolling_24h_spend_usd() == 40.0
    assert open_receipts(gate.audit_log) == []


@pytest.mark.asyncio
async def test_no_receipt_stays_pending_then_unknown_after_24h(gate):
    assert await resolve_pending_receipts(gate, call=_caller(None)) == []
    assert receipt_states(gate.audit_log) == {TX: "pending_receipt"}
    out = await resolve_pending_receipts(gate, call=_caller(None), now=time.time() + 90000)
    assert [o["state"] for o in out] == ["unknown"]
    assert gate.rolling_24h_spend_usd() == 40.0     # unknown is not a refund


@pytest.mark.asyncio
async def test_rpc_error_or_wrong_chain_leaves_it_pending(gate):
    def boom(chain, method, params):
        raise OSError("rpc down")
    assert await resolve_pending_receipts(gate, call=boom) == []

    def other_chain(chain, method, params):
        return hex(1) if method == "eth_chainId" else {"transactionHash": TX, "status": "0x0",
                                                       "blockNumber": "0x1"}
    assert await resolve_pending_receipts(gate, call=other_chain) == []
    assert receipt_states(gate.audit_log) == {TX: "pending_receipt"}


def test_a_normal_record_has_no_receipt_state():
    g = SpendLedger(max_per_tx_usd=100)
    g.record(venue="defi", action="swap", amount_usd=1, counterparty=None,
             idempotency_key=None, result_ref=TX)
    assert "receipt_state" not in g.audit_log[0] and open_receipts(g.audit_log) == []
    assert TX_RECEIPT.validate() == []


def test_interrupted_record_is_pending_receipt():
    from tools.defi.receipt_wait import record_interrupted
    g = SpendLedger(max_per_tx_usd=100)
    record_interrupted(TX, gate=g, record_kw=dict(
        venue="defi", action="swap", amount_usd=1, counterparty=None,
        idempotency_key=None, result_ref=TX, chain="base"))
    assert g.audit_log[0]["receipt_state"] == "pending_receipt"


# -- the ONE record after a receipt wait (tools/defi/receipt_wait.book_after_receipt) --

TX2 = "0x" + "cd" * 32


@pytest.fixture
def empty_gate(monkeypatch):
    import core.money.hooks as hooks
    monkeypatch.setattr(hooks, "submission_journal", lambda: _Journal())
    monkeypatch.setattr("core.config_policy.AutonomyConfig.autonomy_halted",
                        staticmethod(lambda: False))
    return SpendLedger(max_per_tx_usd=100, daily_cap_usd=50)


def _book(gate, status):
    from types import SimpleNamespace

    from tools.defi.receipt_wait import book_after_receipt
    receipt = None if status is None else SimpleNamespace(status=status, block_number=7)
    book_after_receipt(gate, receipt, venue="defi", action="transfer", amount_usd=40,
                       counterparty=None, idempotency_key="k9", result_ref=TX2, chain="base")


def test_a_revert_seen_in_the_wait_leaves_the_caps(empty_gate):
    _book(empty_gate, "failed")
    assert receipt_states(empty_gate.audit_log)[TX2] == "reverted"
    assert empty_gate.check(venue="defi", amount_usd=20, idempotency_key="k2").allowed


def test_a_success_counts_against_the_caps(empty_gate):
    _book(empty_gate, "success")
    assert TX2 not in receipt_states(empty_gate.audit_log)
    assert not empty_gate.check(venue="defi", amount_usd=20, idempotency_key="k2").allowed


@pytest.mark.parametrize("status", ["pending", None])
def test_a_timed_out_or_unread_receipt_is_left_for_the_resolver(empty_gate, status):
    _book(empty_gate, status)
    assert [e["result_ref"] for e in open_receipts(empty_gate.audit_log)] == [TX2]
