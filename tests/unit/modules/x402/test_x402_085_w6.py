"""085 W6 F1/F2 and W8 x402in L3: getting paid cannot be griefed by reference
spam, the chain-id check is per (url, chain), and finality depth is per chain."""
import json
import time
import types
import uuid

import pytest

from modules.x402 import invoicing
from modules.x402 import solana_settlement as ss
from modules.x402.settlement_watcher import SettlementWatcher

TREASURY = "Treas1111111111111111111111111111111111111"
USDC = "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU"   # testnet mint
PRICE = 1_000_000


async def _mint(db, *, deadline):
    rid = f"inv_{uuid.uuid4().hex[:12]}"
    meta = {"kind": "agent_invoice", "chain_family": "svm", "tenant_id": "u1",
            "wake_delivered": False}
    await db.execute(
        """INSERT INTO x402_payment_requests(id,amount,amount_usd,asset,chain,
               recipient,nonce,deadline,status,metadata,
               asset_id,asset_address,asset_decimals,amount_raw,
               created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))""",
        (rid, "1.0", 1.0, "usdc", "solana", TREASURY, rid, int(deadline), "pending",
         json.dumps(meta), "x", USDC, 6, str(PRICE)))
    return rid


def _tx(amount):
    return {"meta": {"err": None, "preTokenBalances": [],
                     "postTokenBalances": [{"owner": TREASURY, "mint": USDC,
                                            "uiTokenAmount": {"amount": str(amount)}}]}}


def _wire(monkeypatch, entries_by_ref, txs):
    """``entries_by_ref``: ref -> [(signature, blockTime)] NEWEST first, the
    order `getSignaturesForAddress` answers in."""
    import modules.x402.settlement_scan as scan
    from core.wallet import solana_onchain
    monkeypatch.setattr(scan, "solana_settle_enabled", lambda: True)
    wallet = types.SimpleNamespace(solana_address=TREASURY, network="testnet")
    monkeypatch.setattr("core.wallet.factory.get_agent_wallet", lambda: wallet)
    calls = {"getTransaction": 0}

    def _rpc(method, params):
        if method == "getSignaturesForAddress":
            ref, opts = params
            entries = entries_by_ref.get(ref, [])
            sigs = [e[0] for e in entries]
            start = sigs.index(opts["before"]) + 1 if opts.get("before") else 0
            return [{"signature": s, "err": None, "blockTime": bt}
                    for s, bt in entries[start:start + opts["limit"]]]
        if method == "getTransaction":
            calls["getTransaction"] += 1
            return txs.get(params[0])
        raise AssertionError(method)

    monkeypatch.setattr(solana_onchain, "_rpc", _rpc)
    return calls


async def _status(db, rid):
    r = await db.fetch_one("SELECT status FROM x402_payment_requests WHERE id=?", (rid,))
    return r["status"]


# -- F1 --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_750_spam_signatures_do_not_expire_a_paid_invoice(x402_db, monkeypatch):
    """The payment lands, then 750 cheap transactions name the public reference
    (newest first, so in front of it). The deadline arrives: the pre-expiry
    pass is uncapped and still finds the payment; the invoice never expires."""
    now = time.time()
    deadline = now + 60                       # inside the final-pass margin
    rid = await _mint(x402_db, deadline=deadline)
    ref = ss.reference_for_invoice(rid)
    # Spam sent BEFORE the payment too: oldest-first alone cannot reach it
    # within the per-tick cap, only the uncapped pass can.
    early_spam = [(f"pre{i}", int(now) + 1) for i in range(750)]
    late_spam = [(f"post{i}", int(now) + 3) for i in range(750)]
    entries = late_spam + [("real", int(now) + 2)] + early_spam
    txs = {s: _tx(1) for s, _ in early_spam + late_spam}
    txs["real"] = _tx(PRICE)
    _wire(monkeypatch, {ref: entries}, txs)
    w = SettlementWatcher(None, db=x402_db)
    await w._scan_solana()
    await invoicing.expire_stale_requests(db=x402_db, now=deadline + 10)
    assert await _status(x402_db, rid) == "completed"


@pytest.mark.asyncio
async def test_a_solana_invoice_without_its_final_pass_is_held_not_expired(
        x402_db, monkeypatch):
    import modules.x402.settlement_scan as scan
    monkeypatch.setattr(scan, "solana_settle_enabled", lambda: True)
    deadline = time.time() - 30
    rid = await _mint(x402_db, deadline=deadline)
    await invoicing.expire_stale_requests(db=x402_db, now=deadline + 10)
    assert await _status(x402_db, rid) == "pending", "no final pass ran yet"
    # A dead RPC cannot hold it forever.
    await invoicing.expire_stale_requests(
        db=x402_db, now=deadline + invoicing._SVM_EXPIRY_HOLD_SEC + 10)
    assert await _status(x402_db, rid) == "expired"


@pytest.mark.asyncio
async def test_a_complete_final_pass_with_no_payment_lets_it_expire(
        x402_db, monkeypatch):
    now = time.time()
    rid = await _mint(x402_db, deadline=now - 5)
    ref = ss.reference_for_invoice(rid)
    _wire(monkeypatch, {ref: [("spam", int(now) - 30)]}, {"spam": _tx(1)})
    await SettlementWatcher(None, db=x402_db)._scan_solana()
    await invoicing.expire_stale_requests(db=x402_db, now=now + 10)
    assert await _status(x402_db, rid) == "expired"


