"""Room invoices expire only while pending; an unconfirmed submission stays held."""
import json
import uuid

import pytest

from modules.x402 import invoicing

TREASURY = "0x" + "11" * 20
ROB = "0x" + "bb" * 20


async def _mint(db, *, kind, status, deadline, stale=False):
    rid = f"inv_{uuid.uuid4().hex[:12]}"
    meta = {"kind": kind, "tenant_id": "u1", "wake_delivered": False}
    updated = "datetime('now', '-1 hour')" if stale else "datetime('now')"
    await db.execute(
        f"""INSERT INTO x402_payment_requests(id,amount,amount_usd,asset,chain,
               recipient,nonce,deadline,status,metadata,
               asset_id,asset_address,asset_decimals,amount_raw,
               created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),{updated})""",
        (rid, "0.5", 0.5, "rob", "robinhood", TREASURY, rid, deadline,
         status, json.dumps(meta), "rob", ROB, 18, str(5 * 10 ** 18)))
    return rid


async def _status(db, rid):
    row = await db.fetch_one("SELECT status FROM x402_payment_requests WHERE id=?", (rid,))
    return row["status"]


@pytest.mark.asyncio
async def test_room_action_invoice_expires_past_its_deadline(x402_db):
    room = await _mint(x402_db, kind="room_action", status="pending", deadline=1)
    agent = await _mint(x402_db, kind="agent_invoice", status="pending", deadline=1)
    other = await _mint(x402_db, kind="subscription_note", status="pending", deadline=1)
    await invoicing.expire_stale_requests(db=x402_db)
    assert await _status(x402_db, room) == "expired"
    assert await _status(x402_db, agent) == "expired"
    assert await _status(x402_db, other) == "pending"   # not a payable kind


@pytest.mark.asyncio
async def test_room_action_unconfirmed_submission_is_not_reopened(x402_db):
    room = await _mint(x402_db, kind="room_action", status="settling",
                       deadline=9_999_999_999, stale=True)
    reverted = await invoicing.revert_stale_settling(max_age_seconds=600, db=x402_db)
    assert reverted == []
    assert await _status(x402_db, room) == "settling"
