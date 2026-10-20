"""085 x402in L1: `/pay` completes an invoice only when the facilitator's tx
receipt carries a Transfer of the asset to the treasury for the full amount."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from modules.x402.facilitator import SettlementPending, receipt_pays

TOKEN = "0x" + "33" * 20
TREASURY = "0x" + "22" * 20
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _log(to, value, token=TOKEN):
    return {"address": token, "data": hex(value),
            "topics": [TRANSFER, "0x" + "0" * 24 + "11" * 20, "0x" + "0" * 24 + to[2:]]}


def _call(receipt):
    def call(method, params):
        assert method == "eth_getTransactionReceipt"
        return receipt
    return call


def _pays(receipt, **kw):
    return receipt_pays("0xtx", chain="base", token=TOKEN, pay_to=TREASURY,
                        min_raw=10_000, call=_call(receipt), sleep=lambda s: None, **kw)


def test_a_transfer_to_the_treasury_for_the_amount_proves_it():
    assert _pays({"status": "0x1", "logs": [_log(TREASURY, 10_000)]}) is True


def test_a_transfer_elsewhere_or_short_or_another_token_does_not():
    assert _pays({"status": "0x1", "logs": [_log("0x" + "99" * 20, 10_000)]}) is False
    assert _pays({"status": "0x1", "logs": [_log(TREASURY, 9_999)]}) is False
    assert _pays({"status": "0x1",
                  "logs": [_log(TREASURY, 10_000, token="0x" + "44" * 20)]}) is False


def test_a_reverted_tx_is_false_and_a_missing_receipt_is_unknown():
    assert _pays({"status": "0x0", "logs": [_log(TREASURY, 10_000)]}) is False
    assert _pays(None, attempts=3) is None


@pytest.mark.asyncio
async def test_pay_holds_the_invoice_when_the_receipt_does_not_prove_payment(monkeypatch):
    """The facilitator says settled, the chain says otherwise: SettlementPending,
    so `/pay` answers 202 `settling` and never completes the invoice."""
    from api import x402_endpoints as ep

    class _Client:
        async def verify_and_settle_payment(self, *, payment_header,
                                            payment_requirements, before_settle):
            return (SimpleNamespace(isValid=True),
                    SimpleNamespace(success=True, transaction="0x" + "a" * 64))

    monkeypatch.setattr("fastapi_x402.init_x402", lambda **k: None)
    monkeypatch.setattr("modules.x402.facilitator.get_facilitator_client", lambda: _Client())
    monkeypatch.setattr(ep, "_invoice_asset_cfg", lambda chain: SimpleNamespace(
        decimals=6, address=TOKEN, eip712_name="USDC", eip712_version="2"))
    seen = {}

    def _fake_receipt(tx, **kw):
        seen.update(kw, tx=tx)
        return False
    monkeypatch.setattr("modules.x402.facilitator.receipt_pays", _fake_receipt)
    noted = []

    async def _note(rid, tx, **kw):
        noted.append((rid, tx))
        return True
    monkeypatch.setattr("modules.x402.settlement_holds.note_facilitator_tx", _note)
    row = {"chain": "base", "amount_usd": 0.01, "recipient": TREASURY, "purpose": "svc"}
    with pytest.raises(SettlementPending):
        await ep._verify_and_settle_invoice("inv", row, "hdr")
    assert seen["pay_to"] == TREASURY and seen["token"] == TOKEN
    assert seen["min_raw"] == 10_000
    # The held row keeps the facilitator's tx: it is what later proves it.
    assert noted == [("inv", "0x" + "a" * 64)]

    # The natural path: a receipt that proves it completes as before.
    monkeypatch.setattr("modules.x402.facilitator.receipt_pays", lambda tx, **kw: True)
    ok, tx, err = await ep._verify_and_settle_invoice("inv", row, "hdr")
    assert ok is True and tx == "0x" + "a" * 64 and err is None


@pytest.mark.asyncio
async def test_pay_endpoint_answers_202_when_held(monkeypatch):
    from api import x402_endpoints as ep
    monkeypatch.setenv("X402_INVOICE_ENABLED", "true")
    row = {"status": "pending", "amount_usd": 5.0, "chain": "base",
           "recipient": TREASURY, "purpose": "svc"}
    settle = AsyncMock(return_value=True)
    with patch("modules.x402.invoicing.get_payment_request", new=AsyncMock(return_value=row)), \
         patch("modules.x402.invoicing.claim_for_settlement", new=AsyncMock(return_value=True)), \
         patch("modules.x402.invoicing.settle_payment_request", new=settle), \
         patch.object(ep, "_verify_and_settle_invoice",
                      new=AsyncMock(side_effect=SettlementPending("not proven"))):
        resp = await ep.pay_invoice("inv_x", SimpleNamespace(headers={"X-PAYMENT": "hdr"}))
    assert resp.status_code == 202
    settle.assert_not_awaited()


# -- a held /pay invoice resolves once the chain proves it ------------------

import base64  # noqa: E402
import json  # noqa: E402

PAYER = "0x" + "11" * 20
NONCE = "0x" + "44" * 32
FTX = "0x" + "ab" * 32


async def _held_invoice(db, rid="inv_held"):
    from fastapi_x402.models import PaymentRequirements

    from modules.x402 import invoicing, settlement_holds
    from modules.x402.settlement_attempt import authorization_record, mark_invoice_submitted
    req = PaymentRequirements(network="base", maxAmountRequired="10000",
                              resource=f"/api/x402/requests/{rid}/pay",
                              payTo=TREASURY, asset=TOKEN)
    header = base64.b64encode(json.dumps({"payload": {"authorization": {
        "from": PAYER, "nonce": NONCE, "validBefore": 9999999999}}}).encode()).decode()
    await db.execute(
        "INSERT INTO x402_payment_requests (id,amount,amount_usd,asset,chain,recipient,"
        "nonce,deadline,metadata) VALUES (?,'10000',0.01,'usdc','base',?,?,9999999999,"
        "'{\"kind\":\"invoice\"}')", (rid, TREASURY, rid))
    assert await invoicing.claim_for_settlement(rid, db=db)
    await mark_invoice_submitted(rid, authorization_record(header, req), db=db)
    assert await settlement_holds.note_facilitator_tx(rid, FTX.upper().replace("0X", "0x"),
                                                      db=db)
    return rid


async def _row(db, rid):
    return await db.fetch_one(
        "SELECT status, transaction_hash FROM x402_payment_requests WHERE id=?", (rid,))


@pytest.mark.asyncio
@pytest.mark.parametrize("pays", [True, False])
async def test_the_resolver_completes_a_held_invoice_by_its_receipt(x402_db, pays):
    """`/pay` held it (receipt unreadable in the poll window). After the hold
    window the authorization reads USED; the recorded facilitator tx's receipt
    then decides: a Transfer to the treasury completes it, anything else stays
    held for the owner."""
    from modules.x402.settlement_watcher import SettlementWatcher
    rid = await _held_invoice(x402_db)
    await x402_db.execute(
        "UPDATE x402_payment_requests SET updated_at=datetime('now','-1 hour')")

    def rpc(method, params):
        if method == "eth_chainId":
            return hex(8453)
        if method == "eth_call":
            return "0x" + "0" * 63 + "1"            # authorization used
        assert method == "eth_getTransactionReceipt" and params == [FTX]
        to = TREASURY if pays else "0x" + "99" * 20
        return {"status": "0x1", "logs": [_log(to, 10_000)]}
    w = SettlementWatcher(task_agent=None, db=x402_db, rpc_call=rpc)
    assert await w._resolve_held_submissions() == []      # never "payable again"
    row = await _row(x402_db, rid)
    if pays:
        assert row["status"] == "completed" and row["transaction_hash"] == FTX
    else:
        assert row["status"] == "settling"


@pytest.mark.asyncio
async def test_the_scan_completes_a_held_invoice_when_its_tx_pays_the_treasury(x402_db):
    """The on-chain scan sees the facilitator's transfer land: it completes the
    held invoice instead of reporting the money as unmatched."""
    from modules.x402.settlement_watcher import SettlementWatcher
    rid = await _held_invoice(x402_db)
    w = SettlementWatcher(task_agent=None, db=x402_db)
    unmatched = []

    async def _flag(transfer, treasury):
        unmatched.append(transfer)
    w._notify_unmatched = _flag
    other = {"tx_hash": "0x" + "cd" * 32, "from": PAYER, "to": TREASURY,
             "amount_raw": 10_000, "log_index": 0, "block": 1}
    settled, flagged = await w._settle_or_flag(
        [other], TREASURY, "base", asset_address=TOKEN, decimals=6)
    assert (settled, flagged) == (0, 1)
    assert (await _row(x402_db, rid))["status"] == "settling"
    short = dict(other, tx_hash=FTX, amount_raw=9_999)
    assert (await w._settle_or_flag([short], TREASURY, "base", asset_address=TOKEN,
                                    decimals=6))[0] == 0
    real = dict(other, tx_hash=FTX)
    settled, flagged = await w._settle_or_flag(
        [real], TREASURY, "base", asset_address=TOKEN, decimals=6)
    assert settled == 1
    row = await _row(x402_db, rid)
    assert row["status"] == "completed" and row["transaction_hash"] == FTX
