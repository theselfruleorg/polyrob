"""The receipt resolver reads the SHARED ledger: a pending receipt another process
booked (a CLI `polyrob run` next to the server) is resolved by the idle server's
resolver without waiting for that server's own next money verb."""
import pytest

from core.money.ledger import SpendLedger
from core.money.receipt_state import receipt_states
from core.wallet.audit_sink import JsonlAuditSink
from core.wallet.receipt_resolver import resolve_pending_receipts

TX = "0x" + "ab" * 32


class _Journal:
    def unresolved(self):
        return []

    def mark_booked(self, *a, **k):
        pass


@pytest.fixture(autouse=True)
def _hooks(monkeypatch):
    import core.money.hooks as hooks
    monkeypatch.setattr(hooks, "submission_journal", lambda: _Journal())
    monkeypatch.setattr("core.config_policy.AutonomyConfig.autonomy_halted",
                        staticmethod(lambda: False))


def _call(chain, method, params):
    if method == "eth_chainId":
        return hex(8453)
    return {"transactionHash": TX, "status": "0x0", "blockNumber": "0x10"}


@pytest.mark.asyncio
async def test_the_idle_server_resolves_a_pending_receipt_the_cli_booked(tmp_path):
    path = str(tmp_path / "audit.jsonl")
    server = SpendLedger(max_per_tx_usd=100, daily_cap_usd=50,
                         audit_sink=JsonlAuditSink(path, seal_key=None))
    cli = SpendLedger(max_per_tx_usd=100, daily_cap_usd=50,
                      audit_sink=JsonlAuditSink(path, seal_key=None))
    cli.record(venue="defi", action="swap", amount_usd=40, counterparty=None,
               idempotency_key="k1", result_ref=TX, chain="base",
               receipt_state="pending_receipt")

    out = await resolve_pending_receipts(server, call=_call)

    assert [o["state"] for o in out] == ["reverted"]
    reread = JsonlAuditSink(path, seal_key=None)
    assert receipt_states(reread) == {TX: "reverted"}