@pytest.mark.asyncio
async def test_a_final_pass_before_the_deadline_does_not_release_expiry(
        x402_db, monkeypatch):
    """A pass that ends before the deadline cannot speak for a payment that
    lands in the last minutes; only a post-deadline pass stamps the row."""
    now = time.time()
    rid = await _mint(x402_db, deadline=now + 30)
    ref = ss.reference_for_invoice(rid)
    _wire(monkeypatch, {ref: [("spam", int(now) + 1)]}, {"spam": _tx(1)})
    await SettlementWatcher(None, db=x402_db)._scan_solana()
    await invoicing.expire_stale_requests(db=x402_db, now=now + 40)
    assert await _status(x402_db, rid) == "pending"


@pytest.mark.asyncio
async def test_an_unreadable_signature_keeps_the_final_pass_incomplete(
        x402_db, monkeypatch):
    """getTransaction answering null for one signature means it was not
    judged — it could be the payment — so the row is held, not expired."""
    now = time.time()
    rid = await _mint(x402_db, deadline=now - 5)
    ref = ss.reference_for_invoice(rid)
    _wire(monkeypatch, {ref: [("spam", int(now) - 30), ("gone", int(now) - 40)]},
          {"spam": _tx(1)})
    await SettlementWatcher(None, db=x402_db)._scan_solana()
    await invoicing.expire_stale_requests(db=x402_db, now=now + 10)
    assert await _status(x402_db, rid) == "pending"


@pytest.mark.asyncio
async def test_with_the_solana_pass_off_nothing_is_held(x402_db, monkeypatch):
    """The natural expiry is unchanged when nothing would ever stamp the row."""
    import modules.x402.settlement_scan as scan
    monkeypatch.setattr(scan, "solana_settle_enabled", lambda: False)
    deadline = time.time() - 30
    rid = await _mint(x402_db, deadline=deadline)
    await invoicing.expire_stale_requests(db=x402_db, now=deadline + 10)
    assert await _status(x402_db, rid) == "expired"


def test_signatures_older_than_the_invoice_are_skipped():
    entries = [("new", 200), ("mid", 150), ("old", 50), ("older", 40)]

    def _rpc(method, params):
        return [{"signature": s, "err": None, "blockTime": bt} for s, bt in entries]

    assert ss.scan_reference("ref", rpc=_rpc, since=100) == ["new", "mid"]
    assert ss.scan_reference("ref", rpc=_rpc, since=100, oldest_first=True) == [
        "mid", "new"]
    # Default behaviour is unchanged for other callers.
    assert ss.scan_reference("ref", rpc=_rpc) == ["new", "mid", "old", "older"]


@pytest.mark.asyncio
async def test_a_payment_after_expiry_reaches_the_owner_once(x402_db, monkeypatch):
    import modules.x402.settlement_scan as scan
    now = time.time()
    rid = await _mint(x402_db, deadline=now - 100)
    await x402_db.execute(
        "UPDATE x402_payment_requests SET status='expired' WHERE id=?", (rid,))
    ref = ss.reference_for_invoice(rid)
    _wire(monkeypatch, {ref: [("late", int(now))]}, {"late": _tx(PRICE)})
    told = []

    async def _notify(self, transfer, treasury):
        told.append(transfer)
    monkeypatch.setattr(scan.SettlementScanMixin, "_notify_unmatched", _notify)
    w = SettlementWatcher(None, db=x402_db)
    await w._scan_solana()
    await w._scan_solana()
    assert len(told) == 1
    assert told[0]["tx_hash"] == "late" and told[0]["request_id"] == rid
    assert await _status(x402_db, rid) == "expired", "a late payment never settles"


# -- F2 --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_chain_id_cache_is_keyed_on_url_and_chain():
    """The probe from the review: a Base RPC passes for base, then must FAIL
    for arbitrum (42161) instead of hitting the url-only cache."""
    w = SettlementWatcher(None, db=None)

    def call(method, params):
        assert method == "eth_chainId"
        return hex(8453)

    url = "https://base.example/rpc"
    assert await w._verify_scan_network(call, 8453, url, "base") is True
    assert await w._verify_scan_network(call, 42161, url, "arbitrum") is False
    assert await w._verify_scan_network(call, 8453, url, "base") is True


def test_the_settlement_rpc_pin_applies_only_to_the_default_chain(monkeypatch):
    from modules.x402.settlement_scan import _resolve_scan_target
    monkeypatch.setenv("X402_SETTLEMENT_RPC", "https://watcher.example/rpc")
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")
    assert _resolve_scan_target("base")[0] == "https://watcher.example/rpc"
    assert _resolve_scan_target("arbitrum")[0] != "https://watcher.example/rpc"


# -- x402in L3 -------------------------------------------------------------

def test_finality_depth_is_per_chain_unless_the_env_sets_it(monkeypatch):
    from modules.x402.settlement_scan import _scan_confirmations
    monkeypatch.delenv("X402_SETTLEMENT_CONFIRMATIONS", raising=False)
    assert _scan_confirmations("base") == 2
    assert _scan_confirmations("base-sepolia") == 2
    assert _scan_confirmations("ethereum") == 12
    assert _scan_confirmations("polygon") == 64
    assert _scan_confirmations("atlantis") == 2
    assert _scan_confirmations() == 2
    monkeypatch.setenv("X402_SETTLEMENT_CONFIRMATIONS", "5")
    assert _scan_confirmations("polygon") == 5
    assert _scan_confirmations("base") == 5
