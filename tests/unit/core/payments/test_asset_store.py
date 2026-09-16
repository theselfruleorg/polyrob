"""Operator asset rows live in their own sidecar DB and win over a builtin."""
import pytest

from core.payments import assets
from core.payments.assets import AssetStore, PaymentAsset


@pytest.fixture()
def store(tmp_path):
    return AssetStore(str(tmp_path / "payment_assets.db"))


def _row(**kw):
    base = dict(asset_id="rob", chain="robinhood",
                address="0x" + "ab" * 20, decimals=18, symbol="ROB",
                rail="onchain_scan", min_amount_raw=10 ** 18,
                liquidity_floor_usd=2500.0, verified_at=1.0, source="operator")
    base.update(kw)
    return PaymentAsset(**base)


def test_an_upserted_row_reads_back_identical(store):
    store.upsert(_row())
    assert store.get("rob") == _row()


def test_amount_floor_survives_18_decimals_as_text(store):
    """⚠️ 10**18 exceeds nothing here, but a large-supply token's floor can
    exceed SQLite's signed 64-bit INTEGER range — the column is TEXT."""
    huge = 10 ** 30
    store.upsert(_row(min_amount_raw=huge))
    assert store.get("rob").min_amount_raw == huge


def test_upsert_is_idempotent_and_updates_in_place(store):
    store.upsert(_row())
    store.upsert(_row(symbol="ROB2", verified_at=2.0))
    rows = store.list_all()
    assert len(rows) == 1
    assert rows[0].symbol == "ROB2"


def test_an_absent_row_is_none_never_a_default(store):
    assert store.get("rob") is None
    assert store.get("") is None


def test_the_address_is_stored_lowercase_so_a_scan_lookup_always_matches(store):
    store.upsert(_row(address="0x" + "AB" * 20))
    assert store.get("rob").address == "0x" + "ab" * 20


def test_resolve_prefers_an_operator_row_over_a_builtin(tmp_path):
    s = AssetStore(assets.store_path(str(tmp_path)))
    s.upsert(_row(asset_id="usdc-base", chain="base", decimals=6, symbol="USDC"))
    got = assets.resolve("usdc-base", data_home=str(tmp_path))
    assert got.source == "operator"
    assert got.min_amount_raw == 10 ** 18


def test_resolve_falls_back_to_the_builtin_when_no_operator_row_exists(tmp_path):
    got = assets.resolve("usdc-base", data_home=str(tmp_path))
    assert got is not None
    assert got.source == "builtin"


def test_resolve_of_an_unknown_id_is_none(tmp_path):
    assert assets.resolve("nope", data_home=str(tmp_path)) is None
    assert assets.resolve(None, data_home=str(tmp_path)) is None


def test_all_assets_merges_builtins_and_operator_rows(tmp_path):
    AssetStore(assets.store_path(str(tmp_path))).upsert(_row())
    ids = {a.asset_id for a in assets.all_assets(data_home=str(tmp_path))}
    assert {"usdc-base", "usdc-base-sepolia", "rob"} <= ids


def test_vocabulary_names_every_resolvable_id_for_a_refusal(tmp_path):
    AssetStore(assets.store_path(str(tmp_path))).upsert(_row())
    vocab = assets.vocabulary(data_home=str(tmp_path))
    assert "rob" in vocab and "usdc-base" in vocab


def test_an_unreadable_store_degrades_to_builtins_and_never_raises(tmp_path):
    """⚠️ A corrupt sidecar must degrade to the pre-046 behaviour, not to an
    outage of the whole receive rail."""
    (tmp_path / "payment_assets.db").write_bytes(b"not a database at all")
    got = assets.resolve("usdc-base", data_home=str(tmp_path))
    assert got is not None and got.source == "builtin"
    assert {a.asset_id for a in assets.all_assets(data_home=str(tmp_path))} == set(
        assets.BUILTIN_ASSETS)


def test_the_sidecar_db_is_in_the_backup_manifest():
    from core.db_manifest import SIDECAR_DB_NAMES
    assert "payment_assets.db" in SIDECAR_DB_NAMES
