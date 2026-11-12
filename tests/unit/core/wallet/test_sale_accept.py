"""111 §8.1 ``accept`` and the market's single-token approval (115 W9a), through
``tx_guard.authorize``.

* ``approve(market, id)`` on a pinned collection: a sale act — only to ITS pinned market, from the
  treasury itself, the owner of the id; measured: exactly one ``Approval(treasury, market, id)``,
  ``getApproved`` = the market after, nothing else. Any other spender refuses; ``approve(0, id)``
  stays the single-token revoke shape.
* ``accept(offer, sig, id, buyerConditions, [])``: an NFT operation whose one declared move is that
  token; the offer's collection, an ERC-20 on the reviewed list, its id, its conditions hash, the
  market's own ``offerDigest``, the approval in place; measured: the token out to the buyer, one
  ``Sold``, the treasury paid exactly ``price − royalty``, the market holding nothing, the salt
  used, the approval consumed.
"""
import pytest

from core.wallet import sale_acts as A, sale_conditions as S, sale_orders as O, tx_guard
from core.wallet.simulation import Deltas
from tests.unit.core.wallet import test_sale_orders as T
from tests.unit.core.wallet.test_tx_guard_via_account import COLLECTION, _run

_pins = T._pins          # the pinned profile and code hashes (autouse)
ME = T.HOLDER
MARKET = T.MARKET
COLL = COLLECTION.lower()
BUYER = "0x" + "be" * 20
TID = T.TOKEN_ID
PRICE = 5 * 10 ** 6
ROYALTY = 10 ** 5
CONDS = [S.Condition(S.ETH, S.ZERO, 0, 1)]


def _w(n):
    return T._w(n)


def _tx(to, data, value=0):
    return {"to": to, "data": data, "value": value, "chainId": 8453, "nonce": 1,
            "gas": 300_000, "maxFeePerGas": 10 ** 8}


# ------------------------------------------------------------------------------ approve(market)

def _approve_intent(**kw):
    base = dict(chain="base", token=None, to=COLL, amount_raw=0, max_spend_usd=2.0,
                is_nft_op=True, idempotency_key="k-approve")
    base.update(kw)
    return tx_guard.TxIntent(**base)


def _approve_log(spender=MARKET, owner=ME):
    return {"address": COLL, "topics": [A.TOPIC_APPROVAL, A.topic_word(owner),
                                        A.topic_word(spender), _w(TID)], "data": "0x"}


def _approve_deltas(*, approvals=((COLL, MARKET, TID),), logs=None, after=MARKET, **kw):
    logs = (_approve_log(),) if logs is None else logs
    key = (COLL, A.encode_get_approved(TID))
    base = dict(ok=True, native_delta=-10 ** 12, gas_used=50_000, logs=tuple(logs),
                holder_nft_approvals=tuple(approvals),
                event_topics=tuple((lg["address"], lg["topics"][0]) for lg in logs),
                views_before={key: 0}, views_after={key: int(after, 16)})
    base.update(kw)
    return Deltas(**base)


def _approve(spender=MARKET, intent=None, deltas=None, rpc=None, sim_calls=None):
    return _run(intent or _approve_intent(), deltas or _approve_deltas(),
                _tx(COLL, A.encode_approve(spender, TID)), holder=ME,
                rpc=rpc or T.MarketRpc(approved=S.ZERO), sim_calls=sim_calls)


def test_the_market_approval_of_one_token_is_allowed_and_measured():
    calls = []
    d = _approve(sim_calls=calls)
    assert d.allowed is True, d.reason
    assert calls[0]["views"] == ((COLL, A.encode_get_approved(TID)),)


