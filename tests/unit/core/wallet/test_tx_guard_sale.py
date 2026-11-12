"""115 W9 / 111 §8.1 — the guard of the BUYER's calls on a pinned market
(``core/wallet/tx_guard_sale.py`` through ``tx_guard.authorize``).

* ``buy``: a native payment of exactly the listing's price to the PINNED market of a pinned
  collection, the NFT declared as the one arrival, the recipient the treasury, ETH listings
  only, the buyer conditions EQUAL to a fresh read of the account (only unchecked-value
  holdings may be dropped); measured: one NFT in from the seller, one ``Sold`` to the treasury,
  ownerOf seller → treasury, the salt used, the market holding nothing but what it books owed;
* ``claim_owed``: ``claim(treasury)`` on the claim shape, owed() before → 0 after, paid exactly;
* ``checkedFill`` refuses by name (not built); ``accept`` is the seller's own shape; any other call on the market still
  refuses in the seller's gate; a ``ConditionFailed`` revert is named.
"""
import pytest

from core.wallet import collection_registry, erc6551, sale_acts as A, sale_conditions as S
from core.wallet import tx_guard
from core.wallet.simulation import Deltas
from tests.unit.core.wallet import test_tx_guard_polyrob_account as P
from tests.unit.core.wallet.test_tx_guard_via_account import COLLECTION, TREASURY, _run

MARKET = "0x" + "b1" * 20
SELLER = "0x" + "5e" * 20
OTHER = "0x" + "77" * 20
TOKEN_ID = 1
PRICE = 10 ** 16
ACCOUNT = P._profile().account_of(TOKEN_ID)[0].lower()
ETH_IN_ACCOUNT = 3 * 10 ** 15


@pytest.fixture(autouse=True)
def _pins(monkeypatch):
    monkeypatch.setattr(erc6551, "CODE_SHA256", {erc6551.REGISTRY.lower(): P._H00,
                                                 erc6551.ACCOUNT_V3_IMPL.lower(): "f" * 64})
    monkeypatch.setattr(collection_registry, "profiles", lambda: (P._profile(),))


def _w(n):
    return "0x" + format(int(n), "064x")


class BuyRpc:
    """The pinned collection and market (code = b'\\x00', their pin) and the sold token's
    account: ETH only, no identity, no mint position, no logs."""

    def __init__(self, *, eth=ETH_IN_ACCOUNT, owed=0, market_code="0x00", junk=False):
        self.eth, self.owed, self.market_code, self.junk = eth, owed, market_code, junk

    def __call__(self, method, params):
        if method == "eth_getCode":
            return self.market_code if params[0].lower() == MARKET else "0x00"
        if method == "eth_blockNumber":
            return hex(100)
        if method == "eth_getBalance":
            assert params[0].lower() == ACCOUNT, params
            return hex(self.eth)
        if method == "eth_getLogs":
            if self.junk and params[0]["topics"][0] == S.TOPIC_TRANSFER:
                return [{"address": OTHER, "topics": [S.TOPIC_TRANSFER, _w(1),
                                                      _w(int(ACCOUNT, 16))], "data": _w(3)}]
            return []
        if method == "eth_call":
            to, sel = params[0]["to"].lower(), params[0]["data"][:10]
            if to == COLLECTION.lower():
                return {S.HAS_IDENTITY_SELECTOR: _w(0), S.POSITION_OF_SELECTOR: _w(0)}[sel]
            if to == MARKET and sel == S.OWED_SELECTOR:
                return _w(self.owed)
            if sel == S.BALANCE_OF_SELECTOR:
                return _w(3 if to == OTHER else 0)
            if sel == S.DECIMALS_SELECTOR:
                return _w(18)
            if sel == S.SYMBOL_SELECTOR:
                return "0x"
        raise AssertionError((method, params))


def _listing(**kw):
    base = dict(collection=COLLECTION.lower(), id=TOKEN_ID, transferCount=1, seller=SELLER,
                currency=S.ZERO, price=PRICE, maxRoyalty=5 * 10 ** 14, expiry=2 * 10 ** 9,
                salt=7, promiseHash=S.conditions_hash([]), nonce=0)
    base.update(kw)
    return S.Listing.of(base)


def _conds(rpc=None):
    _snap, built = S.read_and_build(rpc or BuyRpc(), collection=COLLECTION, token_id=TOKEN_ID,
                                    account=ACCOUNT, deploy_block=0, ctx=S.read_context("base"))
    return built.conditions


