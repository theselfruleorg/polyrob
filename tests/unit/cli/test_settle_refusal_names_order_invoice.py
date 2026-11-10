"""An order's invoice settles only with a transaction hash: a refused /settle names that
cause on both owner seats (Telegram and the REPL)."""
import pytest

from tests.unit.cli.test_repl_owner_verbs import _ctx, _invoice_db  # noqa: F401

CAUSE = "or it is an order's invoice: give the transaction hash"


@pytest.mark.asyncio
async def test_telegram_settle_refusal_names_the_order_invoice(monkeypatch):
    from surfaces.telegram import owner_ops

    async def _rows(**kwargs):
        return [{"request_id": "inv-1", "amount_usd": 5.0, "status": "pending"}]

    async def _settle(request_id, *, transaction_hash=None, db=None):
        return False
    monkeypatch.setattr("modules.x402.invoicing.list_payment_requests", _rows)
    monkeypatch.setattr("modules.x402.invoicing.settle_payment_request", _settle)
    out = await owner_ops.settle_reply("alice", ["inv-1"])
    assert "not settled" in out and CAUSE in out


def test_repl_settle_refusal_names_the_order_invoice(_invoice_db):
    from cli.ui.commands.h_owner import h_settle
    ctx, rec = _ctx(args=["not-a-real-id"], user_id="u1")
    h_settle(ctx)
    assert "not settled" in rec.text and CAUSE in rec.text
