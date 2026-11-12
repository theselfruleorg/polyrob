"""The SELLER's orders on a pinned market of an ERC-6551P collection, and signing class O — a
pinned market order (proposal 111 §8.1 ``list`` (market) and ``accept``, §8.3; 115 W9a; owner
decision 111 D8 / 115 E5).

A signature is not a transaction: nothing simulates it, and whoever holds it can submit it
later. So the wallet signs ONE kind of order, and only after every field is bound to a fact
core reads itself:

* **class O — a market ``Listing``.** EIP-712 typed data whose types are EXACTLY the market's
  ``EIP712Domain`` and ``Listing`` (``PolyrobMarket.sol:36-57``), whose primary type is
  ``Listing``, and whose domain is EXACTLY the pinned market of a pinned collection: the
  ``name`` and ``version`` READ from the pinned code's ``eip712Domain()`` (ERC-5267), the chain
  id of the profile, ``verifyingContract`` = ``profile.erc6551p.market``, the market's and the
  collection's code re-checked against their pins, and ``collection.MARKET()`` = that market.
  Every field of the message is bound (:func:`classify_listing`): the collection is the pinned
  one; the seller is the signing treasury and OWNS the token now (``ownerOf``) and is no
  ERC-6551 account (``SellerIsAccount``); ``transferCount`` and ``nonce`` equal the live reads;
  the salt is unused; the price is above 0; the currency is native or a token on core's
  reviewed list (``sale_conditions.read_context``); ``maxRoyalty`` ≤ :data:`MAX_ROYALTY_BPS` of
  the price; the expiry is ahead and at most :data:`MAX_LISTING_TTL_SEC` away (chain time); the
  ``promiseHash`` is one core BUILDS from a fresh read of the account (``sale_conditions``, the
  seller's promise at t = 0 or 50 bps, reviewed holdings only — the desk's form), never one the
  request carries; and core's digest equals the market's own ``listingDigest``.
* **refused by name** — an ``Offer`` (the treasury's ERC-20 allowance to the market is ONE number
  per token for every open offer, and the holdings watch sends no transaction, so it cannot be
  tracked and reset per offer: :data:`NOT_BUILT_OFFER`); a Seaport order, zone or plain
  (:data:`NOT_BUILT_SEAPORT`); anything else is class D.

The SAME code runs in the agent process and in ``polyrob-signer`` (``sign.order``): each reads
its own facts (:func:`read_listing_facts`, its own RPC and the root-owned registry) and the
signer refuses a class it does not compute itself. The facts are never taken from the wire.

The seller's ``accept`` of a buyer's signed ``Offer`` is a TRANSACTION, not a signature: its
codec is here (:func:`decode_accept`), its guard is ``core/wallet/tx_guard_sale.py``.

Imports: only leaf modules by their full module name (``abi``, ``sale_conditions``,
``sale_acts``); the registry comes in as an argument. So this module stays out of the guard's
import cycle. Every selector, topic and typehash is a literal computed in
``tests/unit/core/wallet/test_sale_orders.py``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Sequence, Tuple

import core.wallet.abi as abi
import core.wallet.sale_acts as sale_acts
import core.wallet.sale_conditions as S

ZERO = S.ZERO

#: The longest a listing may stay valid (chain time). No owner setting fits this (an order is not
#: a spend, so no cap names it): a signed listing outlives a forgotten intent, so it is short.
MAX_LISTING_TTL_SEC = 30 * 86_400
#: A listing must still be valid this long after it is signed (a block or two of margin).
MIN_LISTING_TTL_SEC = 600
#: ``maxRoyalty`` at most this share of the price (the desk's own listings: 500 bps).
MAX_ROYALTY_BPS = 500
#: Tolerances a seller's promise may be built with (111 §8.2: 0 for an idle listing).
PROMISE_TOLERANCE_BPS = S.TOLERANCE_BPS

# -- selectors, topics, typehashes (PolyrobMarket.sol; computed in the test) -------------------
OFFER_TYPE = ("Offer(address collection,uint256 id,bool anyId,address buyer,address currency,"
              "uint256 price,uint256 maxRoyalty,uint256 expiry,uint256 salt,"
              "bytes32 conditionsHash,uint256 nonce)")
OFFER_TYPEHASH = "0x6c5f2097333ed985ecb37f579fcc07e74d6bc3a17c97686d580ff77695f32902"
ACCEPT_SIGNATURE = ("accept((address,uint256,bool,address,address,uint256,uint256,uint256,uint256,"
                    "bytes32,uint256),bytes,uint256,(uint8,address,uint256,uint256,bytes)[],"
                    "(uint8,address,uint256,uint256,bytes)[])")
ACCEPT_SELECTOR = S.ACCEPT_SELECTOR          # 0x8e431f0c
OFFER_DIGEST_SELECTOR = "0xb331ed7f"         # offerDigest(Offer)
MARKET_SELECTOR = "0xf46f16c2"               # MARKET() on the collection
APPROVE_SELECTOR = "0x095ea7b3"              # approve(address,uint256)
GET_APPROVED_SELECTOR = S.GET_APPROVED_SELECTOR
TOPIC_APPROVAL = "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925"

NOT_BUILT_OFFER = ("an Offer is not signed by this build: the treasury's ERC-20 allowance to the "
                   "market is one number per token for every open offer, and the holdings watch "
                   "sends no transaction, so the allowance cannot be tracked and reset per offer")
NOT_BUILT_SEAPORT = ("a Seaport 1.6 order (a zone listing or a plain listing) is not built: it "
                     "needs a Seaport fixture on the proof rig before the wallet signs one")

_DOMAIN_FIELDS = [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                  {"name": "chainId", "type": "uint256"},
                  {"name": "verifyingContract", "type": "address"}]
LISTING_FIELDS = [{"name": n, "type": t} for n, t in zip(S._LISTING_FIELDS, S._LISTING_TYPES)]
_OFFER_FIELDS = ("collection", "id", "anyId", "buyer", "currency", "price", "maxRoyalty", "expiry",
                 "salt", "conditionsHash", "nonce")
_OFFER_TYPES = ("address", "uint256", "bool", "address", "address", "uint256", "uint256", "uint256",
                "uint256", "bytes32", "uint256")
_OFFER = {"type": "tuple", "components": [{"type": t} for t in _OFFER_TYPES]}
_ACCEPT_INPUTS = [_OFFER, {"type": "bytes"}, {"type": "uint256"}, S._CONDS, S._CONDS]


def _low(a) -> str:
    return str(a or "").lower()


# ---------------------------------------------------------------------------------------------
# The Offer (a buyer's; the seller accepts it with a transaction)

@dataclass(frozen=True)
class Offer:
    """The EIP-712 ``Offer`` (PolyrobMarket.sol:59-71); addresses lowercase."""
    collection: str
    id: int
    anyId: bool
    buyer: str
    currency: str
    price: int
    maxRoyalty: int
    expiry: int
    salt: int
    conditionsHash: str
    nonce: int

    @classmethod
    def of(cls, raw) -> "Offer":
        if isinstance(raw, Offer):
            return raw
        vals = [raw[k] for k in _OFFER_FIELDS] if isinstance(raw, dict) else list(raw)
        if len(vals) != len(_OFFER_FIELDS):
            raise ValueError("an Offer has 11 fields")
        out: Dict[str, Any] = {}
        for name, kind, v in zip(_OFFER_FIELDS, _OFFER_TYPES, vals):
            if kind == "address":
                text = _low(v)
                if len(text) != 42 or not text.startswith("0x"):
                    raise ValueError(f"offer.{name} is not an address: {v!r}")
                int(text, 16)
                out[name] = text
            elif kind == "bytes32":
                text = S._hex(v)
                if len(text) != 66:
                    raise ValueError(f"offer.{name} is not 32 bytes: {v!r}")
                out[name] = text
            elif kind == "bool":
                if not isinstance(v, bool):
                    raise ValueError(f"offer.{name} is not a bool: {v!r}")
                out[name] = v
            else:
                n = int(v)
                if not 0 <= n < 1 << 256:
                    raise ValueError(f"offer.{name} is out of range: {v!r}")
                out[name] = n
        return cls(**out)

    def as_tuple(self):
        return tuple(getattr(self, k) for k in _OFFER_FIELDS)


def offer_struct_hash(offer: Offer) -> str:
    o = Offer.of(offer)
    body = abi.encode([{"type": "bytes32"}] + [{"type": t} for t in _OFFER_TYPES],
                      [OFFER_TYPEHASH] + list(o.as_tuple()))
    return S.keccak_hex(body)


def offer_digest(offer: Offer, *, name: str, version: str, chain_id: int, market: str) -> str:
    ds = S.domain_separator(name, version, chain_id, market)
    return S.keccak_hex(b"\x19\x01" + bytes.fromhex(ds[2:]) + bytes.fromhex(offer_struct_hash(offer)[2:]))


def encode_offer_digest(offer: Offer) -> str:
    return OFFER_DIGEST_SELECTOR + abi.encode([_OFFER], [Offer.of(offer).as_tuple()]).hex()


def encode_accept(offer: Offer, sig: str, token_id: int, conditions: Sequence, promise: Sequence) -> str:
    return ACCEPT_SELECTOR + abi.encode(_ACCEPT_INPUTS, [
        Offer.of(offer).as_tuple(), S._hex(sig), int(token_id),
        [S.Condition.of(c).as_tuple() for c in conditions],
        [S.Condition.of(c).as_tuple() for c in promise]]).hex()


@dataclass(frozen=True)
class AcceptCall:
    offer: Offer
    sig: str
    token_id: int
    conditions: Tuple[S.Condition, ...]
    promise: Tuple[S.Condition, ...]


def decode_accept(data: str) -> AcceptCall:
    """The arguments of ``accept`` calldata. Raises ValueError unless *data* is EXACTLY their
    canonical encoding, with at most :data:`sale_conditions.MAX_CONDITIONS` conditions."""
    text = _low(data)
    if not text.startswith(ACCEPT_SELECTOR):
        raise ValueError(f"the calldata ({text[:10] or 'none'}) is not accept(Offer, …)")
    try:
        offer, sig, tid, conds, promise = abi.decode(_ACCEPT_INPUTS, "0x" + text[10:])
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"the accept calldata does not decode ({exc})") from None
    if len(conds) > S.MAX_CONDITIONS or len(promise) > S.MAX_CONDITIONS:
        raise ValueError(f"the accept carries {len(conds)} conditions and {len(promise)} promise "
                         f"entries; at most {S.MAX_CONDITIONS} each")
    call = AcceptCall(Offer.of(offer), S._hex(sig), int(tid),
                      tuple(S.Condition.of(c) for c in conds),
                      tuple(S.Condition.of(c) for c in promise))
    if encode_accept(call.offer, call.sig, call.token_id, call.conditions, call.promise) != text:
        raise ValueError("the accept calldata is not the canonical encoding of its arguments")
    return call


def encode_approve(spender: str, token_id: int) -> str:
    return APPROVE_SELECTOR + "0" * 24 + _low(spender).removeprefix("0x") + format(int(token_id), "064x")


def encode_get_approved(token_id: int) -> str:
    return GET_APPROVED_SELECTOR + format(int(token_id), "064x")


def encode_balance_of(who: str) -> str:
    return S.BALANCE_OF_SELECTOR + "0" * 24 + _low(who).removeprefix("0x")


# ---------------------------------------------------------------------------------------------
# The listing's typed data (eth_signTypedData_v4 form) and its share link

def typed_listing(listing: S.Listing, *, name: str, version: str, chain_id: int,
                  market: str) -> Dict[str, Any]:
    """The EXACT typed data class O signs: the market's two types, nothing else."""
    l = S.Listing.of(listing)
    return {"types": {"EIP712Domain": [dict(f) for f in _DOMAIN_FIELDS],
                      "Listing": [dict(f) for f in LISTING_FIELDS]},
            "primaryType": "Listing",
            "domain": {"name": str(name), "version": str(version), "chainId": int(chain_id),
                       "verifyingContract": _low(market)},
            "message": dict(zip(S._LISTING_FIELDS, l.as_tuple()))}