def _buy_tx(listing=None, conds=None, recipient=TREASURY, value=None, promise=(), to=MARKET,
            data=None):
    listing = listing or _listing()
    data = data or S.encode_buy(listing, "0x" + "ab" * 65, list(promise),
                                _conds() if conds is None else conds, recipient)
    return {"to": to, "data": data, "value": PRICE if value is None else value, "chainId": 8453,
            "nonce": 1, "gas": 300_000, "maxFeePerGas": 10 ** 8}


def _buy_intent(**kw):
    base = dict(chain="base", token=None, to=MARKET, amount_raw=PRICE, max_spend_usd=5.0,
                expected_nft_in=((COLLECTION.lower(), "erc721", TOKEN_ID, 1),),
                idempotency_key="k-buy")
    base.update(kw)
    return tx_guard.TxIntent(**base)


def _topic(addr):
    return A.topic_word(addr)


def _sold(*, buyer=TREASURY, price=PRICE, royalty=0, currency=S.ZERO, token_id=TOKEN_ID):
    return {"address": MARKET, "topics": [S.TOPIC_SOLD, _topic(COLLECTION), _w(token_id),
                                          _topic(SELLER)],
            "data": "0x" + _topic(buyer)[2:] + _topic(currency)[2:] + _w(price)[2:]
                    + _w(royalty)[2:]}


def _transfer(frm=SELLER, to=TREASURY, token_id=TOKEN_ID):
    return {"address": COLLECTION.lower(), "topics": [S.TOPIC_TRANSFER, _topic(frm), _topic(to),
                                                      _w(token_id)], "data": "0x"}


def _views(owner=(SELLER, TREASURY), salt=(0, 1)):
    k1 = (COLLECTION.lower(), S.encode_owner_of(TOKEN_ID).lower())
    k2 = (MARKET, A.encode_used_salt(SELLER, 7).lower())
    return {"views_before": {k1: int(owner[0], 16), k2: salt[0]},
            "views_after": {k1: int(owner[1], 16), k2: salt[1]}}


def _buy_deltas(*, logs=None, nft_in=None, market=0, native=-PRICE, **kw):
    logs = (_transfer(), _sold()) if logs is None else logs
    base = dict(ok=True, native_delta=native, gas_used=150_000, logs=tuple(logs),
                holder_nft_in=((COLLECTION.lower(), "erc721", SELLER, TOKEN_ID, 1),)
                if nft_in is None else nft_in,
                watched_native_deltas={MARKET: market} if market is not None else {},
                event_topics=tuple((lg["address"].lower(), lg["topics"][0]) for lg in logs),
                **_views())
    base.update(kw)
    return Deltas(**base)


def _buy(intent=None, deltas=None, tx=None, rpc=None, sim_calls=None):
    return _run(intent or _buy_intent(), deltas or _buy_deltas(), tx or _buy_tx(),
                holder=TREASURY, rpc=rpc or BuyRpc(), sim_calls=sim_calls)


# ------------------------------------------------------------------------------ buy

def test_a_measured_buy_is_allowed():
    calls = []
    d = _buy(sim_calls=calls)
    assert d.allowed is True, d.reason
    (kw,) = calls
    assert kw["watch_native"] == (MARKET,)
    assert set(kw["views"]) == {(COLLECTION.lower(), S.encode_owner_of(TOKEN_ID)),
                                (MARKET, A.encode_used_salt(SELLER, 7))}


def test_an_owed_payment_to_the_seller_is_what_the_market_may_keep():
    owed = {"address": MARKET, "topics": [S.TOPIC_OWED, _topic(SELLER)], "data": _w(PRICE)}
    d = _buy(deltas=_buy_deltas(logs=(_transfer(), owed, _sold()), market=PRICE))
    assert d.allowed is True, d.reason


