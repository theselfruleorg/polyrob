"""DEFI-16: a test-network invoice is never minted on a production x402 rail,
never counts as income, and every listing names its chain.

Before: a correspondent could steer ``x402_request(chain="base-sepolia")``, a
devnet Solana invoice was stored as chain ``solana`` and counted as income,
and ``x402_invoices`` showed "[completed]" with no chain at all.
"""
import asyncio

import pytest

from modules.database.connection import DatabaseConnection
from modules.database.user_profiles import UserProfiles
from modules.database.x402_tables import X402Tables
from modules.x402 import invoicing
from modules.x402.income_chains import income_predicate, is_income_chain


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0x" + "ab" * 20)
    for var in ("X402_DEFAULT_CHAIN", "X402_INVOICE_MAX_USD", "X402_INVOICE_DAILY_MAX",
                "X402_SETTLE_ONCHAIN_DETECT", "X402_INVOICE_AMOUNT_JITTER",
                "PAYMENT_DEFAULT_ASSET"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(invoicing, "_svm_treasury",
                        lambda: "BrsPwATRZcb2PWsEZba9Bh1mwcxU6M7R64nPgRneCpmL")


async def _db(tmp_path):
    db = DatabaseConnection(tmp_path / "x402.db")
    await db.connect()
    await UserProfiles(db).create_table()
    await X402Tables(db).create_tables()
    return db


async def _mint(db, **kw):
    return await invoicing.create_payment_request(
        user_id="rob", session_id="s1", amount_usd=1.0, purpose="work", db=db, **kw)


def test_devnet_asset_on_the_solana_name_is_not_income():
    assert is_income_chain("solana", "usdc-solana")
    assert not is_income_chain("solana", "usdc-solana-devnet")
    assert not is_income_chain("base-sepolia")


@pytest.mark.asyncio
async def test_a_steered_testnet_chain_is_refused_on_a_production_rail(tmp_path):
    db = await _db(tmp_path)
    try:
        with pytest.raises(ValueError, match="test network"):
            await _mint(db, chain="base-sepolia")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_a_devnet_solana_wallet_invoice_is_refused_on_a_production_rail(
        tmp_path, monkeypatch):
    monkeypatch.setattr("core.payments.assets.solana_default_asset_id",
                        lambda network=None: "usdc-solana-devnet")
    db = await _db(tmp_path)
    try:
        with pytest.raises(ValueError, match="test network"):
            await _mint(db, chain="solana")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_a_testnet_rail_mints_a_marked_invoice_that_is_not_income(
        tmp_path, monkeypatch):
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base-sepolia")
    db = await _db(tmp_path)
    try:
        inv = await _mint(db)
        rows = await invoicing.list_payment_requests(user_id="rob", db=db)
        assert rows[0]["chain"] == "base-sepolia"
        meta = await db.fetch_one(
            "SELECT json_extract(metadata, '$.testnet') AS t FROM x402_payment_requests "
            "WHERE id = ?", (inv["request_id"],))
        assert meta["t"] == 1
        await db.execute("UPDATE x402_payment_requests SET status='completed'")
        frag, args = income_predicate()
        got = await db.fetch_one(
            f"SELECT COUNT(*) AS n FROM x402_payment_requests WHERE {frag}", args)
        assert got["n"] == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_income_predicate_drops_a_devnet_row_stored_as_solana(tmp_path):
    db = await _db(tmp_path)
    try:
        for rid, asset in (("a", "usdc-solana"), ("b", "usdc-solana-devnet")):
            await db.execute(
                "INSERT INTO x402_payment_requests (id,amount,amount_usd,asset,chain,"
                "recipient,nonce,deadline,status,asset_id,metadata) VALUES "
                "(?,'1',1.0,'usdc','solana','r',?,1,'completed',?,'{}')", (rid, rid, asset))
        frag, args = income_predicate()
        rows = await db.fetch_all(
            f"SELECT id FROM x402_payment_requests WHERE {frag}", args)
        assert [r["id"] for r in rows] == ["a"]
    finally:
        await db.close()


def test_listing_names_the_chain_and_marks_a_test_network(monkeypatch):
    from tools.x402.invoice_tool import X402InvoiceTool, InvoiceListParams

    async def fake_list(**kw):
        return [
            {"request_id": "inv_t", "amount_usd": 1.0, "status": "completed",
             "purpose": "p", "chain": "base-sepolia", "asset_id": "usdc-base-sepolia",
             "created_at": "x"},
            {"request_id": "inv_m", "amount_usd": 1.0, "status": "completed",
             "purpose": "p", "chain": "base", "asset_id": "usdc-base", "created_at": "x"},
        ]

    class _Ctx:
        user_id = "rob"
        session_id = "s"

    monkeypatch.setattr(invoicing, "list_payment_requests", fake_list)
    res = asyncio.run(X402InvoiceTool().x402_invoices(
        InvoiceListParams(), execution_context=_Ctx()))
    text = res.extracted_content
    assert "[completed · base-sepolia TEST NETWORK, not income]" in text
    assert "[completed · base]" in text
