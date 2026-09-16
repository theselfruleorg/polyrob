"""One eth_getLogs per (chain, asset), each with its own checkpoint (046)."""
import json
import uuid

import pytest

from modules.x402 import settlement_scan

TREASURY = "0x" + "11" * 20


def test_the_default_asset_keeps_the_bare_treasury_checkpoint_key():
    """⚠️ Continuity. `settlement_scan` is keyed on a single `treasury` column,
    so changing the key for the LIVE USDC rail re-seeds the checkpoint near head
    on the next tick — which scans nothing and silently skips every transfer in
    the gap."""
    from core.payments.assets import resolve
    usdc = resolve("usdc-base")
    assert settlement_scan.scan_key(TREASURY, "base", usdc.address) == TREASURY


def test_a_second_asset_gets_its_own_checkpoint_key():
    key = settlement_scan.scan_key(TREASURY, "robinhood", "0x" + "bb" * 20)
    assert key != TREASURY
    assert "robinhood" in key and "bb" in key


def test_the_key_is_case_insensitive_on_the_address():
    a = settlement_scan.scan_key(TREASURY, "robinhood", "0x" + "BB" * 20)
    b = settlement_scan.scan_key(TREASURY, "robinhood", "0x" + "bb" * 20)
    assert a == b


def test_the_scan_target_is_asset_free_and_comes_from_the_chain_registry():
    target = settlement_scan._resolve_scan_target("base")
    assert target is not None
    rpc_url, chain_id = target
    assert rpc_url
    assert chain_id == 8453


def test_robinhood_is_now_a_scannable_chain():
    """⚠️ Before 046 this returned None: detection on any chain but base was a
    one-warning no-op, forever."""
    target = settlement_scan._resolve_scan_target("robinhood")
    assert target is not None
    assert target[1] == 4663


def test_base_sepolia_is_still_scannable_through_its_own_branch():
    """The money chain registry has no testnet row, so the x402 module keeps
    owning this one (the same constants it always used)."""
    target = settlement_scan._resolve_scan_target("base-sepolia")
    assert target is not None
    assert target[1] == 84532


def test_an_unknown_chain_is_still_unscannable():
    assert settlement_scan._resolve_scan_target("atlantis") is None


def test_a_non_evm_chain_is_unscannable_by_the_evm_pass():
    """Solana settles by REFERENCE in its own pass; answering here would run an
    eth_getLogs against a chain that has no such method."""
    assert settlement_scan._resolve_scan_target("solana") is None


async def _mint(db, asset_id, address, decimals, chain, status="pending"):
    rid = f"inv_{uuid.uuid4().hex[:12]}"
    await db.execute(
        """INSERT INTO x402_payment_requests(id,amount,amount_usd,asset,chain,
               recipient,nonce,deadline,status,metadata,
               asset_id,asset_address,asset_decimals,amount_raw,
               created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))""",
        (rid, "1", 1.0, "x", chain, TREASURY, rid, 9_999_999_999, status,
         json.dumps({"kind": "agent_invoice"}), asset_id, address, decimals, "1"))
    return rid


@pytest.mark.asyncio
async def test_pending_scan_groups_lists_one_row_per_distinct_asset(x402_db):
    await _mint(x402_db, "usdc-base", "0x" + "aa" * 20, 6, "base")
    await _mint(x402_db, "usdc-base", "0x" + "aa" * 20, 6, "base")
    await _mint(x402_db, "rob", "0x" + "bb" * 20, 18, "robinhood")

    groups = await settlement_scan.pending_scan_groups(TREASURY, db=x402_db)
    assert len(groups) == 2
    assert ("robinhood", "0x" + "bb" * 20, 18, "rob") in groups


@pytest.mark.asyncio
async def test_a_settled_invoice_contributes_no_scan_group(x402_db):
    """Scanning is driven by what is actually OWED. An asset nobody is waiting
    on costs no eth_getLogs call."""
    await _mint(x402_db, "rob", "0x" + "bb" * 20, 18, "robinhood",
                status="completed")
    assert await settlement_scan.pending_scan_groups(TREASURY, db=x402_db) == []


@pytest.mark.asyncio
async def test_a_legacy_null_asset_row_still_contributes_the_default_group(x402_db):
    """⚠️ A pre-046 pending row must keep being scanned, or it is orphaned."""
    rid = f"inv_{uuid.uuid4().hex[:12]}"
    await x402_db.execute(
        """INSERT INTO x402_payment_requests(id,amount,amount_usd,asset,chain,
               recipient,nonce,deadline,status,metadata,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))""",
        (rid, "1", 1.0, "usdc", "base", TREASURY, rid, 9_999_999_999, "pending",
         json.dumps({"kind": "agent_invoice"})))
    groups = await settlement_scan.pending_scan_groups(TREASURY, db=x402_db)
    from core.payments.assets import resolve
    usdc = resolve("usdc-base")
    assert groups == [("base", usdc.address.lower(), 6, "usdc-base")]


@pytest.mark.asyncio
async def test_a_non_evm_pending_row_is_not_an_evm_scan_group(x402_db):
    """Its settlement is the reference pass, not a treasury sweep."""
    await _mint(x402_db, "usdc-solana", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                6, "solana")
    assert await settlement_scan.pending_scan_groups(TREASURY, db=x402_db) == []
