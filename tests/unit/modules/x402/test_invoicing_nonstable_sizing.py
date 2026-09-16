"""A USD price on a NON-STABLE asset is sized by the quoter, never by par.

⚠️ The defect this pins. `create_payment_request` fell through to
``atomic_amount(amount_usd, decimals)`` whenever the caller passed no
``amount_raw`` — which is dollar-pegged math. The only agent-facing caller
(`x402_request`, `tools/x402/invoice_tool.py`) takes ``amount_usd`` and NEVER
passes ``amount_raw``, so naming a non-stable ``asset_id`` minted an invoice
for `usd * 10**decimals` RAW UNITS: a $5 invoice in a token worth $0.00006
asked for 5 tokens (~$0.0003) instead of ~78,567, wrong by four orders of
magnitude and silent.

046 §4.4 already owned the policy (`core/payments/quote.py::size_amount_raw`)
and the read (`tools/defi/payment_quote.py::PaymentQuoter`). Neither was
reachable from the mint path. These tests pin the join.
"""
import pytest

from core.payments.quote import PriceQuote
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
def pnl_asset(tmp_path):
    """An operator-pinned 18-decimal memecoin on Robinhood Chain."""
    from core.payments.assets import AssetStore, PaymentAsset, store_path
    home = str(tmp_path / "home")
    AssetStore(store_path(home)).upsert(PaymentAsset(
        asset_id="pnl", chain="robinhood", address="0x" + "bb" * 20,
        decimals=18, symbol="PNL", rail="onchain_scan", source="operator"))
    return home


class _Quoter:
    """The `payment_quoter` container-service contract: ``.quote(asset)``."""

    def __init__(self, usd_per_token=0.00006364, liquidity=22995.9,
                 verdict="SURVIVOR", raises=False):
        self._price, self._liq = usd_per_token, liquidity
        self._verdict, self._raises = verdict, raises
        self.calls = 0

    def quote(self, asset):
        self.calls += 1
        if self._raises:
            raise RuntimeError("indexer down")
        import time
        return PriceQuote(asset_id=asset.asset_id, usd_per_token=self._price,
                          liquidity_usd=self._liq, verdict=self._verdict,
                          source="test", ts=time.time())


# --- the defect ------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_non_stable_asset_is_never_sized_at_par(x402_db, pnl_asset):
    """⚠️ No quoter must REFUSE, not mint `usd * 10**decimals` raw units."""
    with pytest.raises(ValueError) as e:
        await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=5.0, purpose="p",
            chain="robinhood", asset_id="pnl", quoter=None, db=x402_db)
    msg = str(e.value).lower()
    assert "price" in msg and "pnl" in msg


@pytest.mark.asyncio
async def test_a_quoted_non_stable_asset_is_sized_from_the_price(x402_db, pnl_asset):
    q = _Quoter(usd_per_token=0.00006364)
    inv = await invoicing.create_payment_request(
        user_id="rob", session_id="s1", amount_usd=5.0, purpose="p",
        chain="robinhood", asset_id="pnl", quoter=q, db=x402_db)
    assert q.calls == 1
    # 5 / 0.00006364 = 78566.94... tokens, at 18 decimals.
    assert inv["amount_raw"] == pytest.approx(78566.94e18, rel=1e-6)
    assert inv["amount_raw"] > 10 ** 22        # never the par figure 5*10**18


@pytest.mark.asyncio
async def test_a_wash_pool_is_refused_rather_than_priced(x402_db, pnl_asset):
    q = _Quoter(verdict="WASH")
    with pytest.raises(ValueError) as e:
        await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=5.0, purpose="p",
            chain="robinhood", asset_id="pnl", quoter=q, db=x402_db)
    assert "WASH" in str(e.value)


@pytest.mark.asyncio
async def test_a_raising_quoter_refuses_and_never_falls_back_to_par(
        x402_db, pnl_asset):
    """An indexer outage costs an invoice; it must not give one away."""
    with pytest.raises(ValueError):
        await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=5.0, purpose="p",
            chain="robinhood", asset_id="pnl", quoter=_Quoter(raises=True),
            db=x402_db)


@pytest.mark.asyncio
async def test_the_asset_min_amount_floor_still_applies_after_sizing(
        x402_db, tmp_path):
    from core.payments.assets import AssetStore, PaymentAsset, store_path
    AssetStore(store_path(str(tmp_path / "home"))).upsert(PaymentAsset(
        asset_id="pnl", chain="robinhood", address="0x" + "bb" * 20,
        decimals=18, symbol="PNL", rail="onchain_scan",
        min_amount_raw=10 ** 30, source="operator"))
    with pytest.raises(ValueError) as e:
        await invoicing.create_payment_request(
            user_id="rob", session_id="s1", amount_usd=5.0, purpose="p",
            chain="robinhood", asset_id="pnl", quoter=_Quoter(), db=x402_db)
    assert "floor" in str(e.value)


# --- the unchanged paths ---------------------------------------------------

@pytest.mark.asyncio
async def test_a_dollar_pegged_asset_never_consults_the_quoter(x402_db):
    q = _Quoter()
    inv = await invoicing.create_payment_request(
        user_id="rob", session_id="s1", amount_usd=1.25, purpose="p",
        quoter=q, db=x402_db)
    assert q.calls == 0
    assert inv["amount_raw"] == 1_250_000


@pytest.mark.asyncio
async def test_an_explicit_amount_raw_still_wins(x402_db, pnl_asset):
    q = _Quoter()
    inv = await invoicing.create_payment_request(
        user_id="rob", session_id="s1", amount_usd=5.0, purpose="p",
        chain="robinhood", asset_id="pnl", amount_raw=7 * 10 ** 22,
        quoter=q, db=x402_db)
    assert q.calls == 0
    assert inv["amount_raw"] == 7 * 10 ** 22
