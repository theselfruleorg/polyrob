"""Amount-collision jitter is scoped to ONE asset (046 Phase 0)."""
import pytest

from modules.x402 import invoicing

TREASURY = "0x" + "11" * 20


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", TREASURY)
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")
    monkeypatch.setenv("X402_SETTLE_ONCHAIN_DETECT", "true")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    for var in ("X402_INVOICE_DAILY_MAX", "AUTONOMY_HALT"):
        monkeypatch.delenv(var, raising=False)
    from core.payments.assets import AssetStore, PaymentAsset, store_path
    AssetStore(store_path(str(tmp_path / "home"))).upsert(PaymentAsset(
        asset_id="rob", chain="robinhood", address="0x" + "bb" * 20,
        decimals=18, symbol="ROB", rail="onchain_scan", source="operator"))


@pytest.mark.asyncio
async def test_two_different_assets_at_the_same_usd_price_do_not_collide(x402_db):
    """⚠️ A ROB invoice at $1 and a USDC invoice at $1 are already unambiguous
    on-chain — they arrive from different contracts. Jittering one because of
    the other moves a quoted price for no reason."""
    a = await invoicing.create_payment_request(
        user_id="rob", session_id="s", amount_usd=1.0, purpose="a", db=x402_db)
    b = await invoicing.create_payment_request(
        user_id="rob", session_id="s", amount_usd=1.0, purpose="b",
        chain="robinhood", asset_id="rob", amount_raw=10 ** 18, db=x402_db)
    assert a["amount_usd"] == 1.0
    assert b["amount_usd"] == 1.0, "jitter moved a price across assets"


@pytest.mark.asyncio
async def test_two_invoices_in_the_SAME_asset_still_jitter_apart(x402_db):
    a = await invoicing.create_payment_request(
        user_id="rob", session_id="s", amount_usd=1.0, purpose="a", db=x402_db)
    b = await invoicing.create_payment_request(
        user_id="rob", session_id="s", amount_usd=1.0, purpose="b", db=x402_db)
    assert a["amount_usd"] != b["amount_usd"]
    assert a["amount_raw"] != b["amount_raw"]


@pytest.mark.asyncio
async def test_the_unique_index_carries_the_asset(x402_db):
    """The CROSS-process backstop must match the in-process dedupe's key, or
    two workers disagree about what a collision is."""
    await invoicing.create_payment_request(
        user_id="rob", session_id="s", amount_usd=2.0, purpose="a", db=x402_db)
    rows = await x402_db.fetch_all(
        "SELECT name, sql FROM sqlite_master WHERE type='index' "
        "AND name LIKE 'idx_x402_requests_pending_amount%'")
    assert rows, "the pending-amount unique index was never created"
    sql = " ".join(str(r["sql"] or "") for r in rows)
    assert "asset_address" in sql and "amount_raw" in sql


@pytest.mark.asyncio
async def test_a_different_kind_still_gets_its_OWN_amount(x402_db):
    """⚠️ Inverted in 046 phase 2, and the old expectation was the bug.

    The first cut reasoned that a room action and an agent invoice are
    "different producers, so neither should perturb the other's quote". But the
    settlement matcher is KIND-BLIND — `match_pending_invoice` takes every
    payable kind and matches an exact raw amount, oldest-first — so two rows at
    the same price in the same asset were genuinely ambiguous on-chain, and one
    payment settled the older one whatever it had bought.

    Uniqueness must be asserted over exactly the set the matcher searches.
    """
    a = await invoicing.create_payment_request(
        user_id="rob", session_id="s", amount_usd=3.0, purpose="a", db=x402_db)
    b = await invoicing.create_payment_request(
        user_id="rob", session_id="s", amount_usd=3.0, purpose="b",
        kind="room_action", extra_metadata={"room_action": {"offer_id": "o"}},
        db=x402_db)
    assert a["amount_raw"] != b["amount_raw"]


@pytest.mark.asyncio
async def test_two_pinned_raw_room_offers_never_share_an_amount(x402_db):
    """⚠️ THE collision that misapplied a paid action.

    A room offer PINS `amount_raw`, and `_insert` writes that integer verbatim —
    so the USD jitter moved only the displayed dollars while the payable integer
    stayed identical, and `_dedupe_amount_for_treasury` probed a raw value that
    would never be inserted (reporting "no collision" on its first try, every
    time). Two members buying the same price in one room therefore shared an
    amount, and the first payment applied the OTHER one's action against a
    different person.
    """
    raws = []
    for i in range(3):
        inv = await invoicing.create_payment_request(
            user_id="rob", session_id="s", amount_usd=0.5,
            purpose=f"mute {i}", amount_raw=500_000, kind="room_action",
            extra_metadata={"room_action": {"offer_id": f"o{i}"}}, db=x402_db)
        raws.append(int(inv["amount_raw"]))
    assert len(set(raws)) == 3, raws
    assert min(raws) == 500_000, "the FIRST offer keeps the price it was quoted"
