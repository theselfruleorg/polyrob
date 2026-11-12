"""111 §8.3 (D8) / 115 W9a — signing class O: a pinned market ``Listing``
(``core/wallet/sale_orders.py`` + ``core.wallet.sign_policy``).

* every selector, topic and typehash is computed here from its signature;
* the codec of ``accept`` and of the ``Offer`` round-trips and refuses a non-canonical encoding;
* class O: EXACTLY the market's two types and primary type ``Listing``, the domain the pinned
  market's own (name/version read from ``eip712Domain()``), every field bound to a fact read by
  the classifier's process; each near-miss is class D with its reason;
* ``sign_market_order`` signs only class O and the signature recovers over the market's digest;
  the login rail (``classify`` without facts, ``sign_classified``) never signs typed data.
"""
import copy

import pytest
from eth_account import Account
from eth_utils import keccak

import core.wallet.abi as abi
from core.wallet import collection_registry, erc6551, sale_acts as A
from core.wallet import sale_conditions as S, sale_orders as O
from core.wallet.sign_policy import (D, O as CLASS_O, SignContext, SignRefused, SignRequest,
                                     classify, sign_classified, sign_market_order)
from core.wallet.signer import LocalEoaSigner
from tests.unit.core.wallet import test_tx_guard_polyrob_account as P
from tests.unit.core.wallet.test_tx_guard_via_account import COLLECTION

KEY = "0x" + "21" * 32
HOLDER = Account.from_key(KEY).address.lower()
MARKET = "0x" + "b1" * 20
CHAIN = "base"
CHAIN_ID = 8453
TOKEN_ID = 1
NOW = 1_900_000_000
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
ACCOUNT = P._profile().account_of(TOKEN_ID)[0].lower()
#: An ERC-6551 registry clone (173 bytes: header ‖ implementation ‖ tail ‖ footer).
CLONE = "0x363d3d373d3d3d363d73" + "aa" * 20 + "5af43d82803e903d91602b57fd5bf3" + "00" * 128


@pytest.fixture(autouse=True)
def _pins(monkeypatch):
    monkeypatch.setattr(erc6551, "CODE_SHA256", {erc6551.REGISTRY.lower(): P._H00,
                                                 erc6551.ACCOUNT_V3_IMPL.lower(): "f" * 64})
    monkeypatch.setattr(collection_registry, "profiles", lambda: (P._profile(),))


def _w(n):
    return "0x" + format(int(n), "064x")


def _addr_w(a):
    return _w(int(a, 16))


