"""085 x402in L1 (machine payments): the middleware serves a paid request only
when the facilitator's tx receipt pays the treasury. Unreadable in the bounded
window keeps the unknown-outcome semantics (202, no service) and the watcher
resolves the attempt row later; mined-but-not-paying is refused."""
import base64
import json
from types import SimpleNamespace

import httpx
import pytest

from modules.x402.facilitator import StrictFacilitator
from modules.x402.facilitator import receipt_pays as REAL_RECEIPT_PAYS

PAYER = "0x" + "11" * 20
TREASURY = "0x" + "22" * 20
NONCE = "0x" + "44" * 32
FTX = "0x" + "ab" * 32
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
HEADER = base64.b64encode(json.dumps({"payload": {"authorization": {
    "from": PAYER, "nonce": NONCE, "validBefore": 9999999999}}}).encode()).decode()


def _adapter():
    def respond(request):
        if request.url.path == "/verify":
            return httpx.Response(200, json={"isValid": True})
        return httpx.Response(200, json={"success": True, "transaction": FTX,
                                         "network": "base"})
    http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    return http, StrictFacilitator(SimpleNamespace(
        client=http, verify_url="https://x402.org/verify",
        settle_url="https://x402.org/settle", is_coinbase_cdp=False,
        _create_coinbase_headers=lambda endpoint: {}))


async def _run(x402_db, monkeypatch, proof):
    """Drive the REAL middleware settle path; ``proof`` is receipt_pays's answer."""
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    from modules.x402 import middleware as M, settlement_attempt as S
    from modules.x402.middleware import install_auth_state_writer
    from api.auth_state import set_auth_state
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", TREASURY)
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")
    install_auth_state_writer(set_auth_state)

    await x402_db.execute(
        "INSERT OR IGNORE INTO user_profiles (user_id, wallet_address, role, tier) "
        "VALUES ('payer', ?, 'user', 'x402')", (PAYER,))

    async def database(db=None):
        return x402_db
    monkeypatch.setattr(S, "resolve_db", database)
    monkeypatch.setattr("modules.x402.settlement_holds._resolve_db", database)

    async def resolve(addr):
        return "payer"
    monkeypatch.setattr(M, "resolve_payer_user_id", resolve)

    async def ensure(*a, **k):
        return True
    monkeypatch.setattr(M, "ensure_user_profile_for_payer", ensure)

    async def record(**kwargs):
        from modules.x402.x402_integration import record_x402_payment
        return await record_x402_payment(**kwargs, db=x402_db)
    monkeypatch.setattr(M, "record_x402_payment", record)
    seen = {}

    def fake_receipt(tx, **kw):
        seen.update(kw, tx=tx)
        return proof
    monkeypatch.setattr("modules.x402.facilitator.receipt_pays", fake_receipt)
    served = []

    async def downstream(request):
        served.append(True)
        return JSONResponse({"ok": True})
    http, adapter = _adapter()
    async with http:
        obj = M.X402PaymentMiddleware(lambda *a: None, enabled=False)
        obj._facilitator_client = adapter
        request = Request({"type": "http", "method": "POST", "path": "/a2a/rpc",
                           "headers": [], "server": ("test", 80), "scheme": "http",
                           "query_string": b""})
        response = await obj._handle_x402_payment(request, downstream, HEADER)
    return response, served, seen


def _meta(row):
    m = row["metadata"]
    return m if isinstance(m, dict) else json.loads(m)


async def _only_row(db):
    rows = await db.fetch_all("SELECT * FROM x402_payment_requests")
    assert len(rows) == 1
    return dict(rows[0])


@pytest.mark.asyncio
async def test_a_receipt_that_pays_serves_the_request(x402_db, monkeypatch):
    response, served, seen = await _run(x402_db, monkeypatch, True)
    assert response.status_code == 200 and served == [True]
    assert seen["tx"] == FTX and seen["pay_to"] == TREASURY and seen["chain"] == "base"
    assert seen["deadline_sec"] <= 10        # bounded request-path latency
    row = await _only_row(x402_db)
    assert row["status"] == "completed" and row["transaction_hash"] == FTX


@pytest.mark.asyncio
async def test_a_receipt_that_does_not_pay_is_refused(x402_db, monkeypatch):
    response, served, _ = await _run(x402_db, monkeypatch, False)
    assert response.status_code == 402 and served == []
    row = await _only_row(x402_db)
    assert row["status"] == "settling"       # held for the owner, never income


@pytest.mark.asyncio
async def test_an_unreadable_receipt_answers_202_then_the_watcher_resolves_it(
        x402_db, monkeypatch):
    from modules.x402.settlement_watcher import SettlementWatcher
    response, served, _ = await _run(x402_db, monkeypatch, None)
    assert response.status_code == 202 and served == []
    row = await _only_row(x402_db)
    assert row["status"] == "settling"
    assert _meta(row)["facilitator_tx"] == FTX

    # Later: the chain answers. The resolver completes the attempt row — as
    # refund_due, since the payer was told no service was started.
    told = []

    async def tell(container, payment_id):
        told.append(payment_id)
    monkeypatch.setattr("modules.x402.x402_integration._tell_owner_refund_due", tell)
    monkeypatch.setattr("modules.x402.facilitator.receipt_pays", REAL_RECEIPT_PAYS)
    await x402_db.execute(
        "UPDATE x402_payment_requests SET updated_at=datetime('now','-1 hour')")

    def rpc(method, params):
        if method == "eth_chainId":
            return hex(8453)
        assert method == "eth_getTransactionReceipt" and params == [FTX]
        asset = _meta(row)["authorization"]["asset"]
        return {"status": "0x1", "logs": [{
            "address": asset, "data": hex(10 ** 9),
            "topics": [TRANSFER, "0x" + "0" * 24 + PAYER[2:],
                       "0x" + "0" * 24 + TREASURY[2:]]}]}
    w = SettlementWatcher(task_agent=SimpleNamespace(container=object()), db=x402_db,
                          rpc_call=rpc)
    assert await w._resolve_held_submissions() == []
    row = await _only_row(x402_db)
    assert row["status"] == "refund_due" and row["transaction_hash"] == FTX
    assert told == [row["id"]]

