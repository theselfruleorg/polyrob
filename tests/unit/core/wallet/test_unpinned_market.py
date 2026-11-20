"""124 W4: a buyer call on the market of an UNPINNED ERC-6551P collection is found from the chain
(the listing's collection, its MARKET()) and goes to the owner's card."""
import types

from core.wallet import collection_registry, nft_rules as tx_guard_sale

MARKET = "0x" + "4d" * 20
COLL = "0x" + "c0" * 20


def _patch(monkeypatch, market=MARKET):
    import core.wallet.sale_conditions as S
    monkeypatch.setattr(S, "decode_buy", lambda data: types.SimpleNamespace(
        listing=types.SimpleNamespace(collection=COLL)))
    monkeypatch.setattr(collection_registry, "chain_profile", lambda chain, c, rpc=None: types.SimpleNamespace(
        address=c, erc6551p=types.SimpleNamespace(market=market)))
    return S


def test_the_market_of_an_unpinned_collection_is_found(monkeypatch):
    S = _patch(monkeypatch)
    got = tx_guard_sale.unpinned_market("robinhood", {"to": MARKET, "data": S.BUY_SELECTOR + "00"}, None)
    assert got and got[0] == MARKET and got[1].address == COLL


def test_a_contract_that_is_not_the_collections_market_is_no_market(monkeypatch):
    S = _patch(monkeypatch, market="0x" + "11" * 20)
    assert tx_guard_sale.unpinned_market("robinhood", {"to": MARKET, "data": S.BUY_SELECTOR}, None) is None
    assert tx_guard_sale.unpinned_market("robinhood", {"to": MARKET, "data": "0xa9059cbb"}, None) is None
