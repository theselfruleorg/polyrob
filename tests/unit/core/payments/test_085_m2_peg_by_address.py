"""085 M-2: the $1 peg is decided by the pinned (chain, address), never by the
token's self-reported ``symbol()``."""
import pytest

from core.payments.assets import BUILTIN_ASSETS, PaymentAsset
from core.payments.quote import QuoteRefused, is_stable, size_amount_raw
from core.wallet import chains

BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
FAKE = "0x" + "ab" * 20


def _asset(chain, address, symbol="USDC", decimals=6, **kw):
    return PaymentAsset(asset_id="x", chain=chain, address=address,
                        decimals=decimals, symbol=symbol, source="operator", **kw)


def test_a_token_named_usdc_at_an_unpinned_address_is_not_a_stable():
    assert not is_stable(_asset("base", FAKE, "USDC"))


def test_it_is_not_priced_at_par_and_needs_a_quote():
    with pytest.raises(QuoteRefused, match="no price"):
        size_amount_raw(10.0, _asset("base", FAKE, "USDC"), None)


def test_the_pinned_usdc_is_a_stable_whatever_its_symbol_says():
    assert is_stable(_asset("base", BASE_USDC, symbol="WHATEVER"))
    assert is_stable(_asset("base", BASE_USDC.lower()))
    assert size_amount_raw(1.25, _asset("base", BASE_USDC), None) == 1_250_000


def test_usdg_on_robinhood_stays_pegged():
    row = chains.get("robinhood")
    usdg = next(s for s in row.stables if s[1] == "USDG")
    asset = _asset("robinhood", usdg[0], "USDG", decimals=usdg[3])
    assert is_stable(asset)
    assert size_amount_raw(2.0, asset, None) == 2 * 10 ** usdg[3]


def test_every_builtin_row_is_a_stable():
    for row in BUILTIN_ASSETS.values():
        assert is_stable(row), row.asset_id


def test_a_solana_mint_compares_case_exactly():
    mint = BUILTIN_ASSETS["usdc-solana"].address
    assert is_stable(_asset("solana", mint))
    assert not is_stable(_asset("solana", mint.lower()))
    assert chains.is_usd_pegged("solana", mint)
    assert not chains.is_usd_pegged("solana", mint.lower())


def test_a_native_coin_and_an_unknown_chain_are_not_stables():
    assert not is_stable(_asset("base", None))
    assert not is_stable(_asset("nochain", BASE_USDC))


# -- 085 reval: every "is this $1" reader uses the same pinned table ----------

def test_pay_text_shows_par_only_for_the_pinned_usdc():
    from modules.x402.artifact import _build_pay_text, usdc_at_par
    kw = dict(request_id="r", amount_usd=0.5, chain="base", recipient="0xR",
              purpose="p", expiry_text="soon", symbol="USDC", decimals=6)
    fake = _build_pay_text(amount_raw=123_456_789, at_par=usdc_at_par("base", FAKE, "USDC"), **kw)
    assert "$0.50 USDC" not in fake and "123.456789 USDC" in fake
    real = _build_pay_text(amount_raw=500_000, at_par=usdc_at_par("base", BASE_USDC, "USDC"), **kw)
    assert "Pay $0.50 USDC" in real
    assert usdc_at_par("base", "", "USDC")  # a legacy invoice keeps its par line


def test_invoice_card_amount_block_uses_the_pinned_answer():
    from modules.cards.cards import _amount_block_lines
    block = _amount_block_lines(raw=123_456_789, decimals=6, symbol="USDC",
                                amount_usd="0.50", at_par=False)
    assert block["exact"] == "123.456789 USDC"
    assert _amount_block_lines(raw=500_000, decimals=6, symbol="USDC",
                               amount_usd="0.50", at_par=True)["exact"] == ""


def test_the_default_invoice_asset_is_never_an_unpinned_usdc_row(monkeypatch):
    from core.payments import assets as _assets
    from modules.x402 import invoice_assets
    fake_row = _asset("arbitrum", FAKE, "USDC")
    monkeypatch.setattr(_assets, "all_assets", lambda *a, **kw: [fake_row])
    monkeypatch.delenv("PAYMENT_DEFAULT_ASSET", raising=False)
    with pytest.raises(ValueError, match="no default payable asset"):
        invoice_assets.resolve_invoice_asset("arbitrum", None)


def test_the_price_anchor_reads_the_same_table():
    from tools.intel import price
    q = price.PriceQuote(usd=0.5, confidence="low")
    mint = BUILTIN_ASSETS["usdc-solana"].address
    assert price._anchored("solana", mint, q, use_cache=True).usd == 1.0
    assert price._anchored("solana", mint.lower(), q, use_cache=True).usd == 0.5