def listing_payload(*, chain_id: int, market: str, listing: S.Listing, sig: str,
                    promise: Sequence) -> Dict[str, Any]:
    """The desk site's share payload v1 (``web/account-sell.js``): ``k: "m"``."""
    l = S.Listing.of(listing)
    msg = dict(zip(S._LISTING_FIELDS, l.as_tuple()))
    return {"v": 1, "k": "m", "chainId": int(chain_id), "market": _low(market),
            "collection": l.collection, "listing": msg, "sig": S._hex(sig),
            "promise": [S.Condition.of(c).as_json() for c in promise]}


class OrderRefused(ValueError):
    """The typed data is not a class-O order; the text is why."""


def _uint(v, what: str) -> int:
    if isinstance(v, bool):
        raise OrderRefused(f"{what} is a bool, not a number")
    if isinstance(v, str) and v.isdigit():
        v = int(v)
    if not isinstance(v, int) or not 0 <= v < 1 << 256:
        raise OrderRefused(f"{what} is not a uint256: {v!r}")
    return v


def parse_typed(payload) -> Tuple[Dict[str, Any], S.Listing]:
    """``(domain, listing)`` of a class-O payload, or :class:`OrderRefused` naming why not. The
    payload's shape must be EXACTLY the market's: four keys, two types in the market's order,
    primary type ``Listing``, the four domain fields, the eleven message fields."""
    if not isinstance(payload, dict) or set(payload) != {"types", "primaryType", "domain", "message"}:
        raise OrderRefused("the typed data is not {types, primaryType, domain, message}")
    types, primary = payload["types"], payload["primaryType"]
    if not isinstance(types, dict):
        raise OrderRefused("types is not an object")
    if primary == "Offer" or "Offer" in types:
        raise OrderRefused(NOT_BUILT_OFFER)
    if primary in ("OrderComponents", "BulkOrder") or "OrderComponents" in types:
        raise OrderRefused(NOT_BUILT_SEAPORT)
    if primary != "Listing":
        raise OrderRefused(f"the primary type is {str(primary)[:40]!r}, not the market's Listing")
    if set(types) != {"EIP712Domain", "Listing"}:
        raise OrderRefused(f"the types are {sorted(map(str, types))[:6]}, not exactly "
                           f"EIP712Domain and Listing")
    if types["EIP712Domain"] != _DOMAIN_FIELDS or types["Listing"] != LISTING_FIELDS:
        raise OrderRefused("the EIP712Domain or Listing type is not exactly the market's")
    domain, msg = payload["domain"], payload["message"]
    if not isinstance(domain, dict) or set(domain) != {"name", "version", "chainId",
                                                        "verifyingContract"}:
        raise OrderRefused("the domain is not exactly {name, version, chainId, verifyingContract}")
    if not isinstance(domain["name"], str) or not isinstance(domain["version"], str):
        raise OrderRefused("the domain name and version are not strings")
    dom = {"name": domain["name"], "version": domain["version"],
           "chainId": _uint(domain["chainId"], "domain.chainId"),
           "verifyingContract": _low(domain["verifyingContract"])}
    if len(dom["verifyingContract"]) != 42:
        raise OrderRefused("domain.verifyingContract is not an address")
    if not isinstance(msg, dict) or set(msg) != set(S._LISTING_FIELDS):
        raise OrderRefused("the message is not exactly the Listing's eleven fields")
    clean = {}
    for name, kind in zip(S._LISTING_FIELDS, S._LISTING_TYPES):
        v = msg[name]
        if kind == "uint256":
            clean[name] = _uint(v, f"listing.{name}")
        elif not isinstance(v, str):
            raise OrderRefused(f"listing.{name} is not a hex string")
        else:
            clean[name] = v
    try:
        listing = S.Listing.of(clean)
    except ValueError as exc:
        raise OrderRefused(str(exc)) from None
    return dom, listing


