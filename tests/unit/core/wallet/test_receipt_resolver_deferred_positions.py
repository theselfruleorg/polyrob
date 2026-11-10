"""MW-5: a swap whose receipt was still pending when the wait ended stores its
position deltas on the ledger entry; the receipt resolver writes them to the book
ONCE, only when the receipt confirms. A revert or an unknown outcome writes none."""
import time

import pytest

from core.exec_identity import reset_exec_identity, set_exec_identity
from core.money.ledger import SpendLedger
from core.open_positions import PositionDelta
from core.wallet.receipt_resolver import resolve_pending_receipts

TX = "0x" + "ab" * 32
TOKEN = "0x" + "77" * 20
ACCOUNT = "0x" + "AB" * 20


class _Journal:
    def unresolved(self):
        return []

    def mark_booked(self, *a, **k):
        pass


@pytest.fixture
def book(monkeypatch):
    import core.money.hooks as hooks
    calls = []
    monkeypatch.setattr(hooks, "submission_journal", lambda: _Journal())
    monkeypatch.setattr(hooks, "position_book",
                        lambda: (lambda uid, deltas: calls.append((uid, list(deltas)))))
    monkeypatch.setattr("core.config_policy.AutonomyConfig.autonomy_halted",
                        staticmethod(lambda: False))
    return calls


def _leg():
    return PositionDelta(chain="base", address=TOKEN, symbol="TKN", qty=5.0,
                         cost_usd=40.0, qty_source="simulation")


def _gate(*, account=None):
    gate = SpendLedger(max_per_tx_usd=100, daily_cap_usd=50)
    token = set_exec_identity("owner-1", "s1")
    try:
        gate.record(venue="defi", action="swap", amount_usd=40, counterparty=None,
                    idempotency_key="k1", result_ref=TX, chain="base", account=account,
                    receipt_state="pending_receipt", deferred_positions=[_leg()])
    finally:
        reset_exec_identity(token)
    return gate


def _call(status):
    def call(chain, method, params):
        if method == "eth_chainId":
            return hex(8453)
        return {"transactionHash": TX, "status": status, "blockNumber": "0x10"}
    return call


@pytest.mark.asyncio
async def test_pending_then_confirmed_writes_the_position_once(book):
    gate = _gate()
    assert book == []                         # stored, not applied
    entry = gate.audit_log[0]
    assert entry["position_uid"] == "owner-1"
    assert entry["deferred_positions"][0]["address"] == TOKEN

    out = await resolve_pending_receipts(gate, call=_call("0x1"))
    assert [o["state"] for o in out] == ["confirmed"]
    assert book == [("owner-1", [_leg()])]

    assert await resolve_pending_receipts(gate, call=_call("0x1")) == []
    assert len(book) == 1                     # a second pass writes nothing


@pytest.mark.asyncio
async def test_pending_then_reverted_writes_no_position(book):
    gate = _gate()
    out = await resolve_pending_receipts(gate, call=_call("0x0"))
    assert [o["state"] for o in out] == ["reverted"]
    assert book == []


@pytest.mark.asyncio
async def test_pending_then_unknown_writes_no_position(book):
    gate = _gate()

    def none(chain, method, params):
        return hex(8453) if method == "eth_chainId" else None
    out = await resolve_pending_receipts(gate, call=none, now=time.time() + 90000)
    assert [o["state"] for o in out] == ["unknown"]
    assert book == []


@pytest.mark.asyncio
async def test_the_holder_account_is_stamped_lowercase(book):
    gate = _gate(account=ACCOUNT)
    assert gate.audit_log[0]["deferred_positions"][0]["account"] == ACCOUNT.lower()
    await resolve_pending_receipts(gate, call=_call("0x1"))
    assert book[0][1][0].account == ACCOUNT.lower()


def test_no_tenant_stores_nothing(book):
    gate = SpendLedger(max_per_tx_usd=100)
    gate.record(venue="defi", action="swap", amount_usd=1, counterparty=None,
                idempotency_key=None, result_ref=TX, chain="base",
                receipt_state="pending_receipt", deferred_positions=[_leg()])
    assert "deferred_positions" not in gate.audit_log[0]
    assert book == []


@pytest.mark.asyncio
async def test_a_failing_book_write_is_a_warning_and_the_receipt_still_resolves(
        monkeypatch):
    import core.money.hooks as hooks
    monkeypatch.setattr(hooks, "submission_journal", lambda: _Journal())

    def boom(uid, deltas):
        raise OSError("book locked")
    monkeypatch.setattr(hooks, "position_book", lambda: boom)
    gate = _gate()
    # Record the resolver's own warning call: a log capture depends on the logging
    # state that earlier tests of a serial run leave behind (levels, logging.disable).
    import core.wallet.receipt_resolver as resolver
    warned = []
    monkeypatch.setattr(resolver.logger, "warning",
                        lambda msg, *a, **k: warned.append(str(msg) % a if a else str(msg)))
    out = await resolve_pending_receipts(gate, call=_call("0x1"))
    assert [o["state"] for o in out] == ["confirmed"]
    assert any("NOT written" in w for w in warned)
