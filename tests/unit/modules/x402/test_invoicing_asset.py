"""An invoice records WHICH asset it is payable in — and defaults to today's."""
import pytest

from modules.x402 import invoicing


@pytest.fixture(autouse=True)
def _treasury(monkeypatch, tmp_path):
    monkeypatch.setenv("X402_PAYMENT_RECIPIENT", "0x" + "11" * 20)
    monkeypatch.setenv("X402_DEFAULT_CHAIN", "base")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    for var in ("X402_INVOICE_MAX_USD", "X402_INVOICE_DAILY_MAX",
                "PAYMENT_DEFAULT_ASSET", "X402_SETTLE_ONCHAIN_DETECT",
                "AUTONOMY_HALT"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def rob_asset(tmp_path, monkeypatch):
    """An operator-pinned 18-decimal asset on Robinhood Chain."""
    from core.payments.assets import AssetStore, PaymentAsset, store_path
    home = str(tmp_path / "home")
    AssetStore(store_path(home)).upsert(PaymentAsset(
        asset_id="rob", chain="robinhood", address="0x" + "bb" * 20,
        decimals=18, symbol="ROB", rail="onchain_scan", source="operator"))
    return home


# --- resolve_invoice_asset -------------------------------------------------

def test_resolve_invoice_asset_defaults_to_usdc_base_for_a_base_invoice():
    row = invoicing.resolve_invoice_asset("base", None)
    assert row.asset_id == "usdc-base"
    assert row.decimals == 6


def test_resolve_invoice_asset_refuses_an_unknown_id_and_echoes_the_vocabulary():
    with pytest.raises(ValueError) as e:
        invoicing.resolve_invoice_asset("base", "no-such-asset")
    assert "no-such-asset" in str(e.value)
    assert "usdc-base" in str(e.value)


def test_resolve_invoice_asset_refuses_an_asset_from_another_chain(rob_asset):
    """⚠️ Paying an asset on the wrong chain strands the funds permanently."""
    with pytest.raises(ValueError) as e:
        invoicing.resolve_invoice_asset("robinhood", "usdc-base")
    msg = str(e.value)
    assert "usdc-base" in msg and "base" in msg and "robinhood" in msg


def test_resolve_invoice_asset_refuses_a_chain_with_no_payable_asset():
    with pytest.raises(ValueError) as e:
        invoicing.resolve_invoice_asset("polygon", None)
    assert "polygon" in str(e.value)


def test_the_instance_default_asset_env_is_honoured(rob_asset, monkeypatch):
    monkeypatch.setenv("PAYMENT_DEFAULT_ASSET", "rob")
    row = invoicing.resolve_invoice_asset("robinhood", None)
    assert row.asset_id == "rob"


# --- create_payment_request ------------------------------------------------

@pytest.mark.asyncio
async def test_a_default_invoice_still_writes_usdc_and_is_unchanged(x402_db):
    inv = await invoicing.create_payment_request(
        user_id="rob", session_id="s1", amount_usd=1.25, purpose="p", db=x402_db)
    assert inv["asset"] == "usdc"            # legacy column value, unchanged
    assert inv["asset_id"] == "usdc-base"
    assert inv["asset_decimals"] == 6
    assert inv["amount_raw"] == 1_250_000    # 1.25 * 10**6


@pytest.mark.asyncio
async def test_the_asset_columns_are_persisted(x402_db):
    inv = await invoicing.create_payment_request(
        user_id="rob", session_id="s1", amount_usd=1.25, purpose="p", db=x402_db)
    row = await x402_db.fetch_one(
        "SELECT * FROM x402_payment_requests WHERE id = ?", (inv["request_id"],))
    assert row["asset_id"] == "usdc-base"
    assert row["asset_decimals"] == 6
    assert str(row["amount_raw"]) == "1250000"


@pytest.mark.asyncio
async def test_an_explicit_amount_raw_is_stored_verbatim(x402_db, rob_asset):
    inv = await invoicing.create_payment_request(
        user_id="rob", session_id="s1", amount_usd=0.5, purpose="p",
        chain="robinhood", asset_id="rob", amount_raw=7 * 10 ** 18, db=x402_db)
    assert inv["amount_raw"] == 7 * 10 ** 18
    assert inv["asset_id"] == "rob"
    row = await x402_db.fetch_one(
        "SELECT * FROM x402_payment_requests WHERE id = ?", (inv["request_id"],))
    assert str(row["amount_raw"]) == str(7 * 10 ** 18)


@pytest.mark.asyncio
async def test_an_amount_that_rounds_to_zero_raw_units_is_refused(x402_db, rob_asset):
    """⚠️ Never mint an invoice nobody can pay, and never book a free action."""
    with pytest.raises(ValueError, match="raw units"):
        await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=0.0000001, purpose="p",
            db=x402_db)


