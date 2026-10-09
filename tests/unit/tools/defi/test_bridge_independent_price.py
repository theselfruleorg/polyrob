"""A provider's successful arrival must still preserve independently priced value."""
from types import SimpleNamespace

import pytest

from tools.defi.bridge_price import quote_refusal
from tools.defi.providers.relay_bridge import NATIVE_EVM


def _quote(**over):
    return SimpleNamespace(**(dict(currency_out=NATIVE_EVM, decimals_out=18,
        amount_in_raw=10**18, min_out_raw=99 * 10**16, impact_pct=-1,
        amount_in_usd=999999, amount_out_usd=999999) | over))


def _check(quote, price=2000):
    tool = SimpleNamespace(_price=lambda *a: price)
    return quote_refusal(tool, quote, origin="base", destination="robinhood")


def test_independent_floor_passes_and_provider_usd_cannot_hide_loss():
    assert _check(_quote()) is None
    assert "arrival floor" in _check(_quote(min_out_raw=10**16))


@pytest.mark.parametrize("price", [None, 0, -1, True, float('nan'), float('inf')])
def test_unknown_or_nonfinite_price_refuses(price):
    assert _check(_quote(), price)


@pytest.mark.parametrize("impact", [None, 50, -50, float('nan'), float('inf')])
def test_excessive_or_unknown_provider_impact_refuses(impact):
    assert _check(_quote(impact_pct=impact))


def test_provider_cannot_change_native_decimals():
    assert "decimals" in _check(_quote(decimals_out=6))


def test_token_units_come_from_independent_metadata(monkeypatch):
    from core.wallet import chains, tokens
    monkeypatch.setattr(tokens, "get_token_identity",
                        lambda *a: SimpleNamespace(decimals=6))
    target = chains.get("base")
    tool = SimpleNamespace(_price=lambda c, a: 1 if a == target.usdc else 2000)
    quote = _quote(currency_out=target.usdc, decimals_out=6,
                   min_out_raw=1980 * 10**6)
    assert quote_refusal(tool, quote, origin="robinhood", destination="base") is None
    quote.decimals_out = 2
    assert "decimals" in quote_refusal(tool, quote, origin="robinhood", destination="base")
