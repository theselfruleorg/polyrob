"""046 Phase 0: the public invoice challenge names its RAIL.

⚠️ The defect: `_invoice_asset_cfg` asked `fastapi_x402.networks` for "the
default asset of this network". That library knows one asset per network it
knows, and knows nothing at all about Robinhood Chain — so an invoice in an
operator-pinned asset had no expressible challenge, and serving the facilitator
shape anyway would invite a payer to sign an EIP-3009 authorization that no
facilitator will ever verify. They would reasonably believe they had paid.
"""
import pytest

from api.x402_endpoints import _challenge_for_invoice


def _row(**kw):
    base = dict(id="inv_1", amount_usd=1.25, chain="base", purpose="p",
                recipient="0x" + "11" * 20, asset_id="usdc-base",
                asset_address=None, asset_decimals=None, amount_raw=None)
    base.update(kw)
    return base


@pytest.fixture()
def rob_asset(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    from core.payments.assets import AssetStore, PaymentAsset, store_path
    AssetStore(store_path(str(tmp_path))).upsert(PaymentAsset(
        asset_id="rob", chain="robinhood", address="0x" + "bb" * 20,
        decimals=18, symbol="ROB", rail="onchain_scan", source="operator"))


def test_an_onchain_scan_asset_serves_a_direct_transfer_challenge(rob_asset):
    body, status = _challenge_for_invoice(_row(
        chain="robinhood", asset_id="rob", asset_address="0x" + "bb" * 20,
        asset_decimals=18, amount_raw=str(7 * 10 ** 18), amount_usd=0.5))
    assert status == 402
    assert body["rail"] == "onchain_scan"
    assert body["pay_to"] == "0x" + "11" * 20
    assert body["token"] == "0x" + "bb" * 20
    assert body["amount_raw"] == str(7 * 10 ** 18)
    assert body["chain_id"] == 4663
    assert body["symbol"] == "ROB"
    assert body["uri"].startswith("ethereum:")


def test_an_onchain_scan_challenge_carries_NO_accepts_block(rob_asset):
    """⚠️ The whole point. An `accepts` block asks the payer to sign a payment
    authorization; for an asset no facilitator knows, that signature is never
    verified by anyone and the payer has not paid."""
    body, _ = _challenge_for_invoice(_row(
        chain="robinhood", asset_id="rob", asset_address="0x" + "bb" * 20,
        asset_decimals=18, amount_raw=str(10 ** 18)))
    assert "accepts" not in body


def test_the_direct_transfer_challenge_says_not_to_sign_anything(rob_asset):
    body, _ = _challenge_for_invoice(_row(
        chain="robinhood", asset_id="rob", asset_address="0x" + "bb" * 20,
        asset_decimals=18, amount_raw=str(10 ** 18)))
    assert "do not sign" in body["note"].lower()


def test_an_unconfigured_asset_is_unpayable_and_says_so(tmp_path, monkeypatch):
    """⚠️ Never a 402 the payer cannot act on. An invoice whose asset this
    instance no longer knows is UNPAYABLE, with the reason named."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    body, status = _challenge_for_invoice(_row(asset_id="vanished"))
    assert status == 409
    assert body["status"] == "unpayable"
    assert "vanished" in body["reason"]


def test_a_legacy_row_with_no_asset_id_reads_as_usdc_base(tmp_path, monkeypatch):
    """A pre-046 row carries no asset_id. It must keep serving the facilitator
    challenge it always did, or every outstanding invoice becomes unpayable."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    body, status = _challenge_for_invoice(_row(asset_id=None))
    assert status == 402
    assert "accepts" in body
    assert body["accepts"][0]["scheme"] == "exact"


def test_the_facilitator_challenge_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    body, status = _challenge_for_invoice(_row())
    assert status == 402
    accepts = body["accepts"][0]
    assert accepts["network"] == "base"
    assert accepts["payTo"] == "0x" + "11" * 20
    assert accepts["resource"] == "/api/x402/requests/inv_1/pay"
    assert accepts["maxTimeoutSeconds"] == 300
    assert accepts["asset"]
    assert body["amount_usd"] == 1.25


def test_pay_is_refused_for_an_onchain_scan_asset(rob_asset):
    """The facilitator cannot settle it, so accepting an X-PAYMENT header would
    take a signature and do nothing with it."""
    from api.x402_endpoints import _facilitator_refusal
    refusal = _facilitator_refusal(_row(
        chain="robinhood", asset_id="rob", asset_address="0x" + "bb" * 20,
        asset_decimals=18, amount_raw=str(10 ** 18)))
    assert refusal is not None
    assert "DIRECT TRANSFER" in refusal
    assert "ROB" in refusal


def test_pay_is_allowed_for_a_facilitator_asset(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    from api.x402_endpoints import _facilitator_refusal
    assert _facilitator_refusal(_row()) is None


def test_the_middleware_refuses_rather_than_defaulting_to_six_decimals():
    """⚠️ A silent 6-decimal fallback sizes an 18-decimal charge a trillion
    times too small."""
    from modules.x402.middleware import resolve_charge_asset
    with pytest.raises(ValueError, match="asset"):
        resolve_charge_asset("no-such-network")