class MarketRpc:
    """The pinned collection (code b'\\x00', its pin), its market, the token's account (ETH only),
    as the class-O reader and the accept guard read them."""

    def __init__(self, *, owner=HOLDER, tc=1, nonce=0, salt_used=False, eth=10 ** 15,
                 name="PolyrobMarket", version="1", dom_chain=CHAIN_ID, dom_market=MARKET,
                 collection_market=MARKET, market_code="0x00", holder_code="0x", now=NOW,
                 digest_off=False, approved=MARKET, buyer_nonce=0, buyer_salt_used=False):
        self.__dict__.update(locals())
        self.reads = []

    def _domain(self):
        return "0x" + abi.encode(
            [{"type": "bytes1"}, {"type": "string"}, {"type": "string"}, {"type": "uint256"},
             {"type": "address"}, {"type": "bytes32"}, {"type": "uint256[]"}],
            [b"\x0f", self.name, self.version, self.dom_chain, self.dom_market, b"\0" * 32,
             []]).hex()

    def __call__(self, method, params):
        if method == "eth_getCode":
            a = params[0].lower()
            return self.market_code if a == MARKET else (
                "0x00" if a == COLLECTION.lower() else self.holder_code)
        if method == "eth_blockNumber":
            return hex(100)
        if method == "eth_getBlockByNumber":
            return {"timestamp": hex(self.now)}
        if method == "eth_getBalance":
            return hex(self.eth)
        if method == "eth_getLogs":
            return []
        assert method == "eth_call", (method, params)
        to, data = params[0]["to"].lower(), params[0]["data"].lower()
        sel = data[:10]
        self.reads.append((to, sel))
        if to == MARKET:
            if sel == S.EIP712_DOMAIN_SELECTOR:
                return self._domain()
            if sel == A.NONCES_SELECTOR:
                who = "0x" + data[-40:]
                return _w(self.nonce if who == HOLDER else self.buyer_nonce)
            if sel == A.USED_SALT_SELECTOR:
                who = "0x" + data[34:74]
                return _w(self.salt_used if who == HOLDER else self.buyer_salt_used)
            if sel == S.LISTING_DIGEST_SELECTOR:
                (l,) = abi.decode([S._LISTING], "0x" + data[10:])
                d = S.listing_digest(S.Listing.of(l), name="PolyrobMarket", version="1",
                                     chain_id=CHAIN_ID, market=MARKET)
                return "0x" + ("ff" * 32 if self.digest_off else d[2:])
            if sel == O.OFFER_DIGEST_SELECTOR:
                (o,) = abi.decode([O._OFFER], "0x" + data[10:])
                d = O.offer_digest(O.Offer.of(o), name="PolyrobMarket", version="1",
                                   chain_id=CHAIN_ID, market=MARKET)
                return "0x" + ("ff" * 32 if self.digest_off else d[2:])
        if to == COLLECTION.lower():
            return {S.HAS_IDENTITY_SELECTOR: _w(0), S.POSITION_OF_SELECTOR: _w(0),
                    O.MARKET_SELECTOR: _addr_w(self.collection_market),
                    S.OWNER_OF_SELECTOR: _addr_w(self.owner),
                    S.TRANSFER_COUNT_SELECTOR: _w(self.tc),
                    A.GET_APPROVED_SELECTOR: _addr_w(self.approved)}[sel]
        if sel == S.BALANCE_OF_SELECTOR:
            return _w(0)
        if sel == S.DECIMALS_SELECTOR:
            return _w(6)
        if sel == S.SYMBOL_SELECTOR:
            return "0x"
        raise AssertionError((method, params))


def _promise(rpc=None, bps=0):
    ctx = S.read_context(CHAIN)
    snap = S.read_snapshot(rpc or MarketRpc(), collection=COLLECTION, token_id=TOKEN_ID,
                           account=ACCOUNT, deploy_block=0, ctx=ctx)
    return O.seller_promise(snap, ctx, bps).conditions


def _listing(**kw):
    base = dict(collection=COLLECTION.lower(), id=TOKEN_ID, transferCount=1, seller=HOLDER,
                currency=S.ZERO, price=10 ** 16, maxRoyalty=5 * 10 ** 14,
                expiry=NOW + 7 * 86_400, salt=12345, promiseHash=S.conditions_hash(_promise()),
                nonce=0)
    base.update(kw)
    return S.Listing.of(base)


def _typed(listing=None, **dom):
    d = dict(name="PolyrobMarket", version="1", chain_id=CHAIN_ID, market=MARKET)
    d.update(dom)
    return O.typed_listing(listing or _listing(), **d)


def _facts(typed=None, rpc=None, holder=HOLDER):
    return O.read_listing_facts(rpc or MarketRpc(), collection_registry, chain=CHAIN,
                                payload=typed or _typed(), holder=holder)


def _classify(typed=None, rpc=None, holder=HOLDER):
    typed = typed or _typed()
    try:
        facts = _facts(typed, rpc, holder)
    except O.OrderRefused as exc:
        facts = O.ListingFacts(0, refusal=str(exc))
    return classify(SignRequest("eip712", typed), SignContext(holder=holder, order_facts=facts))


# ------------------------------------------------------------------------------ literals

