"""A dapp page's transaction is booked at broadcast with no receipt read, so it is
booked ``pending_receipt``: the receipt resolver settles it, and a revert leaves
the daily caps instead of staying in them for 24 h."""
import pytest

from core.money.ledger import SpendLedger
from core.money.receipt_state import open_receipts, voided_refs
from core.wallet.receipt_resolver import resolve_pending_receipts
from core.wallet.tx_guard import Decision
from tests.unit.tools.dapp_browser.test_bridge import (POOL, _ask, _Rail, _Wallet)
from tools.dapp_browser import bridge as B

TX = "0x" + "ee" * 32


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
    return SpendLedger(max_per_tx_usd=100, daily_cap_usd=50)


def _bridge(gate):
    def _guard(intent, tx, **kw):
        return Decision(allowed=True, reason="test", lane="autonomous",
                        amount_usd=2.0, sim_gas_used=None)
    envelope = B.Envelope(chain="base", max_spend_usd=50.0, session_budget_usd=100.0,
                          allow_contracts=(), approval_timeout_sec=0.5)
    return B.WalletBridge(
        envelope=envelope, wallet=_Wallet(gate), execution_context=None,
        rail_factory=_Rail, guard_fn=_guard, price_fn=lambda c, a: 2500.0,
        rpc_fn=lambda chain, method, params: "0xdeadbeef",
        approver=None, armed_origin="https://app.example")


@pytest.mark.asyncio
async def test_a_dapp_call_is_booked_pending_and_a_revert_leaves_the_caps(gate):
    out = await _ask(_bridge(gate), "eth_sendTransaction",
                     [{"to": POOL, "data": "0xdeadbeef", "value": "0x0"}])
    assert out["result"] == TX
    [entry] = open_receipts(gate.audit_log)
    assert entry["action"] == "dapp_call" and entry["chain"] == "base"
    assert entry["result_ref"] == TX
    assert gate.rolling_24h_spend_usd() == 2.0

    def call(chain, method, params):
        if method == "eth_chainId":
            return hex(8453)
        return {"transactionHash": TX, "status": "0x0", "blockNumber": "0x3"}
    await resolve_pending_receipts(gate, call=call)
    assert voided_refs(gate.audit_log) == {TX}
    assert gate.rolling_24h_spend_usd() == 0.0
