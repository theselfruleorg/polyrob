"""A Solana mint is base58 — case is part of the value; an EVM address is not."""
from core.payments.assets import AssetStore, PaymentAsset

MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _row(**kw):
    base = dict(asset_id="bonk", chain="solana", address=MINT, decimals=5,
                symbol="BONK", rail="svm_reference", min_amount_raw=1,
                liquidity_floor_usd=0.0, verified_at=1.0, source="operator")
    base.update(kw)
    return PaymentAsset(**base)


def test_a_solana_mint_keeps_its_case(tmp_path):
    store = AssetStore(str(tmp_path / "a.db"))
    store.upsert(_row())
    assert store.get("bonk").address == MINT


def test_an_evm_address_is_still_lowercased(tmp_path):
    store = AssetStore(str(tmp_path / "a.db"))
    store.upsert(_row(asset_id="rob", chain="base", address="0x" + "AB" * 20,
                      rail="onchain_scan"))
    assert store.get("rob").address == "0x" + "ab" * 20