def test_every_literal_is_computed_from_its_signature():
    def sel(sig):
        return "0x" + keccak(text=sig).hex()[:8]
    assert O.OFFER_TYPEHASH == "0x" + keccak(text=O.OFFER_TYPE).hex()
    assert O.ACCEPT_SELECTOR == sel(O.ACCEPT_SIGNATURE)
    assert O.OFFER_DIGEST_SELECTOR == sel(
        "offerDigest((address,uint256,bool,address,address,uint256,uint256,uint256,uint256,"
        "bytes32,uint256))")
    assert O.MARKET_SELECTOR == sel("MARKET()")
    assert O.APPROVE_SELECTOR == A.APPROVE_SELECTOR == sel(A.APPROVE_SIGNATURE)
    assert A.GET_APPROVED_SELECTOR == sel("getApproved(uint256)")
    assert O.TOPIC_APPROVAL == A.TOPIC_APPROVAL == "0x" + keccak(
        text="Approval(address,address,uint256)").hex()
    assert S.LISTING_TYPEHASH == "0x" + keccak(text=S.LISTING_TYPE).hex()
    # the typed data's Listing fields ARE the typehash's, in order
    assert "Listing(" + ",".join(f"{f['type']} {f['name']}" for f in O.LISTING_FIELDS) + ")" \
        == S.LISTING_TYPE


def test_the_desk_form_of_the_offer_type_is_the_markets():
    # PolyrobMarket.sol:40-43, web/abi.js EIP712.offer
    assert O.OFFER_TYPE == (
        "Offer(address collection,uint256 id,bool anyId,address buyer,address currency,uint256 "
        "price,uint256 maxRoyalty,uint256 expiry,uint256 salt,bytes32 conditionsHash,"
        "uint256 nonce)")


def _offer(**kw):
    base = dict(collection=COLLECTION.lower(), id=TOKEN_ID, anyId=False, buyer="0x" + "be" * 20,
                currency=USDC, price=5 * 10 ** 6, maxRoyalty=2 * 10 ** 5, expiry=NOW + 86_400,
                salt=99, conditionsHash=S.conditions_hash([S.Condition(S.ETH, S.ZERO, 0, 1)]),
                nonce=0)
    base.update(kw)
    return O.Offer.of(base)


def test_the_accept_codec_round_trips_and_refuses_a_non_canonical_encoding():
    conds = [S.Condition(S.ETH, S.ZERO, 0, 1)]
    data = O.encode_accept(_offer(), "0x" + "ab" * 65, TOKEN_ID, conds, [])
    call = O.decode_accept(data)
    assert call.offer == _offer() and call.token_id == TOKEN_ID and call.conditions == tuple(conds)
    with pytest.raises(ValueError):
        O.decode_accept(data + "00")
    with pytest.raises(ValueError):
        O.decode_accept(S.BUY_SELECTOR + data[10:])


def test_the_offer_digest_is_eip712():
    o = _offer()
    ds = S.domain_separator("PolyrobMarket", "1", CHAIN_ID, MARKET)
    want = "0x" + keccak(b"\x19\x01" + bytes.fromhex(ds[2:])
                         + bytes.fromhex(O.offer_struct_hash(o)[2:])).hex()
    assert O.offer_digest(o, name="PolyrobMarket", version="1", chain_id=CHAIN_ID,
                          market=MARKET) == want


# ------------------------------------------------------------------------------ class O

def test_a_bound_listing_is_class_o():
    v = _classify()
    assert v.cls == CLASS_O, v.reason
    assert v.digest == S.listing_digest(_listing(), name="PolyrobMarket", version="1",
                                        chain_id=CHAIN_ID, market=MARKET)
    assert v.decoded["kind"] == "market_listing" and v.decoded["price"] == 10 ** 16
    assert v.signable is False      # never the login rail's class


def test_a_reviewed_token_price_and_the_50_bps_promise_are_class_o():
    l = _listing(currency=USDC, promiseHash=S.conditions_hash(_promise(bps=50)))
    assert _classify(_typed(l)).cls == CLASS_O