@pytest.mark.parametrize("kw, needle", [
    (dict(spender="0x" + "77" * 20), "only to its PINNED market"),
    (dict(intent=_approve_intent(is_nft_op=False)), "amount must be greater than zero"),
    (dict(intent=_approve_intent(nft_out=((COLL, "erc721", TID, 1),))), "sale act"),
    (dict(rpc=T.MarketRpc(owner="0x" + "5e" * 20, approved=S.ZERO)), "not this treasury"),
    (dict(rpc=T.MarketRpc(market_code="0x6080", approved=S.ZERO)), "pinned market"),
    (dict(deltas=_approve_deltas(approvals=())), "not exactly"),
    (dict(deltas=_approve_deltas(approvals=((COLL, MARKET, TID), (COLL, MARKET, 2)))), "not exactly"),
    (dict(deltas=_approve_deltas(after=S.ZERO)), "not the market"),
    (dict(deltas=_approve_deltas(logs=(_approve_log(), _approve_log()))), "2 Approval"),
    (dict(deltas=_approve_deltas(logs=(_approve_log(), {"address": "0x" + "77" * 20,
                                                        "topics": ["0x" + "12" * 32],
                                                        "data": "0x"}))), "emits"),
    (dict(deltas=_approve_deltas(holder_operator_grants=((COLL, MARKET, True),))), "something else"),
    (dict(deltas=_approve_deltas(holder_nft_out=((COLL, "erc721", BUYER, TID, 1),))), "something else"),
])
def test_market_approval_refusals(kw, needle):
    d = _approve(**kw)
    assert d.allowed is False and needle in d.reason, d.reason


def test_a_blanket_operator_grant_to_the_market_still_refuses():
    data = "0xa22cb465" + "0" * 24 + MARKET[2:] + _w(1)[2:]       # setApprovalForAll(market, true)
    d = _run(_approve_intent(), _approve_deltas(approvals=(), logs=(),
                                                holder_operator_grants=((COLL, MARKET, True),)),
             _tx(COLL, data), holder=ME, rpc=T.MarketRpc())
    assert d.allowed is False, d.reason


def test_an_approval_through_the_account_refuses():
    d = _run(_approve_intent(via_account=T.ACCOUNT, via_account_state=1), _approve_deltas(),
             _tx(T.ACCOUNT, A.encode_approve(MARKET, TID)), holder=ME, rpc=T.MarketRpc())
    assert d.allowed is False, d.reason


# ------------------------------------------------------------------------------ accept

def _offer(**kw):
    base = dict(price=PRICE, maxRoyalty=2 * ROYALTY, conditionsHash=S.conditions_hash(CONDS),
                buyer=BUYER)
    base.update(kw)
    return T._offer(**base)


def _accept_data(offer=None, tid=TID, conds=None, promise=()):
    return O.encode_accept(offer or _offer(), "0x" + "ab" * 65, tid,
                           CONDS if conds is None else conds, list(promise))


def _accept_intent(**kw):
    base = dict(chain="base", token=None, to=MARKET, amount_raw=0, max_spend_usd=2.0,
                is_nft_op=True, nft_out=((COLL, "erc721", TID, 1),), idempotency_key="k-accept")
    base.update(kw)
    return tx_guard.TxIntent(**base)


def _sold(price=PRICE, royalty=ROYALTY, buyer=BUYER, seller=ME, currency=T.USDC):
    return {"address": MARKET, "topics": [S.TOPIC_SOLD, A.topic_word(COLL), _w(TID),
                                          A.topic_word(seller)],
            "data": "0x" + A.topic_word(buyer)[2:] + A.topic_word(currency)[2:] + _w(price)[2:]
                    + _w(royalty)[2:]}


def _nft_transfer(to=BUYER):
    return {"address": COLL, "topics": [S.TOPIC_TRANSFER, A.topic_word(ME), A.topic_word(to),
                                        _w(TID)], "data": "0x"}


def _paid(amount=PRICE - ROYALTY, frm=MARKET):
    return {"address": T.USDC, "topics": [S.TOPIC_TRANSFER, A.topic_word(frm), A.topic_word(ME)],
            "data": _w(amount)}