# ---------------------------------------------------------------------------------------------
# The facts (I/O) and the class-O decision (pure)

@dataclass(frozen=True)
class ListingFacts:
    """What core read itself for one listing. ``refusal`` set = a pin or a read failed (class D
    with that reason). Never built from the request."""
    chain_id: int
    market: str = ""
    collection: str = ""
    refusal: Optional[str] = None
    name: str = ""
    version: str = ""
    domain_chain_id: int = 0
    domain_verifying: str = ""
    collection_market: str = ""
    owner: str = ""
    transfer_count: int = -1
    nonce: int = -1
    salt_used: bool = True
    now: int = 0
    holder_is_account: bool = True
    sale_rule: bool = False
    reviewed: Tuple[str, ...] = ()
    promise_hashes: Tuple[str, ...] = ()
    market_digest: str = ""


def read_domain(rpc, market: str) -> Tuple[str, str, int, str]:
    """``(name, version, chainId, verifyingContract)`` of ``eip712Domain()`` (ERC-5267)."""
    _f, name, version, cid, verifying, _salt, _ext = abi.decode(
        [{"type": "bytes1"}, {"type": "string"}, {"type": "string"}, {"type": "uint256"},
         {"type": "address"}, {"type": "bytes32"}, {"type": "uint256[]"}],
        rpc("eth_call", [{"to": market, "data": S.EIP712_DOMAIN_SELECTOR}, "latest"]))
    return str(name), str(version), int(cid), _low(verifying)


