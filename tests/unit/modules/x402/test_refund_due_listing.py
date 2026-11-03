"""098 review: a refund obligation is listed where the owner looks for it.

Only machine-payment rows reach ``refund_due`` (the A2A/OpenAI-compat payer
whose request then failed). The shared invoice reader filtered on the
``agent_invoice`` kind only, so ``/invoices refund_due`` and
``/invoices refunded <id>`` never found the row the ledger counted as owed.
"""
import json

import pytest

from modules.x402.invoicing import list_payment_requests


async def _seed(db, rid, status, kind, tenant):
    await db.execute(
        "INSERT INTO x402_payment_requests (id,amount,amount_usd,asset,chain,recipient,nonce,"
        "deadline,status,metadata) VALUES (?,'10000',0.01,'usdc','base','0xt',?,9999999999,?,?)",
        (rid, rid, status, json.dumps({"kind": kind, "tenant_id": tenant})))


@pytest.mark.asyncio
async def test_refund_due_listing_includes_machine_payments(x402_db):
    await _seed(x402_db, "mp1", "refund_due", "machine_payment", "owner")
    await _seed(x402_db, "mp2", "refund_due", "machine_payment", "someone_else")
    await _seed(x402_db, "mp3", "refunded", "machine_payment", "owner")
    await _seed(x402_db, "inv1", "pending", "agent_invoice", "owner")

    owed = await list_payment_requests(user_id="owner", status="refund_due", db=x402_db)
    assert [r["request_id"] for r in owed] == ["mp1"]  # tenant-scoped
    done = await list_payment_requests(user_id="owner", status="refunded", db=x402_db)
    assert [r["request_id"] for r in done] == ["mp3"]


@pytest.mark.asyncio
async def test_receivable_listings_stay_agent_invoices_only(x402_db):
    await _seed(x402_db, "mp1", "completed", "machine_payment", "owner")
    await _seed(x402_db, "inv1", "completed", "agent_invoice", "owner")
    rows = await list_payment_requests(user_id="owner", status="completed", db=x402_db)
    assert [r["request_id"] for r in rows] == ["inv1"]
    rows = await list_payment_requests(user_id="owner", db=x402_db)
    assert [r["request_id"] for r in rows] == ["inv1"]