@pytest.mark.parametrize("deltas, needle", [
    (_buy_deltas(nft_in=()), "brings in nothing"),
    (_buy_deltas(nft_in=((COLLECTION.lower(), "erc721", SELLER, TOKEN_ID, 1),
                         (OTHER, "erc721", SELLER, 3, 1))), "not exactly"),
    (_buy_deltas(nft_in=((COLLECTION.lower(), "erc721", OTHER, TOKEN_ID, 1),)), "not exactly"),
    (_buy_deltas(logs=(_transfer(),)), "0 Sold events"),
    (_buy_deltas(logs=(_transfer(), _sold(), _sold())), "2 Sold events"),
    (_buy_deltas(logs=(_transfer(), _sold(buyer=OTHER))), "not this listing"),
    (_buy_deltas(logs=(_transfer(), _sold(price=PRICE - 1))), "price"),
    (_buy_deltas(logs=(_transfer(), _sold(royalty=6 * 10 ** 14))), "at most"),
    (_buy_deltas(market=5), "holds something"),
    (_buy_deltas(market=None), "native balance"),
    (_buy_deltas(logs=(_transfer(), _sold(), {"address": MARKET, "topics": [
        S.TOPIC_OWED, _topic(TREASURY)], "data": _w(1)})), "owed to the treasury"),
    (_buy_deltas(logs=(_transfer(), _sold(), {"address": MARKET, "topics": ["0x" + "12" * 32],
                                               "data": "0x"})), "not part of a buy"),
    (_buy_deltas(logs=(_transfer(), _sold(), {"address": OTHER, "topics": [
        "0x" + "12" * 32, _topic(TREASURY)], "data": "0x"})), "naming the treasury"),
    (_buy_deltas(**{**_views(owner=(SELLER, OTHER))}), "not the seller"),
    (_buy_deltas(**{**_views(salt=(0, 0))}), "salt goes"),
    (_buy_deltas(views_before={}, views_after={}), "did not measure ownerOf"),
    (_buy_deltas(native=-PRICE // 2), "moves only"),
    (_buy_deltas(holder_approvals=((OTHER, MARKET, 5),)), "approves more"),
])
def test_buy_measurement_refusals(deltas, needle):
    d = _buy(deltas=deltas)
    assert d.allowed is False and needle in d.reason, d.reason


@pytest.mark.parametrize("intent_kw, tx_kw, needle", [
    ({}, dict(listing=_listing(currency=OTHER)), "ETH listings only"),
    ({}, dict(recipient=OTHER), "not to the signing treasury"),
    ({}, dict(value=PRICE - 1), "all three must be the same"),
    (dict(amount_raw=PRICE - 1), {}, "all three must be the same"),
    (dict(expected_nft_in=((COLLECTION.lower(), "erc721", 2, 1),)), {}, "exactly the NFT"),
    (dict(expected_nft_in=()), {}, "exactly the NFT"),
    (dict(is_nft_op=True), {}, "native payment"),
    (dict(is_transfer=True), {}, "native payment"),
    (dict(token=OTHER), {}, "token"),
    (dict(to=OTHER), {}, "addressed to it"),
    ({}, dict(listing=_listing(collection=OTHER)), "not the pinned collection"),
    ({}, dict(listing=_listing(seller=TREASURY)), "from itself"),
    ({}, dict(conds=[]), "no buyer condition"),
    ({}, dict(listing=_listing(promiseHash="0x" + "11" * 32)), "promiseHash"),
    ({}, dict(data=S.CHECKED_FILL_SELECTOR + "00" * 64), "checkedFill is not built"),
    ({}, dict(data=S.ACCEPT_SELECTOR + "00" * 64), "only by the guarded agent_nft_sale_accept"),  # 111 W9a
])
def test_buy_shape_refusals(intent_kw, tx_kw, needle):
    d = _buy(intent=_buy_intent(**intent_kw), tx=_buy_tx(**tx_kw))
    assert d.allowed is False and needle in d.reason, d.reason


def test_a_buy_through_an_account_is_refused():
    from tests.unit.core.wallet.test_tx_guard_via_account import _outer, _via
    intent = _via(_buy_intent(), account=P.ACCOUNT)
    inner = _buy_tx()
    d = _run(intent, _buy_deltas(), _outer(inner, to=P.ACCOUNT, value=0), holder=TREASURY,
             rpc=BuyRpc())
    assert d.allowed is False and "via_account" in d.reason, d.reason


def test_any_other_call_on_the_market_still_refuses():
    d = _buy(tx=_buy_tx(data="0x12345678"))
    assert d.allowed is False and "only to cancel" in d.reason, d.reason


def test_the_guard_rebuilds_the_conditions_and_refuses_a_changed_account():
    """The seller emptied the account after the quote: the calldata's ETH minimum is the old
    balance, the fresh read builds another — refused before any simulation."""
    calls = []
    stale = _buy_tx(conds=_conds(BuyRpc(eth=ETH_IN_ACCOUNT)))
    d = _buy(tx=stale, rpc=BuyRpc(eth=0), sim_calls=calls)
    assert d.allowed is False and "fresh read" in d.reason and "quote again" in d.reason, d.reason
    assert calls == []


def test_a_caller_list_that_is_weaker_than_the_fresh_read_is_refused():
    weak = [S.Condition(S.ETH, S.ZERO, 0, 1)]
    d = _buy(tx=_buy_tx(conds=weak))
    assert d.allowed is False and "fresh read" in d.reason, d.reason


def test_the_buyer_tolerance_of_50_bps_is_admissible():
    snap, built = S.read_and_build(BuyRpc(), collection=COLLECTION, token_id=TOKEN_ID,
                                   account=ACCOUNT, deploy_block=0, ctx=S.read_context("base"),
                                   tolerance_bps=50)
    assert built.conditions != _conds()
    d = _buy(tx=_buy_tx(conds=built.conditions))
    assert d.allowed is True, d.reason


def test_an_unreviewed_holding_may_be_dropped_a_reviewed_one_may_not():
    rpc = BuyRpc(junk=True)
    full = _conds(rpc)
    assert [c.kind for c in full] == [S.ETH, S.ERC20] and full[1].target == OTHER
    d = _buy(tx=_buy_tx(conds=full[:1]), rpc=rpc)
    assert d.allowed is True, d.reason       # the junk token: value not checked, droppable
    d = _buy(tx=_buy_tx(conds=full[1:]), rpc=rpc)
    assert d.allowed is False and "missing or changed" in d.reason, d.reason


def test_a_changed_market_code_refuses():
    d = _buy(rpc=BuyRpc(market_code="0x6080"))
    assert d.allowed is False and "pinned market" in d.reason, d.reason


def test_a_condition_failed_revert_is_named():
    d = _buy(deltas=Deltas(ok=False, error="the transaction REVERTS in simulation",
                           revert_data="0x72fc940b" + format(0, "064x")))
    assert d.allowed is False and "ConditionFailed" in d.reason, d.reason


# ------------------------------------------------------------------------------ claim_owed

OWED = 4 * 10 ** 15


def _claim_intent(**kw):
    base = dict(chain="base", token=None, to=MARKET, amount_raw=0, max_spend_usd=5.0,
                is_claim=True, min_native_inflow_wei=OWED, idempotency_key="k-claim")
    base.update(kw)
    return tx_guard.TxIntent(**base)


def _claim_tx(who=TREASURY, value=0):
    return {"to": MARKET, "data": S.encode_claim(who), "value": value, "chainId": 8453,
            "nonce": 1, "gas": 100_000, "maxFeePerGas": 10 ** 8}


def _claim_deltas(*, before=OWED, after=0, native=OWED, market=-OWED, logs=()):
    key = (MARKET, S.encode_owed(TREASURY).lower())
    return Deltas(ok=True, native_delta=native, gas_used=40_000, logs=tuple(logs),
                  watched_native_deltas={MARKET: market}, views_before={key: before},
                  views_after={key: after})


def _claim(intent=None, deltas=None, tx=None, rpc=None):
    return _run(intent or _claim_intent(), deltas or _claim_deltas(), tx or _claim_tx(),
                holder=TREASURY, rpc=rpc or BuyRpc(owed=OWED))


def test_a_measured_claim_is_allowed():
    d = _claim()
    assert d.allowed is True, d.reason


@pytest.mark.parametrize("kw, needle", [
    (dict(deltas=_claim_deltas(after=1)), "pays it out to 0"),
    (dict(deltas=_claim_deltas(native=OWED - 10 ** 12)), "below the declared minimum"),
    (dict(deltas=_claim_deltas(market=-OWED - 1)), "does something else"),
    (dict(deltas=_claim_deltas(logs=({"address": MARKET, "topics": ["0x" + "12" * 32],
                                       "data": "0x"},))), "emits an event"),
    (dict(tx=_claim_tx(who=OTHER)), "not the signing treasury"),
    (dict(tx=_claim_tx(value=1)), "carries no value"),
    (dict(intent=_claim_intent(min_native_inflow_wei=None)), "must declare"),
    (dict(intent=_claim_intent(is_claim=False, min_native_inflow_wei=None, amount_raw=1)),
     "declared as a claim"),
    (dict(rpc=BuyRpc(owed=OWED - 1)), "less than the declared"),
])
def test_claim_refusals(kw, needle):
    d = _claim(**kw)
    assert d.allowed is False and needle in d.reason, d.reason
