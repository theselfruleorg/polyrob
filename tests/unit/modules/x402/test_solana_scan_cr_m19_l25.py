"""CR-M19: the Solana pass settles only a USDC invoice, for its own raw
amount. CR-L25: reference spam does not hide a real payment."""
import json
import types
import uuid

import pytest

from modules.x402 import solana_settlement as ss
from modules.x402.settlement_watcher import SettlementWatcher

TREASURY = "Treas1111111111111111111111111111111111111"
USDC = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"   # testnet mint
OTHER = "Mint2222222222222222222222222222222222222222"


async def _mint(db, *, asset_address, amount_usd, amount_raw):
    rid = f"inv_{uuid.uuid4().hex[:12]}"
    meta = {"kind": "agent_invoice", "chain_family": "svm", "tenant_id": "u1",
            "wake_delivered": False}
    await db.execute(
        """INSERT INTO x402_payment_requests(id,amount,amount_usd,asset,chain,
               recipient,nonce,deadline,status,metadata,
               asset_id,asset_address,asset_decimals,amount_raw,
               created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))""",
        (rid, str(amount_usd), amount_usd, "usdc", "solana", TREASURY, rid,
         9_999_999_999, "pending", json.dumps(meta), "x", asset_address, 6,
         None if amount_raw is None else str(amount_raw)))
    return rid


def _tx(mint, amount):
    return {"meta": {"err": None,
                     "preTokenBalances": [],
                     "postTokenBalances": [{"owner": TREASURY, "mint": mint,
                                            "uiTokenAmount": {"amount": str(amount)}}]}}


def _wire(monkeypatch, sigs_by_ref, txs):
    import modules.x402.settlement_scan as scan
    from core.wallet import solana_onchain
    monkeypatch.setattr(scan, "solana_settle_enabled", lambda: True)
    wallet = types.SimpleNamespace(solana_address=TREASURY, network="testnet")
    monkeypatch.setattr("core.wallet.factory.get_agent_wallet", lambda: wallet)
    calls = {"getTransaction": 0}

    def _rpc(method, params):
        if method == "getSignaturesForAddress":
            ref, opts = params
            sigs = sigs_by_ref.get(ref, [])
            before = opts.get("before")
            start = sigs.index(before) + 1 if before else 0
            return [{"signature": s, "err": None}
                    for s in sigs[start:start + opts["limit"]]]
        if method == "getTransaction":
            calls["getTransaction"] += 1
            return txs.get(params[0])
        raise AssertionError(method)

    monkeypatch.setattr(solana_onchain, "_rpc", _rpc)
    return calls


async def _status(db, rid):
    r = await db.fetch_one("SELECT status FROM x402_payment_requests WHERE id=?", (rid,))
    return r["status"]


@pytest.mark.asyncio
async def test_usdc_payment_never_settles_a_non_usdc_invoice(x402_db, monkeypatch):
    rid = await _mint(x402_db, asset_address=OTHER, amount_usd=1.0, amount_raw=5_000_000)
    ref = ss.reference_for_invoice(rid)
    _wire(monkeypatch, {ref: ["sigA"]}, {"sigA": _tx(USDC, 5_000_000)})
    w = SettlementWatcher(None, db=x402_db)
    settled, _ = await w._scan_solana()
    assert settled == 0
    assert await _status(x402_db, rid) == "pending"


@pytest.mark.asyncio
async def test_the_row_raw_amount_is_the_expected_amount(x402_db, monkeypatch):
    # $1.00 displayed, but the invoice's own raw is 1_000_123 (a tail).
    rid = await _mint(x402_db, asset_address=USDC, amount_usd=1.0, amount_raw=1_000_123)
    ref = ss.reference_for_invoice(rid)
    _wire(monkeypatch, {ref: ["under", "exact"]},
          {"under": _tx(USDC, 1_000_000), "exact": _tx(USDC, 1_000_123)})
    w = SettlementWatcher(None, db=x402_db)
    settled, _ = await w._scan_solana()
    assert settled == 1
    row = await x402_db.fetch_one(
        "SELECT transaction_hash FROM x402_payment_requests WHERE id=?", (rid,))
    assert row["transaction_hash"] == "exact"


@pytest.mark.asyncio
async def test_spam_on_the_reference_does_not_hide_the_payment(x402_db, monkeypatch):
    rid = await _mint(x402_db, asset_address=USDC, amount_usd=1.0, amount_raw=1_000_000)
    ref = ss.reference_for_invoice(rid)
    spam = [f"spam{i}" for i in range(60)]
    txs = {s: _tx(USDC, 1) for s in spam}
    txs["real"] = _tx(USDC, 1_000_000)
    calls = _wire(monkeypatch, {ref: spam + ["real"]}, txs)
    w = SettlementWatcher(None, db=x402_db)
    for _ in range(4):
        await w._scan_solana()
        if await _status(x402_db, rid) == "completed":
            break
    assert await _status(x402_db, rid) == "completed"
    # Capped per tick, and a judged signature is never fetched twice.
    assert calls["getTransaction"] == 61


def test_scan_reference_pages_back_past_the_newest_page():
    sigs = [f"s{i}" for i in range(2500)]

    def _rpc(method, params):
        _, opts = params
        start = sigs.index(opts["before"]) + 1 if opts.get("before") else 0
        return [{"signature": s, "err": None} for s in sigs[start:start + opts["limit"]]]

    assert len(list(ss.scan_reference("ref", rpc=_rpc))) == 2500
