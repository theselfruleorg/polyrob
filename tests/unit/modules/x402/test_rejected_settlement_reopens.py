"""DEFI-2: a definite settle rejection must reopen an invoice; only an unknown
outcome stays held, and a held one is released only by chain proof.

Before: any rejected settle left ``facilitator_submitted=1`` and nothing ever
reopened it, so anyone holding an invoice id could lock it in 'settling'.
"""
import base64
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi_x402.models import PaymentRequirements

from modules.x402 import invoicing, settlement_holds
from modules.x402.facilitator import SettlementPending, StrictFacilitator
from modules.x402.settlement_attempt import authorization_record, mark_invoice_submitted

PAYER = "0x" + "11" * 20
ASSET = "0x" + "33" * 20
NONCE = "0x" + "44" * 32
REQ = PaymentRequirements(network="base", maxAmountRequired="10000",
                          resource="/api/x402/requests/inv/pay",
                          payTo="0x" + "22" * 20, asset=ASSET)
HEADER = base64.b64encode(json.dumps({"payload": {"authorization": {
    "from": PAYER, "nonce": NONCE, "validBefore": 9999999999}}}).encode()).decode()


def _adapter(http):
    return StrictFacilitator(SimpleNamespace(
        client=http, verify_url="https://x402.org/verify",
        settle_url="https://x402.org/settle", is_coinbase_cdp=False,
        _create_coinbase_headers=lambda endpoint: {}))


async def _settle_with(body):
    def respond(request):
        if request.url.path == "/verify":
            return httpx.Response(200, json={"isValid": True})
        return httpx.Response(200, json=body)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        return await _adapter(http).verify_and_settle_payment(HEADER, REQ)


@pytest.mark.asyncio
async def test_explicit_refusal_is_a_failed_response_not_pending():
    _, settle = await _settle_with({"success": False, "errorReason": "invalid_signature",
                                    "transaction": "", "network": "base"})
    assert settle.success is False


@pytest.mark.asyncio
async def test_refusal_naming_a_transaction_stays_unknown():
    with pytest.raises(SettlementPending):
        await _settle_with({"success": False, "transaction": "0x" + "a" * 64,
                            "network": "base"})


async def _submitted_invoice(db, rid="inv"):
    await db.execute(
        "INSERT INTO x402_payment_requests (id,amount,amount_usd,asset,chain,recipient,"
        "nonce,deadline,metadata) VALUES (?,'10000',0.01,'usdc','base',?,?,9999999999,"
        "'{\"kind\":\"invoice\"}')", (rid, REQ.payTo, rid))
    assert await invoicing.claim_for_settlement(rid, db=db)
    await mark_invoice_submitted(rid, authorization_record(HEADER, REQ), db=db)
    return rid


@pytest.mark.asyncio
async def test_definite_rejection_reopens_for_the_real_payer(x402_db):
    rid = await _submitted_invoice(x402_db)
    await invoicing.revert_settlement_claim(rid, db=x402_db)  # the old no-op
    assert not await invoicing.claim_for_settlement(rid, db=x402_db)
    assert not await settlement_holds.reopen_rejected_settlement(rid, "0x" + "55" * 32, db=x402_db)
    assert await settlement_holds.reopen_rejected_settlement(rid, NONCE, db=x402_db)
    assert await invoicing.claim_for_settlement(rid, db=x402_db)


def _watcher(db, auth_used):
    from modules.x402.settlement_watcher import SettlementWatcher
    calls = []

    def rpc(method, params):
        calls.append(method)
        if method == "eth_chainId":
            return hex(8453)
        assert method == "eth_call" and params[0]["to"] == ASSET
        assert params[0]["data"].startswith("0xe94a0102")
        return "0x" + ("0" * 63) + ("1" if auth_used else "0")
    return SettlementWatcher(task_agent=None, db=db, rpc_call=rpc), calls


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_used", [False, True])
async def test_held_submission_released_only_on_chain_proof(x402_db, auth_used):
    rid = await _submitted_invoice(x402_db)
    w, _ = _watcher(x402_db, auth_used)
    # Fresh: inside the hold window nothing is read or released.
    assert await w._resolve_held_submissions() == []
    await x402_db.execute(
        "UPDATE x402_payment_requests SET updated_at=datetime('now','-1 hour')")
    released = await w._resolve_held_submissions()
    row = await x402_db.fetch_one("SELECT status FROM x402_payment_requests WHERE id=?", (rid,))
    if auth_used:
        assert released == [] and row["status"] == "settling"
    else:
        assert [r["request_id"] for r in released] == [rid]
        assert row["status"] == "pending"


@pytest.mark.asyncio
async def test_held_submission_stays_held_when_the_chain_cannot_be_read(x402_db):
    from modules.x402.settlement_watcher import SettlementWatcher
    rid = await _submitted_invoice(x402_db)
    await x402_db.execute(
        "UPDATE x402_payment_requests SET updated_at=datetime('now','-1 hour')")

    def rpc(method, params):
        if method == "eth_chainId":
            return hex(8453)
        raise RuntimeError("rpc down")
    w = SettlementWatcher(task_agent=None, db=x402_db, rpc_call=rpc)
    assert await w._resolve_held_submissions() == []
    row = await x402_db.fetch_one("SELECT status FROM x402_payment_requests WHERE id=?", (rid,))
    assert row["status"] == "settling"