def seller_promise(snap: Dict[str, Any], ctx: S.ReadContext, tolerance_bps: int) -> S.Built:
    """The seller's promise of one account (the desk's: reviewed holdings only, the mint position,
    the identity and ETH). ONE builder for the verb and the classifier."""
    return S.build(snap, tolerance_bps=tolerance_bps, reviewed_erc20=ctx.reviewed_erc20,
                   reviewed_hooks=ctx.reviewed_hooks, only_reviewed=True)


def _word(raw) -> int:
    return S._word_of(raw, 0)


def read_listing_facts(rpc, registry, *, chain: str, payload, holder: str) -> ListingFacts:
    """Every fact class O binds, read by THIS process from the chain and the root-owned registry
    (``core.wallet.collection_registry`` comes in as *registry*). A malformed payload raises
    :class:`OrderRefused`; a failed pin or read returns facts with ``refusal`` set."""
    _dom, l = parse_typed(payload)
    chain_id = int(registry.chain_id_of(chain))
    try:
        profile = next((p for p in registry.profiles_on(chain_id)
                        if p.address == l.collection and p.erc6551p is not None), None)
    except Exception as exc:  # noqa: BLE001 — an untrusted registry pins nothing
        return ListingFacts(chain_id, refusal=f"the collection registry cannot be trusted ({exc})")
    if profile is None:
        return ListingFacts(chain_id, refusal=(f"{l.collection} is not a pinned collection with a "
                                               f"pinned market on {chain}"))
    std = profile.erc6551p
    market = _low(std.market)
    base = dict(chain_id=chain_id, market=market, collection=profile.address)
    try:
        why = (registry.runtime_refusal(rpc, profile)
               or registry.code_pin_refusal(rpc, market, std.market_runtime_sha256,
                                            "pinned market"))
        if why:
            return ListingFacts(**base, refusal=why)
        name, version, dcid, verifying = read_domain(rpc, market)

        def call(to, data):
            return rpc("eth_call", [{"to": to, "data": data}, "latest"])
        block = rpc("eth_getBlockByNumber", ["latest", False]) or {}
        account, version_row = profile.account_of(int(l.id))
        ctx = S.read_context(chain)
        snap = S.read_snapshot(rpc, collection=profile.address, token_id=int(l.id),
                               account=account, deploy_block=profile.deploy_block, ctx=ctx)
        hashes = tuple(S.conditions_hash(seller_promise(snap, ctx, bps).conditions)
                       for bps in PROMISE_TOLERANCE_BPS)
        return ListingFacts(
            **base, name=name, version=version, domain_chain_id=dcid, domain_verifying=verifying,
            collection_market="0x" + format(_word(call(profile.address, MARKET_SELECTOR)), "064x")[-40:],
            owner="0x" + format(_word(call(profile.address, S.encode_owner_of(l.id))), "064x")[-40:],
            transfer_count=_word(call(profile.address, S.encode_transfer_count(l.id))),
            nonce=sale_acts.read_nonce(rpc, market, holder),
            salt_used=sale_acts.read_used_salt(rpc, market, holder, l.salt),
            now=int(block.get("timestamp"), 16),
            holder_is_account=sale_acts.is_erc6551_clone(rpc("eth_getCode", [holder, "latest"])),
            sale_rule=bool(getattr(version_row.kind, "sale_ready_view", None)),
            reviewed=tuple(_low(t) for t in ctx.reviewed_erc20), promise_hashes=hashes,
            market_digest="0x" + format(_word(call(market, S.encode_listing_digest(l))), "064x"))
    except Exception as exc:  # noqa: BLE001 — unread is never "fine"
        return ListingFacts(**base, refusal=f"the listing's facts could not be read ({exc})")