def _accept_views(owner=(ME, BUYER), approved=(MARKET, S.ZERO), salt=(0, 1),
                  mine=(10, 10 + PRICE - ROYALTY), held=(3, 3)):
    keys = [(COLL, S.encode_owner_of(TID)), (COLL, O.encode_get_approved(TID)),
            (MARKET, A.encode_used_salt(BUYER, 99)), (T.USDC, O.encode_balance_of(ME)),
            (T.USDC, O.encode_balance_of(MARKET))]
    pairs = [tuple(int(x, 16) for x in owner), tuple(int(x, 16) for x in approved), salt, mine,
             held]
    return {"views_before": {k: v[0] for k, v in zip(keys, pairs)},
            "views_after": {k: v[1] for k, v in zip(keys, pairs)}}


def _accept_deltas(*, logs=None, out=None, market=0, **kw):
    logs = (_nft_transfer(), _paid(), _sold()) if logs is None else logs
    base = dict(ok=True, native_delta=-10 ** 12, gas_used=200_000, logs=tuple(logs),
                holder_nft_out=((COLL, "erc721", BUYER, TID, 1),) if out is None else out,
                watched_native_deltas={MARKET: market} if market is not None else {},
                event_topics=tuple((lg["address"], lg["topics"][0]) for lg in logs),
                **_accept_views())
    base.update(kw)
    return Deltas(**base)


def _accept(intent=None, deltas=None, data=None, rpc=None, sim_calls=None, value=0):
    return _run(intent or _accept_intent(), deltas or _accept_deltas(),
                _tx(MARKET, data or _accept_data(), value), holder=ME,
                rpc=rpc or T.MarketRpc(), sim_calls=sim_calls)


def test_a_measured_accept_is_allowed():
    calls = []
    d = _accept(sim_calls=calls)
    assert d.allowed is True, d.reason
    (kw,) = calls
    assert kw["watch_native"] == (MARKET,) and len(kw["views"]) == 5


def test_an_any_id_offer_is_accepted_for_the_named_token():
    o = _offer(anyId=True, id=0)
    d = _accept(data=_accept_data(offer=o))
    assert d.allowed is True, d.reason


@pytest.mark.parametrize("intent_kw, data_kw, value, needle", [
    (dict(is_nft_op=False, nft_out=()), {}, 0, "amount must be greater than zero"),
    (dict(is_nft_op=False, nft_out=(), amount_raw=1), {}, 0,
     "only by the guarded agent_nft_sale_accept"),
    (dict(nft_out=((COLL, "erc721", 2, 1),)), {}, 0, "exactly the token that leaves"),
    (dict(nft_out=()), {}, 0, "exactly the token that leaves"),
    (dict(amount_raw=5), {}, 0, "not amount_raw"),
    (dict(token=T.USDC), {}, 0, "declares only the token"),
    (dict(expected_nft_in=((COLL, "erc721", 2, 1),)), {}, 0, "declares only the token"),
    ({}, {}, 5, "carries no value"),
    ({}, dict(offer=_offer(currency=S.ZERO)), 0, "ERC-20 only"),
    ({}, dict(offer=_offer(collection="0x" + "67" * 20)), 0, "not the pinned collection"),
    ({}, dict(offer=_offer(id=2)), 0, "not #1"),
    ({}, dict(promise=CONDS), 0, "seller's promise"),
    ({}, dict(conds=[S.Condition(S.ETH, S.ZERO, 0, 2)]), 0, "do not hash"),
    ({}, dict(conds=[], offer=_offer(conditionsHash=S.conditions_hash([]))), 0, "no buyer condition"),
    ({}, dict(offer=_offer(buyer=ME)), 0, "its own offer"),
    ({}, dict(offer=_offer(maxRoyalty=PRICE + 1)), 0, "not a price"),
])
def test_accept_shape_refusals(intent_kw, data_kw, value, needle):
    d = _accept(intent=_accept_intent(**intent_kw), data=_accept_data(**data_kw), value=value)
    assert d.allowed is False and needle in d.reason, d.reason


