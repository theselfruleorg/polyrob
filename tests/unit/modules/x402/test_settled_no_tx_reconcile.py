"""098 W1 MO-21: a payment recorded with no on-chain tx (`settled_no_tx`) is
UNPROVEN: never income or 8004 reputation, one owner notice after 24 h, and a
tx completes it."""
import time

import pytest

from modules.x402 import invoice_reaper, invoicing
from modules.x402.invoice_status import INCOME_STATUSES, X402_INVOICE


async def _row(db, rid="m1"):
    await db.execute(
        "INSERT INTO x402_payment_requests (id,amount,amount_usd,asset,chain,recipient,nonce,"
        "deadline,status,metadata) VALUES (?,'10000',0.01,'usdc','base','0xt',?,1,"
        "'settled_no_tx','{\"kind\":\"machine_payment\",\"tenant_id\":\"u1\"}')", (rid, rid))
    return rid


@pytest.mark.asyncio
async def test_used_nonce_found_tx_completes(x402_db):
    rid = await _row(x402_db)
    assert await invoicing.settle_payment_request(rid, db=x402_db) is False  # no tx, no move
    assert await invoicing.settle_payment_request(rid, transaction_hash="0x" + "ab" * 32,
                                                  db=x402_db)
    row = await x402_db.fetch_one("SELECT status FROM x402_payment_requests WHERE id=?", (rid,))
    assert row["status"] == "completed"


@pytest.mark.asyncio
async def test_unused_nonce_after_24h_notifies_once_and_leaves_income(x402_db):
    rid = await _row(x402_db)
    assert "settled_no_tx" not in INCOME_STATUSES
    assert await invoice_reaper.escalate_unproven(db=x402_db) == []
    later = time.time() + 2 * 86400
    out = await invoice_reaper.escalate_unproven(db=x402_db, now=later)
    assert [(o["request_id"], o["reason"]) for o in out] == [(rid, "no_tx_unproven")]
    assert await invoice_reaper.escalate_unproven(db=x402_db, now=later) == []
    from modules.x402.inbound_receipts import inbound_receipts
    got = await inbound_receipts(x402_db, "u1", 30)
    assert got["income_usd"] == 0.0


def test_scan_pass_never_completes_settled_no_tx():
    assert X402_INVOICE.allowed("settled_no_tx", "completed")
    from modules.eip8004 import reputation
    assert reputation._SETTLED_STATUSES == ("completed",)


PAYER = "0x" + "a1" * 20
NONCE = "0x" + "b2" * 32
ASSET = "0x" + "c3" * 20


async def _auth_row(db, rid="m2", amount_usd=2.5):
    import json
    meta = {"kind": "machine_payment", "tenant_id": "u1",
            "authorization": {"payer": PAYER, "nonce": NONCE, "asset": ASSET,
                              "network": "base"}}
    await db.execute(
        "INSERT INTO x402_payment_requests (id,amount,amount_usd,asset,chain,recipient,nonce,"
        "deadline,status,metadata) VALUES (?,'10000',?,'usdc','base','0xt',?,1,"
        "'settled_no_tx',?)", (rid, amount_usd, rid, json.dumps(meta)))
    return rid


def _caller(answer, calls):
    async def call_for(chain):
        def call(method, params):
            calls.append((chain, method, params))
            if isinstance(answer, Exception):
                raise answer
            return answer
        return call
    return call_for


@pytest.mark.asyncio
async def test_authorization_used_on_chain_is_stamped_never_income(x402_db):
    """098 close-out MO-21: the chain read stamps `authorization_used`; the row
    stays unproven (a cancel also uses a nonce) — its own ledger line."""
    rid = await _auth_row(x402_db)
    calls = []
    out = await invoice_reaper.check_no_tx_authorizations(_caller("0x" + "0" * 63 + "1", calls),
                                                          db=x402_db)
    assert [o["request_id"] for o in out] == [rid]
    chain, method, params = calls[0]
    assert (chain, method) == ("base", "eth_call") and params[0]["to"] == ASSET
    assert params[0]["data"] == "0xe94a0102" + PAYER[2:].rjust(64, "0") + NONCE[2:]
    row = await x402_db.fetch_one("SELECT status FROM x402_payment_requests WHERE id=?", (rid,))
    assert row["status"] == "settled_no_tx"
    # once: a stamped row is not read again
    assert await invoice_reaper.check_no_tx_authorizations(_caller("0x1", calls), db=x402_db,
                                                           now=time.time() + 7200) == []
    assert len(calls) == 1
    from modules.credits.unified_ledger import build_ledger, format_ledger
    led = await build_ledger("u1", days=30, db=x402_db)
    t = led["treasury"]
    assert t["income_usd"] == 0.0 and t["net_usd"] == round(-t["spend_usd"], 6)
    assert (t["unproven_usd"], t["unproven_count"], t["unproven_auth_used_count"]) == (2.5, 1, 1)
    assert "unproven: $2.5000 across 1 payment(s)" in format_ledger(led)


@pytest.mark.asyncio
async def test_unused_or_unreadable_authorization_stays_unproven(x402_db):
    rid = await _auth_row(x402_db)
    calls = []
    assert await invoice_reaper.check_no_tx_authorizations(
        _caller(RuntimeError("rpc down"), calls), db=x402_db) == []
    assert await invoice_reaper.check_no_tx_authorizations(
        _caller("0x" + "0" * 64, calls), db=x402_db) == []
    # checked: not read again within the hour, read again after it
    assert await invoice_reaper.check_no_tx_authorizations(
        _caller("0x" + "0" * 64, calls), db=x402_db) == []
    assert len(calls) == 2
    await invoice_reaper.check_no_tx_authorizations(_caller("0x" + "0" * 64, calls), db=x402_db,
                                                    now=time.time() + 3700)
    assert len(calls) == 3
    from modules.x402.inbound_receipts import inbound_receipts
    got = await inbound_receipts(x402_db, "u1", 30)
    assert (got["income_usd"], got["unproven_count"], got["unproven_auth_used_count"]) == \
        (0.0, 1, 0)
    row = await x402_db.fetch_one("SELECT status FROM x402_payment_requests WHERE id=?", (rid,))
    assert row["status"] == "settled_no_tx"


@pytest.mark.asyncio
async def test_row_without_authorization_is_not_read(x402_db):
    await _row(x402_db)          # legacy: no payer/nonce/asset to ask about
    calls = []
    assert await invoice_reaper.check_no_tx_authorizations(_caller("0x1", calls),
                                                           db=x402_db) == []
    assert calls == []