def classify_listing(payload, holder: str, facts: ListingFacts
                     ) -> Tuple[str, str, Dict[str, Any], str]:
    """``(cls, reason, decoded, digest)``: ``"O"`` when *payload* is a market Listing every field
    of which is bound to *facts*, else ``"D"`` (refused) with the first reason. Pure."""
    try:
        dom, l = parse_typed(payload)
    except OrderRefused as exc:
        return "D", str(exc), {"kind": "market_order"}, ""
    me = _low(holder)
    decoded = {"kind": "market_listing", "collection": l.collection, "id": l.id,
               "price": l.price, "currency": l.currency, "expiry": l.expiry, "salt": l.salt,
               "market": dom["verifyingContract"], "chain_id": dom["chainId"]}

    def no(reason):
        return "D", reason, decoded, ""
    if facts is None or facts.refusal:
        return no(getattr(facts, "refusal", None) or "no facts were read for this order")
    if l.collection != facts.collection:
        return no(f"the listing sells {l.collection}, not the pinned collection {facts.collection}")
    if dom["verifyingContract"] != facts.market:
        return no(f"the domain's verifyingContract {dom['verifyingContract']} is not the pinned "
                  f"market {facts.market} of {facts.collection}")
    if facts.domain_verifying != facts.market or facts.domain_chain_id != facts.chain_id:
        return no("the pinned market's eip712Domain() names another contract or chain")
    if dom["chainId"] != facts.chain_id:
        return no(f"the domain's chainId {dom['chainId']} is not the profile's {facts.chain_id}")
    if (dom["name"], dom["version"]) != (facts.name, facts.version):
        return no(f"the domain is ({dom['name']!r}, {dom['version']!r}); the pinned market's "
                  f"eip712Domain() is ({facts.name!r}, {facts.version!r})")
    if facts.collection_market != facts.market:
        return no(f"the collection's MARKET() is {facts.collection_market}, not the pinned market")
    if l.seller != me:
        return no(f"the seller is {l.seller}, not this treasury {me}")
    if facts.owner != me:
        return no(f"this treasury does not own #{l.id} now (its owner is {facts.owner})")
    if facts.holder_is_account:
        return no("the treasury is an ERC-6551 account (the market refuses: SellerIsAccount)")
    if not facts.sale_rule:
        return no(f"#{l.id} is held in an account kind with no sale rule")
    if l.transferCount != facts.transfer_count:
        return no(f"transferCount is {l.transferCount}; the live read is {facts.transfer_count}")
    if l.nonce != facts.nonce:
        return no(f"the nonce is {l.nonce}; the market's nonces(treasury) is {facts.nonce}")
    if facts.salt_used:
        return no(f"the salt {l.salt} is already used or cancelled")
    if not 0 < l.price < 1 << 128:
        return no(f"the price {l.price} is not above 0 (or is absurdly large)")
    if l.currency != ZERO and l.currency not in facts.reviewed:
        return no(f"the currency {l.currency} is neither native nor a reviewed token")
    if l.maxRoyalty > l.price * MAX_ROYALTY_BPS // 10_000:
        return no(f"maxRoyalty {l.maxRoyalty} is above {MAX_ROYALTY_BPS} bps of the price")
    if l.expiry < facts.now + MIN_LISTING_TTL_SEC:
        return no(f"the listing expires at {l.expiry}, less than {MIN_LISTING_TTL_SEC} s after "
                  f"the chain's now {facts.now}")
    if l.expiry > facts.now + MAX_LISTING_TTL_SEC:
        return no(f"the listing expires at {l.expiry}, more than {MAX_LISTING_TTL_SEC // 86_400} "
                  f"days after the chain's now {facts.now}")
    if l.promiseHash not in facts.promise_hashes:
        return no("the promiseHash is not one core builds from a fresh read of the account (the "
                  "account changed since the listing was built, or the promise was not core's)")
    digest = S.listing_digest(l, name=facts.name, version=facts.version,
                              chain_id=facts.chain_id, market=facts.market)
    if digest != facts.market_digest:
        return no("core and the pinned market disagree on the listing's EIP-712 digest")
    return "O", (f"a market listing of {l.collection} #{l.id} on its pinned market "
                 f"{facts.market}, every field bound"), decoded, digest


def recovers(digest: str, signature: str, address: str) -> bool:
    """The signature is over *digest* by *address* (an EOA's ECDSA)."""
    try:
        from eth_account import Account
        who = Account._recover_hash(bytes.fromhex(S._hex(digest)[2:]), signature=S._hex(signature))
        return _low(who) == _low(address)
    except Exception:  # noqa: BLE001 — an unverifiable signature is not returned
        return False


__all__ = ["AcceptCall", "ListingFacts", "MAX_LISTING_TTL_SEC", "MAX_ROYALTY_BPS",
           "NOT_BUILT_OFFER", "NOT_BUILT_SEAPORT", "Offer", "OrderRefused", "classify_listing",
           "decode_accept", "encode_accept", "encode_approve", "encode_get_approved",
           "encode_offer_digest", "listing_payload", "offer_digest", "parse_typed",
           "read_listing_facts", "recovers", "seller_promise", "typed_listing"]