@pytest.mark.parametrize("data_kw, rpc_kw, needle", [
    (dict(offer=_offer(currency="0x" + "77" * 20)), {}, "reviewed list"),
    ({}, dict(owner="0x" + "5e" * 20), "does not own"),
    ({}, dict(approved=S.ZERO), "not approved"),
    ({}, dict(collection_market="0x" + "b2" * 20), "MARKET()"),
    (dict(offer=_offer(expiry=T.NOW - 1)), {}, "expired"),
    ({}, dict(buyer_nonce=1), "nonce moved"),
    ({}, dict(buyer_salt_used=True), "salt is used"),
    ({}, dict(digest_off=True), "disagree"),
    ({}, dict(dom_chain=1), "another contract or chain"),
    ({}, dict(market_code="0x6080"), "pinned market"),
])
def test_accept_runtime_refusals(data_kw, rpc_kw, needle):
    calls = []
    d = _accept(data=_accept_data(**data_kw), rpc=T.MarketRpc(**rpc_kw), sim_calls=calls)
    assert d.allowed is False and needle in d.reason, d.reason
    assert calls == []


@pytest.mark.parametrize("deltas, needle", [
    (_accept_deltas(out=()), "moves nothing out"),
    (_accept_deltas(out=((COLL, "erc721", "0x" + "77" * 20, TID, 1),)), "not exactly"),
    (_accept_deltas(logs=(_nft_transfer(), _paid())), "0 Sold"),
    (_accept_deltas(logs=(_nft_transfer(), _paid(), _sold(), _sold())), "2 Sold"),
    (_accept_deltas(logs=(_nft_transfer(), _paid(), _sold(buyer="0x" + "77" * 20))), "not this offer"),
    (_accept_deltas(logs=(_nft_transfer(), _paid(), _sold(price=PRICE - 1))), "the offer says"),
    (_accept_deltas(logs=(_nft_transfer(), _paid(), _sold(royalty=3 * ROYALTY))), "the offer says"),
    (_accept_deltas(logs=(_nft_transfer(), _paid(PRICE - ROYALTY - 1), _sold())), "not exactly price"),
    (_accept_deltas(logs=(_nft_transfer(), _sold())), "not exactly price"),
    (_accept_deltas(logs=(_nft_transfer(), _paid(), _sold(), {
        "address": MARKET, "topics": ["0x" + "12" * 32], "data": "0x"})), "not part of an accept"),
    (_accept_deltas(logs=(_nft_transfer(), _paid(), _sold(), {
        "address": "0x" + "77" * 20, "topics": ["0x" + "12" * 32, A.topic_word(ME)],
        "data": "0x"})), "naming the treasury"),
    (_accept_deltas(market=5), "ETH moves"),
    (_accept_deltas(**_accept_views(approved=(MARKET, MARKET))), "approval survived"),
    (_accept_deltas(**_accept_views(owner=(ME, "0x" + "77" * 20))), "not the treasury → the buyer"),
    (_accept_deltas(**_accept_views(salt=(0, 0))), "salt goes"),
    (_accept_deltas(**_accept_views(mine=(10, 11))), "not up by exactly"),
    (_accept_deltas(**_accept_views(held=(3, 4))), "holds something after"),
    (_accept_deltas(views_before={}, views_after={}), "did not measure"),
    (_accept_deltas(holder_transfers=((T.USDC, MARKET, 5),)), "more of the treasury"),
    (_accept_deltas(holder_nft_approvals=((COLL, "0x" + "77" * 20, 3),)), "more of the treasury"),
    (_accept_deltas(holder_nft_in=((COLL, "erc721", BUYER, 2, 1),)), "more of the treasury"),
])
def test_accept_measurement_refusals(deltas, needle):
    d = _accept(deltas=deltas)
    assert d.allowed is False and needle in d.reason, d.reason


def test_checked_fill_still_refuses_by_name():
    d = _accept(data=S.CHECKED_FILL_SELECTOR + "00" * 64)
    assert d.allowed is False and "checkedFill is not built" in d.reason, d.reason