def _mutate(path, value):
    t = _typed()
    node = t
    for k in path[:-1]:
        node = node[k]
    if value is KeyError:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return t


NEAR_MISSES = [
    # the domain
    (lambda: _mutate(("domain", "verifyingContract"), "0x" + "b2" * 20), {}, "verifyingContract"),
    (lambda: _mutate(("domain", "chainId"), 1), {}, "chainId"),
    (lambda: _mutate(("domain", "name"), "Seaport"), {}, "eip712Domain()"),
    (lambda: _mutate(("domain", "version"), "2"), {}, "eip712Domain()"),
    (lambda: _mutate(("domain", "salt"), "0x" + "00" * 32), {}, "exactly {name"),
    (lambda: _typed(), dict(dom_market="0x" + "b2" * 20), "another contract"),
    (lambda: _typed(), dict(name="Other"), "eip712Domain()"),
    (lambda: _typed(), dict(market_code="0x6080"), "pinned market"),
    (lambda: _typed(), dict(collection_market="0x" + "b2" * 20), "MARKET()"),
    # the types
    (lambda: _mutate(("types", "Extra"), [{"name": "x", "type": "uint256"}]), {}, "not exactly"),
    (lambda: _mutate(("types", "Listing"), O.LISTING_FIELDS[:-1]), {}, "not exactly the market"),
    (lambda: _mutate(("types", "Listing"), list(reversed(O.LISTING_FIELDS))), {}, "not exactly the market"),
    (lambda: _mutate(("primaryType",), "Permit"), {}, "not the market's Listing"),
    (lambda: _mutate(("primaryType",), "Offer"), {}, "an Offer is not signed"),
    (lambda: _mutate(("types", "OrderComponents"), []), {}, "Seaport 1.6 order"),
    (lambda: _mutate(("extra",), 1), {}, "not {types"),
    # the message
    (lambda: _mutate(("message", "extra"), 1), {}, "eleven fields"),
    (lambda: _mutate(("message", "price"), True), {}, "bool"),
    (lambda: _typed(_listing(seller="0x" + "5e" * 20)), {}, "not this treasury"),
    (lambda: _typed(_listing(collection="0x" + "67" * 20)), {}, "not a pinned collection"),
    (lambda: _typed(), dict(owner="0x" + "5e" * 20), "does not own"),
    (lambda: _typed(), dict(holder_code=CLONE), "SellerIsAccount"),
    (lambda: _typed(), dict(tc=2), "transferCount"),
    (lambda: _typed(), dict(nonce=1), "nonce"),
    (lambda: _typed(), dict(salt_used=True), "salt"),
    (lambda: _typed(_listing(price=0, maxRoyalty=0)), {}, "not above 0"),
    (lambda: _typed(_listing(currency="0x" + "77" * 20)), {}, "neither native nor a reviewed"),
    (lambda: _typed(_listing(maxRoyalty=10 ** 15)), {}, "maxRoyalty"),
    (lambda: _typed(_listing(expiry=NOW + 60)), {}, "less than"),
    (lambda: _typed(_listing(expiry=NOW + 31 * 86_400)), {}, "more than 30 days"),
    (lambda: _typed(_listing(promiseHash=S.conditions_hash([]))), {}, "promiseHash"),
    (lambda: _typed(), dict(eth=7), "promiseHash"),        # the account changed since
    (lambda: _typed(), dict(digest_off=True), "disagree"),
]


@pytest.mark.parametrize("make, rpc_kw, needle", NEAR_MISSES)
def test_every_near_miss_is_class_d(make, rpc_kw, needle):
    v = _classify(make(), MarketRpc(**rpc_kw))
    assert v.cls == D and needle in v.reason, v.reason


def test_an_unpinned_market_is_class_d(monkeypatch):
    monkeypatch.setattr(collection_registry, "profiles", lambda: ())
    v = _classify()
    assert v.cls == D and "not a pinned collection" in v.reason, v.reason


