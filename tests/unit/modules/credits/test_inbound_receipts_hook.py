"""067 P5a: the ledger's inbound leg is the credits.inbound_receipts hook."""
import asyncio
import pathlib

import modules.credits.unified_ledger as L


def test_the_credits_ledger_names_no_payment_table():
    src = pathlib.Path(L.__file__).read_text()
    assert "x402_payment_requests" not in src


def test_the_x402_rail_registers_the_reader():
    import modules.x402.inbound_receipts as R
    assert L._inbound_reader() is R.inbound_receipts


def test_no_reader_is_unavailable_never_zero(monkeypatch):
    monkeypatch.setattr(L, "_INBOUND_READER", None)
    monkeypatch.setattr(L, "_receipts_loaded", True)
    out = asyncio.run(L._inbound_leg(object(), "u1", 7))
    assert out["inbound_available"] is False and out["income_usd"] == 0.0


def test_a_registered_reader_is_the_leg(monkeypatch):
    seen = []

    async def reader(db, uid, days):
        seen.append((uid, days))
        return {"income_usd": 5.0, "settled_payments": 1, "pending_invoices_usd": 0.0,
                "pending_invoices": 0, "refund_due_usd": 0.0, "refund_due_count": 0}
    monkeypatch.setattr(L, "_INBOUND_READER", None)
    L.register_inbound_receipts(reader)
    out = asyncio.run(L._inbound_leg(object(), "u1", 3))
    assert seen == [("u1", 3)] and out["income_usd"] == 5.0 and out["inbound_available"] is True
    assert list(out)[-1] == "inbound_available"
