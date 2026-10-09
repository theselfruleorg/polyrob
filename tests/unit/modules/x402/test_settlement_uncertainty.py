"""A lost facilitator response must not lose the payment or invite a second one."""
import asyncio
import base64
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi_x402.models import PaymentRequirements

from modules.x402 import invoicing
from modules.x402.facilitator import StrictFacilitator, SettlementPending
from modules.x402.settlement_attempt import (
    authorization_record, mark_invoice_submitted, prepare_machine_payment,
)

PAYER = "0x" + "11" * 20
REQ = PaymentRequirements(network="base", maxAmountRequired="10000", resource="/v1/chat/completions",
                          payTo="0x" + "22" * 20, asset="0x" + "33" * 20)
HEADER = base64.b64encode(json.dumps({"payload": {"signature": "private-signature",
    "authorization": {"from": PAYER, "nonce": "0x" + "44" * 32,
                      "validBefore": 9999999999}}}).encode()).decode()


def _meta(row):
    return row['metadata'] if isinstance(row['metadata'], dict) else json.loads(row['metadata'])


def _adapter(http):
    return StrictFacilitator(SimpleNamespace(client=http, verify_url="https://x402.org/verify",
        settle_url="https://x402.org/settle", is_coinbase_cdp=False,
        _create_coinbase_headers=lambda endpoint: {}))


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_submitted_invoice_remains_claimed_after_timeout_or_cancel(x402_db, cancel):
    rid = "invoice-test"
    await x402_db.execute(
        "INSERT INTO x402_payment_requests (id,amount,amount_usd,asset,chain,recipient,nonce,deadline,metadata) "
        "VALUES (?,'10000',0.01,'usdc','base',?,? ,9999999999,'{}')", (rid, REQ.payTo, rid))
    assert await invoicing.claim_for_settlement(rid, db=x402_db)
    async def before_settle():
        await mark_invoice_submitted(rid, authorization_record(HEADER, REQ), db=x402_db)
    async def respond(request):
        if request.url.path == "/verify":
            return httpx.Response(200, json={"isValid": True})
        row = await x402_db.fetch_one("SELECT * FROM x402_payment_requests WHERE id=?", (rid,))
        assert _meta(row)['facilitator_submitted'] == 1
        assert "private-signature" not in row['metadata']
        raise asyncio.CancelledError() if cancel else httpx.ReadTimeout("lost response")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(asyncio.CancelledError if cancel else SettlementPending) as caught:
            await _adapter(http).verify_and_settle_payment(HEADER, REQ, before_settle=before_settle)
        if not cancel:
            assert isinstance(caught.value.__cause__, httpx.ReadTimeout)
    await invoicing.revert_settlement_claim(rid, db=x402_db)
    await x402_db.execute("UPDATE x402_payment_requests SET updated_at=datetime('now','-1 day')")
    assert await invoicing.revert_stale_settling(db=x402_db) == []
    assert not await invoicing.claim_for_settlement(rid, db=x402_db)
    # A proven receipt can complete the same row; no replacement invoice needed.
    assert await invoicing.settle_payment_request(rid, transaction_hash="0x"+'a'*64, db=x402_db)


@pytest.mark.asyncio
async def test_machine_submission_is_durable_and_replayed_authorization_cannot_send(x402_db):
    details = authorization_record(HEADER, REQ)
    calls = []
    async def before_settle():
        await prepare_machine_payment(details, amount_usd=.01, user_id="payer", tenant_id="owner", db=x402_db)
    async def respond(request):
        calls.append(request.url.path)
        if request.url.path == "/verify":
            return httpx.Response(200, json={"isValid": True})
        raise httpx.ReadTimeout("lost response")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        for _ in range(2):
            with pytest.raises(SettlementPending):
                await _adapter(http).verify_and_settle_payment(HEADER, REQ, before_settle=before_settle)
    assert calls.count('/settle') == 1
    row = await x402_db.fetch_one("SELECT * FROM x402_payment_requests WHERE id=?", (details['id'],))
    assert row['status'] == 'settling' and row['payer_address'] == PAYER
    assert _meta(row)['tenant_id'] == 'owner'
    assert 'private-signature' not in row['metadata']


@pytest.mark.asyncio
async def test_unavailable_journal_stops_submission_before_post():
    calls = []
    async def fail_record():
        raise OSError("read-only ledger")
    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"isValid": True})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(OSError):
            await _adapter(http).verify_and_settle_payment(HEADER, REQ, before_settle=fail_record)
    assert calls == ['/verify']


@pytest.mark.asyncio
async def test_machine_success_completes_the_original_attempt_once(x402_db):
    from modules.x402.x402_integration import record_x402_payment
    details = authorization_record(HEADER, REQ)
    rid = await prepare_machine_payment(details, amount_usd=.01, user_id='payer', tenant_id='owner', db=x402_db)
    await x402_db.execute("INSERT INTO user_profiles (user_id, wallet_address, role, tier) VALUES ('payer','0xpayer','user','x402')")
    kwargs = dict(payment_id=rid, attempt_id=rid, wallet_address=PAYER, user_id='payer',
        amount_usd=.01, network='base', recipient=REQ.payTo, transaction_hash='0x'+'a'*64,
        tenant_id='owner', db=x402_db)
    assert await record_x402_payment(**kwargs)
    assert not await record_x402_payment(**kwargs)
    rows = await x402_db.fetch_all('SELECT * FROM x402_payment_requests')
    assert len(rows) == 1 and rows[0]['status'] == 'completed'
    assert _meta(rows[0])['authorization']['nonce'] == '0x'+'44'*32


@pytest.mark.asyncio
async def test_middleware_reports_pending_without_starting_service(x402_db, monkeypatch):
    from starlette.requests import Request
    from modules.x402 import middleware as M, settlement_attempt as S
    monkeypatch.setenv('X402_PAYMENT_RECIPIENT', REQ.payTo)
    monkeypatch.setenv('X402_DEFAULT_CHAIN', 'base')
    async def database(db=None):
        return x402_db
    monkeypatch.setattr(S, 'resolve_db', database)
    async def resolve(addr):
        return 'payer'
    monkeypatch.setattr(M, 'resolve_payer_user_id', resolve)
    def respond(request):
        if request.url.path == '/verify':
            return httpx.Response(200, json={'isValid': True})
        raise httpx.ReadTimeout('lost response')
    async def downstream(request):
        pytest.fail('service cannot run on an unconfirmed payment')
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        obj = M.X402PaymentMiddleware(lambda *a: None, enabled=False)
        obj._facilitator_client = _adapter(http)
        request = Request({'type': 'http', 'method': 'POST', 'path': '/a2a/rpc',
                           'headers': [], 'server': ('test', 80), 'scheme': 'http', 'query_string': b''})
        response = await obj._handle_x402_payment(request, downstream, HEADER)
    assert response.status_code == 202
    body = json.loads(response.body)
    assert body['status'] == 'settling' and body['payment_id'].startswith('x402_auth_')
    row = await x402_db.fetch_one('SELECT * FROM x402_payment_requests WHERE id=?', (body['payment_id'],))
    assert row['status'] == 'settling'
