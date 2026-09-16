"""Settlement matches on (recipient, asset_address, amount_raw) integers.

⚠️ The defect this closes: `match_pending_invoice_by_amount` compared a FLOAT
`amount_usd` treasury-wide with NO asset filter. That was safe only because
exactly one contract was ever queried. With a second asset scannable, a $1
payment in a worthless token settles a $1 USDC invoice.
"""
import json
import uuid

import pytest

from modules.x402 import invoicing

TREASURY = "0x" + "11" * 20
USDC = "0x" + "aa" * 20
ROB = "0x" + "bb" * 20


async def _mint(db, *, asset_id, address, decimals, raw, usd,
                kind="agent_invoice", chain="base"):
    rid = f"inv_{uuid.uuid4().hex[:12]}"
    await db.execute(
        """INSERT INTO x402_payment_requests(id,user_id,amount,amount_usd,asset,
               chain,recipient,nonce,deadline,status,metadata,
               asset_id,asset_address,asset_decimals,amount_raw,
               created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))""",
        (rid, None, str(usd), usd, "x", chain, TREASURY, rid, 9_999_999_999,
         "pending", json.dumps({"kind": kind, "tenant_id": "u1"}),
         asset_id, address, decimals, str(raw)))
    return rid


@pytest.mark.asyncio
async def test_a_payment_in_one_asset_never_settles_an_invoice_in_another(x402_db):
    """THE 046 §4.3.3 defect, asserted."""
    usdc_inv = await _mint(x402_db, asset_id="usdc-base", address=USDC,
                           decimals=6, raw=1_000_000, usd=1.0)
    hit = await invoicing.match_pending_invoice(
        TREASURY, ROB, 1_000_000, db=x402_db)
    assert hit is None, "a ROB transfer settled a USDC invoice"
    same = await invoicing.match_pending_invoice(
        TREASURY, USDC, 1_000_000, db=x402_db)
    assert same["request_id"] == usdc_inv


@pytest.mark.asyncio
async def test_an_18_decimal_amount_matches_exactly(x402_db):
    raw = 7 * 10 ** 18 + 3
    rid = await _mint(x402_db, asset_id="rob", address=ROB, decimals=18,
                      raw=raw, usd=0.5, chain="robinhood")
    hit = await invoicing.match_pending_invoice(TREASURY, ROB, raw, db=x402_db)
    assert hit["request_id"] == rid
    assert await invoicing.match_pending_invoice(
        TREASURY, ROB, raw - 1, db=x402_db) is None


@pytest.mark.asyncio
async def test_the_oldest_pending_invoice_wins_on_a_tie(x402_db):
    first = await _mint(x402_db, asset_id="rob", address=ROB, decimals=18,
                        raw=10 ** 18, usd=1.0, chain="robinhood")
    await _mint(x402_db, asset_id="rob", address=ROB, decimals=18,
                raw=10 ** 18, usd=1.0, chain="robinhood")
    hit = await invoicing.match_pending_invoice(TREASURY, ROB, 10 ** 18, db=x402_db)
    assert hit["request_id"] == first


@pytest.mark.asyncio
async def test_a_legacy_row_with_null_asset_columns_still_matches_as_usdc(x402_db):
    """⚠️ A pre-046 row has NULL asset_address and NULL amount_raw. It must stay
    settleable, or every invoice outstanding at deploy time is orphaned."""
    rid = f"inv_{uuid.uuid4().hex[:12]}"
    await x402_db.execute(
        """INSERT INTO x402_payment_requests(id,user_id,amount,amount_usd,asset,
               chain,recipient,nonce,deadline,status,metadata,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))""",
        (rid, None, "1.25", 1.25, "usdc", "base", TREASURY, rid, 9_999_999_999,
         "pending", json.dumps({"kind": "agent_invoice", "tenant_id": "u1"})))
    hit = await invoicing.match_pending_invoice(
        TREASURY, USDC, 1_250_000, decimals=6, db=x402_db)
    assert hit is not None and hit["request_id"] == rid


@pytest.mark.asyncio
async def test_a_legacy_row_is_NOT_matched_without_decimals(x402_db):
    """Without decimals there is no honest way to turn a raw integer into the
    USD figure the legacy row stores — skip rather than match wrongly."""
    rid = f"inv_{uuid.uuid4().hex[:12]}"
    await x402_db.execute(
        """INSERT INTO x402_payment_requests(id,user_id,amount,amount_usd,asset,
               chain,recipient,nonce,deadline,status,metadata,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))""",
        (rid, None, "1.25", 1.25, "usdc", "base", TREASURY, rid, 9_999_999_999,
         "pending", json.dumps({"kind": "agent_invoice"})))
    assert await invoicing.match_pending_invoice(
        TREASURY, USDC, 1_250_000, db=x402_db) is None


@pytest.mark.asyncio
async def test_a_kind_outside_the_allowed_set_is_not_matched(x402_db):
    """⚠️ The kind filter is why a room-action invoice needs kinds= — without
    it a paid offer sits pending forever with the money already received."""
    await _mint(x402_db, asset_id="rob", address=ROB, decimals=18,
                raw=10 ** 18, usd=1.0, kind="room_action", chain="robinhood")
    assert await invoicing.match_pending_invoice(
        TREASURY, ROB, 10 ** 18, db=x402_db) is None
    hit = await invoicing.match_pending_invoice(
        TREASURY, ROB, 10 ** 18, kinds=("agent_invoice", "room_action"),
        db=x402_db)
    assert hit is not None
    assert hit["kind"] == "room_action"


@pytest.mark.asyncio
async def test_a_settled_invoice_is_never_rematched(x402_db):
    rid = await _mint(x402_db, asset_id="usdc-base", address=USDC, decimals=6,
                      raw=1_000_000, usd=1.0)
    await x402_db.execute(
        "UPDATE x402_payment_requests SET status='completed' WHERE id=?", (rid,))
    assert await invoicing.match_pending_invoice(
        TREASURY, USDC, 1_000_000, db=x402_db) is None


@pytest.mark.asyncio
async def test_the_legacy_by_amount_shim_still_works(x402_db):
    """Kept so no caller breaks mid-series."""
    from core.payments.assets import resolve
    usdc = resolve("usdc-base")
    rid = await _mint(x402_db, asset_id="usdc-base", address=usdc.address,
                      decimals=6, raw=1_250_000, usd=1.25)
    hit = await invoicing.match_pending_invoice_by_amount(
        1.25, TREASURY, db=x402_db)
    assert hit["request_id"] == rid


@pytest.mark.asyncio
async def test_a_zero_amount_never_matches_anything(x402_db):
    await _mint(x402_db, asset_id="usdc-base", address=USDC, decimals=6,
                raw=1_000_000, usd=1.0)
    assert await invoicing.match_pending_invoice(
        TREASURY, USDC, 0, db=x402_db) is None