@pytest.mark.asyncio
async def test_an_amount_below_the_assets_floor_is_refused(x402_db, tmp_path):
    from core.payments.assets import AssetStore, PaymentAsset, store_path
    AssetStore(store_path(str(tmp_path / "home"))).upsert(PaymentAsset(
        asset_id="usdc-base", chain="base", address="0x" + "aa" * 20,
        decimals=6, symbol="USDC", rail="facilitator",
        min_amount_raw=5_000_000, source="operator"))
    with pytest.raises(ValueError, match="floor"):
        await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=1.0, purpose="p",
            db=x402_db)


@pytest.mark.asyncio
async def test_a_custom_kind_is_stored_and_carries_its_extra_metadata(x402_db):
    import json
    inv = await invoicing.create_payment_request(
        user_id="rob", session_id="s1", amount_usd=1.0, purpose="p",
        kind="room_action", extra_metadata={"room_action": {"offer_id": "o1"}},
        db=x402_db)
    row = await x402_db.fetch_one(
        "SELECT metadata FROM x402_payment_requests WHERE id = ?",
        (inv["request_id"],))
    meta = row["metadata"] if isinstance(row["metadata"], dict) else json.loads(
        row["metadata"])
    assert meta["kind"] == "room_action"
    assert meta["room_action"] == {"offer_id": "o1"}


@pytest.mark.asyncio
async def test_a_custom_kind_does_not_count_against_the_agent_invoice_cap(
        x402_db, monkeypatch):
    """046 §4.7: a room-action invoice must not eat X402_INVOICE_DAILY_MAX,
    which exists to stop the AGENT invoicing from its own judgment.

    ⚠️ Asserted here rather than inferred from the query's kind filter."""
    monkeypatch.setenv("X402_INVOICE_DAILY_MAX", "1")
    await invoicing.create_payment_request(
        user_id="rob", session_id="s1", amount_usd=1.0, purpose="a", db=x402_db)
    with pytest.raises(ValueError, match="daily invoicing cap"):
        await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=2.0, purpose="b",
            db=x402_db)
    inv = await invoicing.create_payment_request(
        user_id="rob", session_id="s1", amount_usd=3.0, purpose="c",
        kind="room_action", extra_metadata={"room_action": {"offer_id": "o1"}},
        db=x402_db)
    assert inv["status"] == "pending"


@pytest.mark.asyncio
async def test_jitter_recomputes_amount_raw_from_the_FINAL_amount(
        x402_db, monkeypatch):
    """⚠️ The jitter path moves amount_usd AFTER sizing. If amount_raw still
    described the pre-jitter figure, the on-chain match would never fire."""
    monkeypatch.setenv("X402_SETTLE_ONCHAIN_DETECT", "true")
    a = await invoicing.create_payment_request(
        user_id="rob", session_id="s1", amount_usd=1.0, purpose="a", db=x402_db)
    b = await invoicing.create_payment_request(
        user_id="rob", session_id="s1", amount_usd=1.0, purpose="b", db=x402_db)
    assert a["amount_usd"] != b["amount_usd"], "jitter did not fire"
    for inv in (a, b):
        assert inv["amount_raw"] == round(inv["amount_usd"] * 10 ** 6)


@pytest.mark.asyncio
async def test_get_payment_request_returns_the_asset_columns(x402_db, rob_asset):
    """⚠️ Without these the PUBLIC challenge reads every invoice as usdc-base
    and serves a facilitator shape for an asset no facilitator can settle."""
    inv = await invoicing.create_payment_request(
        user_id="rob", session_id="s", amount_usd=0.5, purpose="p",
        chain="robinhood", asset_id="rob", amount_raw=7 * 10 ** 18, db=x402_db)
    got = await invoicing.get_payment_request(inv["request_id"], db=x402_db)
    assert got["asset_id"] == "rob"
    assert got["asset_address"] == "0x" + "bb" * 20
    assert got["asset_decimals"] == 18
    assert str(got["amount_raw"]) == str(7 * 10 ** 18)


@pytest.mark.asyncio
async def test_get_payment_request_carries_the_kind_and_room_action(x402_db):
    """The settlement side branches on these; a read that drops them makes the
    branch unreachable."""
    inv = await invoicing.create_payment_request(
        user_id="rob", session_id="s", amount_usd=1.0, purpose="p",
        kind="room_action", extra_metadata={"room_action": {"offer_id": "o1"}},
        db=x402_db)
    got = await invoicing.get_payment_request(inv["request_id"], db=x402_db)
    assert got["kind"] == "room_action"
    assert got["room_action"] == {"offer_id": "o1"}