def test_an_untrusted_registry_is_class_d(monkeypatch):
    def boom():
        raise collection_registry.CollectionRegistryError("writable")
    monkeypatch.setattr(collection_registry, "profiles", boom)
    v = _classify()
    assert v.cls == D and "cannot be trusted" in v.reason, v.reason


def test_a_failed_read_is_class_d():
    class Broken(MarketRpc):
        def __call__(self, method, params):
            if method == "eth_getBlockByNumber":
                raise ConnectionError("rpc down")
            return super().__call__(method, params)
    v = _classify(rpc=Broken())
    assert v.cls == D and "could not be read" in v.reason, v.reason


def test_typed_data_without_facts_stays_d_on_every_rail():
    """The login rail: ``classify`` with no facts, and ``sign_classified``, never sign it."""
    v = classify(SignRequest("eip712", _typed()), SignContext(holder=HOLDER))
    assert v.cls == D and "class O" in v.reason
    with pytest.raises(SignRefused):
        sign_classified(LocalEoaSigner(bytes.fromhex(KEY[2:])), SignRequest("eip712", _typed()))


def test_facts_of_one_listing_do_not_bind_another():
    facts = _facts(_typed())
    other = _typed(_listing(price=2 * 10 ** 16, maxRoyalty=0))
    v = classify(SignRequest("eip712", other), SignContext(holder=HOLDER, order_facts=facts))
    assert v.cls == D and "disagree" in v.reason, v.reason


# ------------------------------------------------------------------------------ signing

def test_sign_market_order_signs_class_o_and_recovers_over_the_markets_digest():
    signer = LocalEoaSigner(bytes.fromhex(KEY[2:]))
    typed = _typed()
    sig, v = sign_market_order(signer, SignRequest("eip712", typed), facts=_facts(typed),
                               chain=CHAIN)
    assert v.cls == CLASS_O
    who = Account._recover_hash(bytes.fromhex(v.digest[2:]), signature=sig)
    assert who.lower() == HOLDER
    # the same bytes eth_account signs for the desk's eth_signTypedData_v4 form
    from eth_account.messages import encode_typed_data
    full = copy.deepcopy(typed)
    assert Account.sign_message(encode_typed_data(full_message=full), KEY).signature.hex() \
        .removeprefix("0x") == sig.removeprefix("0x")


def test_sign_market_order_refuses_class_d_before_the_key_is_used():
    class Spy(LocalEoaSigner):
        used = False

        def sign_typed_data(self, *a):
            Spy.used = True
            return super().sign_typed_data(*a)
    signer = Spy(bytes.fromhex(KEY[2:]))
    typed = _typed(_listing(seller="0x" + "5e" * 20))
    with pytest.raises(SignRefused) as exc:
        sign_market_order(signer, SignRequest("eip712", typed), facts=_facts(typed), chain=CHAIN)
    assert exc.value.verdict.cls == D and Spy.used is False


def test_a_signature_that_does_not_recover_is_refused():
    class Liar(LocalEoaSigner):
        def sign_typed_data(self, *a):
            return "0x" + "11" * 65
    typed = _typed()
    with pytest.raises(SignRefused) as exc:
        sign_market_order(Liar(bytes.fromhex(KEY[2:])), SignRequest("eip712", typed),
                          facts=_facts(typed), chain=CHAIN)
    assert "does not recover" in str(exc.value)


def test_the_share_payload_is_the_desks_v1_form():
    l = _listing()
    p = O.listing_payload(chain_id=CHAIN_ID, market=MARKET, listing=l, sig="0x" + "ab" * 65,
                          promise=_promise())
    assert set(p) == {"v", "k", "chainId", "market", "collection", "listing", "sig", "promise"}
    assert p["v"] == 1 and p["k"] == "m" and p["listing"]["price"] == l.price
    back = S.decode_link("https://x.example/buy.html#x=" + S.encode_link(p))
    assert S.Listing.of(back["listing"]) == l
    assert S.conditions_hash([S.Condition.of(c) for c in back["promise"]]) == l.promiseHash
