"""The ONE buyer-conditions builder of the checked sale, and the market's buyer calls (proposal
111 §8.2, §8.1 ``buy`` / ``claim_owed``; 115 W9).

A buyer without conditions buys an account a seller can empty (R-3). The market reads every
condition on the sold token's account AFTER the transfer, in the same transaction, and a failed
one reverts the whole fill (``ConditionFailed(i)``). Core builds that list here and nowhere else;
the verb (``tools/agent_nft/sale_buy.py``) and the guard (``core/wallet/tx_guard_sale.py``) both
call :func:`build` on a fresh :func:`read_snapshot`, and the guard accepts a buy only when the
calldata's list IS that list (less only holdings whose value is not checked anyway, see
:func:`admissible`).

Pure: this module imports only leaf modules (``abi``, ``custom_errors``) — never the guard, the
registry or the simulation — so it stays out of the guard's import cycle. Every read takes an
``rpc(method, params)`` callable; every address it needs (the collection, the account, the
PositionManager, the reviewed tokens) is an argument.

The contract of record (the first deployment, polyrob-desk ``contracts/src/sale``):

* ``Condition = (uint8 kind, address target, uint256 id, uint256 min, bytes data)``
  (``Conditions.sol:7-13``); the kinds are ``Conditions.sol:26-37`` (:data:`KINDS`).
  ``ETH``: ``a.balance >= min``; ``ERC20``: ``balanceOf(a) >= min``; ``ERC721``: ``ownerOf(id)
  == a`` and ``getApproved(id) == 0``; ``ERC1155`` / ``ERC6909``: ``balanceOf(a, id) >= min``;
  ``MINT_POSITION``: the collection's ``positionOf(soldId)`` on the PositionManager ``target``:
  owned by ``a``, not approved, no subscriber, liquidity ``>= min``; ``V4POSITION``: the same for
  position ``id`` plus the pool hook named in ``data``; ``IDENTITY``: ``hasIdentity(soldId)``,
  ``agentIdOf`` owned by ``a``, not approved, ``getAgentWallet == a`` (``Conditions.sol:93-188``).
* ``PolyrobMarket.buy(Listing, bytes sig, Condition[] sellerPromise, Condition[] buyerConditions,
  address recipient) payable`` (``PolyrobMarket.sol:155-174``): ``msg.value == price`` for an
  ETH listing; the transfer seller → recipient on a one-use ticket; the promise and the buyer's
  conditions on the account; then the royalty ``min(ERC-2981, maxRoyalty, price)`` and the seller
  are paid (a refused ETH payment is booked in ``owed``); ``Sold(collection, id, seller, buyer,
  currency, price, royalty)``.
* the EIP-712 ``Listing`` (``PolyrobMarket.sol:36-57``); the domain is read from the pinned
  market's ``eip712Domain()`` (ERC-5267), never written here.
* ``claim(address who)`` pays ``owed[who]`` to ``who`` for any caller (``:264-270``), no event.

The default list is the desk reader's (``web/checked-sale.js`` ``defaults``): the mint position,
the identity, ETH, then one condition per holding in the snapshot's order; a holding not on the
reviewed list is checked by amount and labeled "value not checked". The shared vectors are
``tests/unit/core/wallet/fixtures/sale_conditions_vectors.json`` (both readers, one fixture).
"""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import core.wallet.abi as abi   # by its module name: no edge to the core.wallet package

ZERO = "0x" + "0" * 40

# -- the condition kinds (Conditions.sol:26-37) -------------------------------------------------
ETH, ERC20, ERC721, ERC1155, MINT_POSITION, V4POSITION, ERC6909, IDENTITY = 0, 1, 2, 3, 4, 5, 6, 7
STATE, CALLEQ, CALLGE, CALLLE = 8, 9, 10, 11
KINDS = {"ETH": ETH, "ERC20": ERC20, "ERC721": ERC721, "ERC1155": ERC1155,
         "MINT_POSITION": MINT_POSITION, "V4POSITION": V4POSITION, "ERC6909": ERC6909,
         "IDENTITY": IDENTITY, "STATE": STATE, "CALLEQ": CALLEQ, "CALLGE": CALLGE,
         "CALLLE": CALLLE}
#: The kinds an any-id offer may carry (they name no fact of one token).
ANY_ID_KINDS = (ETH, ERC20, ERC1155, ERC6909, MINT_POSITION, IDENTITY)

#: The buyer's tolerance t: 0 for offers and an idle account, 50 bps for an active agent's.
TOLERANCE_BPS = (0, 50)
#: Most conditions in one fill: each read is a STATICCALL capped at 60,000 gas after a full-cap
#: guard (``Conditions.sol:22-23``), so 48 stay under ~3.3M gas. Known holdings first.
MAX_CONDITIONS = 48
#: The simulate-and-drop loop: at most this many unreviewed holdings dropped per quote.
MAX_DROPS = 8
#: ``eth_getLogs`` windows of the holdings scan (halved on an error, down to the floor).
LOG_WINDOW = 500_000
LOG_WINDOW_FLOOR = 2_000
LOG_MAX_CALLS = 256

UNREADABLE_WHY = "unreadable: it cannot be checked, count it as worth 0"
DROPPED_WHY = ("it gave the sale contract another answer than this read, so it cannot be "
               "checked: count it as worth 0")
CAPPED_WHY = (f"left out: at most {MAX_CONDITIONS} conditions fit one fill (known holdings "
              f"first) — not checked, count it as worth 0")
VALUE_NOT_CHECKED = "not a token we know: its value is not checked"

# -- selectors and topics (literals; tests/unit/core/wallet/test_sale_conditions.py computes each)
BUY_SIGNATURE = ("buy((address,uint256,uint256,address,address,uint256,uint256,uint256,uint256,"
                 "bytes32,uint256),bytes,(uint8,address,uint256,uint256,bytes)[],"
                 "(uint8,address,uint256,uint256,bytes)[],address)")
BUY_SELECTOR = "0xe9c3b815"
#: checkedFill(AdvancedOrder, CriteriaResolver[], bytes32, Fill, Condition[])
CHECKED_FILL_SELECTOR = "0xec48f34c"
ACCEPT_SELECTOR = "0x8e431f0c"         # accept(Offer,bytes,uint256,Condition[],Condition[])
CLAIM_SELECTOR = "0x1e83409a"          # claim(address)
OWED_SELECTOR = "0xdf18e047"           # owed(address)
LISTING_DIGEST_SELECTOR = "0x200beaeb"  # listingDigest(Listing)
EIP712_DOMAIN_SELECTOR = "0x84b0196e"  # eip712Domain() (ERC-5267)
TRANSFER_COUNT_SELECTOR = "0xec02ef57"  # transferCount(uint256)
POSITION_OF_SELECTOR = "0x46f96023"    # positionOf(uint256)
HAS_IDENTITY_SELECTOR = "0x83aa3bcc"   # hasIdentity(uint256)
AGENT_ID_OF_SELECTOR = "0x540a90d0"    # agentIdOf(uint256)
IDENTITY_REGISTRY_SELECTOR = "0x134e18f4"  # identityRegistry()
OWNER_OF_SELECTOR = "0x6352211e"       # ownerOf(uint256)
GET_APPROVED_SELECTOR = "0x081812fc"   # getApproved(uint256)
GET_AGENT_WALLET_SELECTOR = "0x00339509"  # getAgentWallet(uint256)
BALANCE_OF_SELECTOR = "0x70a08231"     # balanceOf(address)
BALANCE_OF_ID_SELECTOR = "0x00fdd58e"  # balanceOf(address,uint256) — ERC-1155 and ERC-6909
DECIMALS_SELECTOR = "0x313ce567"       # decimals()
SYMBOL_SELECTOR = "0x95d89b41"         # symbol()
POSITION_LIQUIDITY_SELECTOR = "0x1efeed33"  # getPositionLiquidity(uint256)
POOL_AND_POSITION_SELECTOR = "0x7ba03aad"   # getPoolAndPositionInfo(uint256)
CONDITION_FAILED_SELECTOR = "0x72fc940b"    # ConditionFailed(uint256)

TOPIC_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
TOPIC_TRANSFER_SINGLE = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"
TOPIC_TRANSFER_BATCH = "0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb"
TOPIC_TRANSFER_6909 = "0x1b3d7edb2e9c0b0e7c525b20aaaef0f5940d2ed71663c7d39266ecafac728859"
TOPIC_SOLD = "0x114ac8ea6e195fce183853d5744958b4900cb955d588f0a38b04cdf80f56f746"
TOPIC_CHECKED_FILL = "0x611cadbea8a33140e27d884538ffce24107f06d46a036a34e25d491e0734409c"
TOPIC_OWED = "0x7393c924ac30f9256e6cebcfb78841b1e7ef11bf8b09d87c2257b32b8063c979"

#: EIP-712 ``Listing`` (PolyrobMarket.sol:36-39) and its typehash.
LISTING_TYPE = ("Listing(address collection,uint256 id,uint256 transferCount,address seller,"
                "address currency,uint256 price,uint256 maxRoyalty,uint256 expiry,uint256 salt,"
                "bytes32 promiseHash,uint256 nonce)")
LISTING_TYPEHASH = "0x0bdecbab38ed00bb9b2096f45dcdc681642c147e28d9782c9fd046a9b70e5d70"
EIP712_DOMAIN_TYPE = ("EIP712Domain(string name,string version,uint256 chainId,"
                      "address verifyingContract)")

_COND_COMPONENTS = [{"type": "uint8"}, {"type": "address"}, {"type": "uint256"},
                    {"type": "uint256"}, {"type": "bytes"}]
_CONDS = {"type": "tuple[]", "components": _COND_COMPONENTS}
_LISTING_FIELDS = ("collection", "id", "transferCount", "seller", "currency", "price",
                   "maxRoyalty", "expiry", "salt", "promiseHash", "nonce")
_LISTING_TYPES = ("address", "uint256", "uint256", "address", "address", "uint256", "uint256",
                  "uint256", "uint256", "bytes32", "uint256")
_LISTING = {"type": "tuple", "components": [{"type": t} for t in _LISTING_TYPES]}
_BUY_INPUTS = [_LISTING, {"type": "bytes"}, _CONDS, _CONDS, {"type": "address"}]

Rpc = Callable[[str, list], Any]


def _low(a) -> str:
    return str(a or "").lower()


def _hex(data) -> str:
    text = _low(data)
    if not text.startswith("0x"):
        text = "0x" + text
    body = text[2:]
    if len(body) % 2 or any(c not in "0123456789abcdef" for c in body):
        raise ValueError(f"not hex bytes: {data!r}")
    return text


def keccak_hex(data: bytes) -> str:
    from eth_utils import keccak
    return "0x" + keccak(data).hex()


# ---------------------------------------------------------------------------------------------
# Condition and Listing

@dataclass(frozen=True)
class Condition:
    """One ``Condition`` (Conditions.sol:7-13); addresses and ``data`` lowercase 0x hex."""
    kind: int
    target: str
    id: int
    min: int
    data: str = "0x"

    @classmethod
    def of(cls, raw) -> "Condition":
        """From a dict (``{kind, target, id, min, data}``, numbers as ints or decimal strings, as
        the desk's links carry them) or a decoded tuple."""
        if isinstance(raw, Condition):
            return raw
        if isinstance(raw, dict):
            kind, target, cid, low, data = (raw["kind"], raw["target"], raw.get("id", 0),
                                            raw.get("min", 0), raw.get("data", "0x"))
        else:
            kind, target, cid, low, data = raw
        out = cls(int(kind), _low(target), int(cid), int(low), _hex(data or "0x"))
        if not 0 <= out.kind < 256 or out.id < 0 or out.min < 0:
            raise ValueError(f"not a Condition: {raw!r}")
        return out

    def as_tuple(self):
        return (self.kind, self.target, self.id, self.min, self.data)

    def as_json(self) -> Dict[str, Any]:
        return {"kind": self.kind, "target": self.target, "id": str(self.id),
                "min": str(self.min), "data": self.data}


def encode_conditions(conds: Sequence[Condition]) -> str:
    """``abi.encode(Condition[])`` — the preimage of ``promiseHash`` / ``conditionsHash``."""
    return "0x" + abi.encode([_CONDS], [[Condition.of(c).as_tuple() for c in conds]]).hex()


def conditions_hash(conds: Sequence[Condition]) -> str:
    return keccak_hex(bytes.fromhex(encode_conditions(conds)[2:]))


@dataclass(frozen=True)
class Listing:
    """The EIP-712 ``Listing`` (PolyrobMarket.sol:45-57); addresses lowercase."""
    collection: str
    id: int
    transferCount: int
    seller: str
    currency: str
    price: int
    maxRoyalty: int
    expiry: int
    salt: int
    promiseHash: str
    nonce: int

    @classmethod
    def of(cls, raw) -> "Listing":
        if isinstance(raw, Listing):
            return raw
        vals = [raw[k] for k in _LISTING_FIELDS] if isinstance(raw, dict) else list(raw)
        if len(vals) != len(_LISTING_FIELDS):
            raise ValueError("a Listing has 11 fields")
        out = {}
        for name, kind, v in zip(_LISTING_FIELDS, _LISTING_TYPES, vals):
            if kind == "address":
                text = _low(v)
                if len(text) != 42 or not text.startswith("0x"):
                    raise ValueError(f"listing.{name} is not an address: {v!r}")
                int(text, 16)
                out[name] = text
            elif kind == "bytes32":
                text = _hex(v)
                if len(text) != 66:
                    raise ValueError(f"listing.{name} is not 32 bytes: {v!r}")
                out[name] = text
            else:
                n = int(v)
                if not 0 <= n < 1 << 256:
                    raise ValueError(f"listing.{name} is out of range: {v!r}")
                out[name] = n
        return cls(**out)

    def as_tuple(self):
        return tuple(getattr(self, k) for k in _LISTING_FIELDS)


def listing_struct_hash(listing: Listing) -> str:
    l = Listing.of(listing)
    body = abi.encode([{"type": "bytes32"}] + [{"type": t} for t in _LISTING_TYPES],
                      [LISTING_TYPEHASH] + list(l.as_tuple()))
    return keccak_hex(body)


def domain_separator(name: str, version: str, chain_id: int, verifying_contract: str) -> str:
    from eth_utils import keccak
    body = abi.encode([{"type": "bytes32"}] * 3 + [{"type": "uint256"}, {"type": "address"}],
                      ["0x" + keccak(text=EIP712_DOMAIN_TYPE).hex(),
                       "0x" + keccak(text=str(name)).hex(), "0x" + keccak(text=str(version)).hex(),
                       int(chain_id), verifying_contract])
    return keccak_hex(body)


def listing_digest(listing: Listing, *, name: str, version: str, chain_id: int,
                   market: str) -> str:
    """The EIP-712 digest the seller signs (``_hashTypedDataV4``, PolyrobMarket.sol:436-455)."""
    ds = domain_separator(name, version, chain_id, market)
    return keccak_hex(b"\x19\x01" + bytes.fromhex(ds[2:])
                      + bytes.fromhex(listing_struct_hash(listing)[2:]))


def encode_buy(listing: Listing, sig: str, promise: Sequence[Condition],
               conditions: Sequence[Condition], recipient: str) -> str:
    return BUY_SELECTOR + abi.encode(_BUY_INPUTS, [
        Listing.of(listing).as_tuple(), _hex(sig), [Condition.of(c).as_tuple() for c in promise],
        [Condition.of(c).as_tuple() for c in conditions], recipient]).hex()


@dataclass(frozen=True)
class BuyCall:
    listing: Listing
    sig: str
    promise: Tuple[Condition, ...]
    conditions: Tuple[Condition, ...]
    recipient: str


def decode_buy(data: str) -> BuyCall:
    """The arguments of ``buy`` calldata. Raises ValueError unless *data* is EXACTLY their
    canonical encoding (a re-encode equals it byte for byte), with at most
    :data:`MAX_CONDITIONS` buyer conditions and as many promise entries."""
    text = _low(data)
    if not text.startswith(BUY_SELECTOR):
        raise ValueError(f"the calldata ({text[:10] or 'none'}) is not buy(Listing, …)")
    body = "0x" + text[10:]
    try:
        listing, sig, promise, conds, recipient = abi.decode(_BUY_INPUTS, body)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"the buy calldata does not decode ({exc})") from None
    if len(conds) > MAX_CONDITIONS or len(promise) > MAX_CONDITIONS:
        raise ValueError(f"the buy carries {len(conds)} conditions and {len(promise)} promise "
                         f"entries; at most {MAX_CONDITIONS} each")
    call = BuyCall(Listing.of(listing), _hex(sig), tuple(Condition.of(c) for c in promise),
                   tuple(Condition.of(c) for c in conds), _low(recipient))
    if encode_buy(call.listing, call.sig, call.promise, call.conditions, call.recipient) != text:
        raise ValueError("the buy calldata is not the canonical encoding of its arguments")
    return call


def encode_claim(who: str) -> str:
    return CLAIM_SELECTOR + "0" * 24 + _low(who).removeprefix("0x")


def decode_claim(data: str) -> str:
    text = _low(data)
    if not text.startswith(CLAIM_SELECTOR) or len(text) != 74 or int(text[10:34], 16):
        raise ValueError("the calldata is not the canonical claim(address) encoding")
    return "0x" + text[-40:]


def encode_owed(who: str) -> str:
    return OWED_SELECTOR + "0" * 24 + _low(who).removeprefix("0x")


def encode_owner_of(token_id: int) -> str:
    return OWNER_OF_SELECTOR + format(int(token_id), "064x")


def encode_transfer_count(token_id: int) -> str:
    return TRANSFER_COUNT_SELECTOR + format(int(token_id), "064x")


def encode_listing_digest(listing: Listing) -> str:
    return LISTING_DIGEST_SELECTOR + abi.encode([_LISTING], [Listing.of(listing).as_tuple()]).hex()


def failed_condition_index(revert_data) -> Optional[int]:
    """``i`` of a ``ConditionFailed(i)`` revert (promise first, then the buyer's), else None."""
    from core.wallet.custom_errors import revert_hex
    data = revert_hex(revert_data)
    if data[:10] != CONDITION_FAILED_SELECTOR or len(data) < 74:
        return None
    return int(data[10:74], 16)


# ---------------------------------------------------------------------------------------------
# The share link of a listing (the desk site's form, web/checked-sale.js encodeLink/decodeLink):
# JSON with every big number as a decimal string, base64url, after "#x=" in a URL — or the bare
# code, or the JSON itself. ``k``: "m" a market listing, "s" a Seaport listing, "o" an offer.

def decode_link(text: str) -> Dict[str, Any]:
    import re
    s = str(text or "").strip()
    if len(s) > 64_000:
        raise ValueError("the listing text is too long")
    m = re.search(r"[#&?]x=([A-Za-z0-9_-]+)", s)
    raw = m.group(1) if m else (s if re.fullmatch(r"[A-Za-z0-9_-]+", s) else None)
    try:
        if raw is not None:
            pad = "=" * (-len(raw) % 4)
            obj = json.loads(base64.urlsafe_b64decode(raw + pad).decode("utf-8"))
        else:
            obj = json.loads(s)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"the listing is unreadable ({type(exc).__name__})") from None
    if not isinstance(obj, dict) or obj.get("v") != 1 or obj.get("k") not in ("m", "s", "o"):
        raise ValueError("this is not a listing or offer of the checked sale (v 1, k m|s|o)")
    return obj


def encode_link(payload: Dict[str, Any]) -> str:
    def _plain(v):
        if isinstance(v, bool) or v is None or isinstance(v, str):
            return v
        if isinstance(v, int):
            return v if abs(v) < 1 << 53 else str(v)   # a JS number stays exact below 2^53
        if isinstance(v, dict):
            return {k: _plain(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [_plain(x) for x in v]
        return v
    raw = json.dumps(_plain(payload), separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


# ---------------------------------------------------------------------------------------------
# The builder (111 §8.2 steps 1-2 and 4): a snapshot in, (conditions, rows) out. Pure.
#
# snapshot = {account, positionManager, identityRegistry, eth, positionId,
#             position: {liquidity, hook, owner, approved} | None,
#             identity: {agentId, owner, wallet} | None,
#             holdings: [{kind: "erc20", token, amount, decimals, symbol} |
#                        {kind: "nft", token, id, held, symbol, position?} |
#                        {kind: "erc1155" | "erc6909", token, id, amount, symbol}],
#             coverage, why, block}
# (the desk's saleSnapshot shape; numbers are ints, None = unreadable).

def min_of(balance: int, bps: int) -> int:
    """``b - floor(b * t)``, and at least 1 when ``b >= 1`` (web/checked-sale.js minOf)."""
    b = int(balance or 0)
    m = b - (b * int(bps or 0)) // 10_000
    return 1 if b >= 1 and m < 1 else m


def _hook_data(hook) -> str:
    return "0x" + "0" * 24 + _low(hook or ZERO).removeprefix("0x").rjust(40, "0")


@dataclass
class Row:
    """One line of what the buyer gets: a holding, whether a condition checks it, and why."""
    label: str
    amount: Any
    checked: bool
    cond: Optional[int]
    why: str
    key: Optional[str] = None
    known: Optional[bool] = None
    bad: bool = False
    acted: bool = False
    min: Optional[int] = None
    token: Optional[str] = None
    decimals: Optional[int] = None


@dataclass
class Built:
    conditions: List[Condition] = field(default_factory=list)
    rows: List[Row] = field(default_factory=list)

    def droppable(self) -> Dict[int, Row]:
        """The conditions whose holding's value is not checked anyway (off the reviewed list):
        the only ones a failed simulation may drop (step 3)."""
        return {r.cond: r for r in self.rows
                if r.checked and r.cond is not None and r.known is False and r.key}


def build(snap: Dict[str, Any], *, tolerance_bps: int = 0, reviewed_erc20: Iterable[str] = (),
          reviewed_hooks: Iterable[str] = (ZERO,), exclude: Optional[Dict[str, str]] = None,
          any_id: bool = False, only_reviewed: bool = False, acted: Iterable[str] = (),
          max_conditions: int = MAX_CONDITIONS) -> Built:
    """The default buyer conditions of one account (web/checked-sale.js ``defaults``, the same
    list for the same snapshot), then the per-fill cap: over :data:`MAX_CONDITIONS`, the mint
    position, the identity, ETH and the reviewed holdings stay, the rest is labeled left out."""
    if int(tolerance_bps or 0) not in TOLERANCE_BPS:
        raise ValueError(f"the tolerance is one of {TOLERANCE_BPS} bps, not {tolerance_bps}")
    bps = int(tolerance_bps or 0)
    out = Built()
    reviewed = {_low(t) for t in reviewed_erc20}
    hooks = {_low(h) for h in reviewed_hooks}
    pm, reg = _low(snap.get("positionManager")), _low(snap.get("identityRegistry"))
    mint_pos = int(snap.get("positionId") or 0)
    account = _low(snap.get("account"))
    exclude = dict(exclude or {})
    acted_set = {_low(a) for a in acted}

    def add(c: Condition) -> Optional[int]:
        if any_id and c.kind not in ANY_ID_KINDS:
            return None
        out.conditions.append(c)
        return len(out.conditions) - 1

    def gone(owner) -> bool:
        return bool(account and owner and _low(owner) != account)

    pos = snap.get("position")
    if mint_pos and pos and (gone(pos.get("owner")) or (pos.get("approved")
                                                         and _low(pos.get("approved")) != ZERO)):
        out.rows.append(Row(f"Pool position #{mint_pos} (from the mint)",
                            f"liquidity {int(pos.get('liquidity') or 0)}", False, None,
                            "not in the account now" if gone(pos.get("owner"))
                            else "an address may move it now"))
    elif mint_pos and pos:
        i = add(Condition(MINT_POSITION, pm, 0, int(pos.get("liquidity") or 0)))
        out.rows.append(Row(f"Pool position #{mint_pos} (from the mint)",
                            f"liquidity {int(pos.get('liquidity') or 0)}", i is not None, i,
                            "" if i is None else "still in the account, no approval, no "
                                                 "subscriber, liquidity at least as now"))
    elif mint_pos:   # core only: the desk shows no row when the position is unreadable
        out.rows.append(Row(f"Pool position #{mint_pos} (from the mint)", None, False, None,
                            UNREADABLE_WHY, bad=True))
    ident = snap.get("identity")
    if ident and (gone(ident.get("owner")) or (ident.get("wallet") and gone(ident.get("wallet")))):
        out.rows.append(Row(f"Agent identity #{ident.get('agentId')} (ERC-8004)", "1", False,
                            None, "not in the account now" if gone(ident.get("owner"))
                            else "its wallet is not the account now"))
    elif ident:
        j = add(Condition(IDENTITY, reg, 0, 0))
        out.rows.append(Row(f"Agent identity #{ident.get('agentId')} (ERC-8004)", "1",
                            j is not None, j, "still owned by the account, no approval, its "
                                              "wallet is the account"))
    e = add(Condition(ETH, ZERO, 0, min_of(int(snap.get("eth") or 0), bps)))
    out.rows.append(Row("ETH", int(snap.get("eth") or 0), True, e, "at least the minimum",
                        decimals=18, min=out.conditions[e].min))

    def hold(key, row: Row, cond: Condition, known: bool, checked_why: str):
        row.key, row.known = key, known
        row.acted = row.token in acted_set
        if exclude.get(key):
            row.checked, row.bad, row.cond, row.why = False, True, None, exclude[key]
            out.rows.append(row)
            return
        if only_reviewed and not known:
            row.checked, row.cond, row.why = False, None, "not on the reviewed list"
            out.rows.append(row)
            return
        i = add(cond)
        if i is None:
            row.checked, row.cond, row.why = False, None, "an offer for any id cannot name it"
            out.rows.append(row)
            return
        row.checked, row.cond = True, i
        if cond.kind not in (V4POSITION, ERC721):
            row.min = out.conditions[i].min
        row.why = checked_why + ("" if known or "value is not checked" in checked_why
                                 else " · " + VALUE_NOT_CHECKED)
        out.rows.append(row)

    for h in snap.get("holdings") or ():
        t, kind = _low(h.get("token")), h.get("kind")
        if kind == "erc20":
            if h.get("amount") is None:
                out.rows.append(Row(h.get("symbol") or "?", None, False, None, UNREADABLE_WHY,
                                    key="20:" + t, known=False, bad=True, token=t))
                continue
            if int(h["amount"]) == 0:
                continue
            hold("20:" + t, Row(h.get("symbol") or "?", int(h["amount"]), False, None, "",
                                token=t, decimals=h.get("decimals")),
                 Condition(ERC20, t, 0, min_of(int(h["amount"]), bps)), t in reviewed,
                 "at least the minimum")
            continue
        if kind in ("erc1155", "erc6909"):
            tag = "1155:" if kind == "erc1155" else "6909:"
            hid = int(h.get("id") or 0)
            if h.get("amount") is None:
                out.rows.append(Row(f"{h.get('symbol') or '?'} #{hid}", None, False, None,
                                    UNREADABLE_WHY, key=f"{tag}{t}:{hid}", known=False, bad=True,
                                    token=t))
                continue
            if int(h["amount"]) == 0:
                continue
            name = h.get("symbol") or ("Item" if kind == "erc1155" else "Id")
            hold(f"{tag}{t}:{hid}", Row(f"{name} #{hid}", int(h["amount"]), False, None, "",
                                        token=t, decimals=0),
                 Condition(ERC1155 if kind == "erc1155" else ERC6909, t, hid,
                           min_of(int(h["amount"]), bps)), False, "at least the minimum")
            continue
        if h.get("held") is False:
            continue
        hid = int(h.get("id") or 0)
        if t == pm and mint_pos and hid == mint_pos:
            continue   # the MINT_POSITION row above
        if t == reg and ident and hid == int(ident.get("agentId") or 0):
            continue   # the IDENTITY row above
        hp = h.get("position")
        if t == pm and hp:
            hook_ok = _low(hp.get("hook") or ZERO) in hooks
            liq = int(hp.get("liquidity") or 0)
            hold(f"v4:{hid}", Row(f"Pool position #{hid}", f"liquidity {liq}", False, None, "",
                                  token=t),
                 Condition(V4POSITION, pm, hid, min_of(liq, bps), _hook_data(hp.get("hook"))),
                 hook_ok, "still in the account, no approval, no subscriber, the pool hook as "
                          "named" if hook_ok else
                 "still in the account, no approval, no subscriber, liquidity at least as now · "
                 "its pool has a hook we have not reviewed: its value is not checked")
            continue
        if h.get("held") is None:
            out.rows.append(Row(f"{h.get('symbol') or 'NFT'} #{hid}", "1", False, None,
                                UNREADABLE_WHY, key=f"721:{t}:{hid}", known=False, bad=True,
                                token=t))
            continue
        hold(f"721:{t}:{hid}", Row(f"{h.get('symbol') or 'NFT'} #{hid}", "1", False, None, "",
                                   token=t),
             Condition(ERC721, t, hid, 0), False, "still owned by the account, with no approval")
    _cap(out, max_conditions)
    return out


def _cap(out: Built, limit: int) -> None:
    """Over *limit*: keep the conditions of rows with no key (the mint position, the identity,
    ETH) and of reviewed holdings, then unreviewed ones in order; label the rest left out."""
    if len(out.conditions) <= limit:
        return
    keep_rows = [r for r in out.rows if r.checked and r.cond is not None]
    first = [r for r in keep_rows if r.key is None or r.known]
    rest = [r for r in keep_rows if not (r.key is None or r.known)]
    kept = (first + rest)[:limit]
    old = out.conditions
    order = sorted(kept, key=lambda r: r.cond)
    remap = {r.cond: n for n, r in enumerate(order)}
    out.conditions = [old[r.cond] for r in order]
    for r in keep_rows:
        if r.cond in remap:
            r.cond = remap[r.cond]
        else:
            r.checked, r.cond, r.min, r.why = False, None, None, CAPPED_WHY


def admissible(calldata: Sequence[Condition], built: Built,
               max_drops: int = MAX_DROPS) -> Optional[str]:
    """None when *calldata* is *built*'s list with at most *max_drops* DROPPABLE conditions
    removed (order kept), else why not. A dropped condition is one whose holding's value is
    not checked anyway (step 3: a token that answered the sale contract otherwise)."""
    full, droppable = built.conditions, built.droppable()
    cd = [Condition.of(c) for c in calldata]
    j = dropped = 0
    for i, c in enumerate(full):
        if j < len(cd) and cd[j] == c:
            j += 1
        elif i in droppable:
            dropped += 1
        else:
            return (f"condition {i} of the fresh read ({describe(c)}) is missing or changed in "
                    f"the transaction")
    if j != len(cd):
        return (f"the transaction carries {len(cd) - j} condition(s) the fresh read does not "
                f"build (from {describe(cd[j])})")
    if dropped > max_drops:
        return f"{dropped} conditions were dropped; at most {max_drops}"
    if not cd:
        return "the transaction carries no buyer condition (NoBuyerConditions)"
    return None


def describe(c: Condition) -> str:
    c = Condition.of(c)
    short = (c.target[:6] + "…" + c.target[-4:]) if len(c.target) == 42 else c.target
    return {
        ETH: f"the account holds at least {c.min} wei of ETH",
        ERC20: f"at least {c.min} of token {short}",
        ERC721: f"owns NFT #{c.id} of {short}, no approval",
        ERC1155: f"at least {c.min} of item #{c.id} of {short}",
        ERC6909: f"at least {c.min} of id #{c.id} at {short}",
        MINT_POSITION: f"the mint position on {short}, liquidity at least {c.min}",
        V4POSITION: f"pool position #{c.id} on {short}, liquidity at least {c.min}",
        IDENTITY: f"the agent identity on {short}, its wallet the account",
    }.get(c.kind, f"kind {c.kind} on {short}")


def card_lines(snap: Dict[str, Any], built: Built, *, limit: int = 24) -> List[str]:
    """What the buyer gets, one line per row: checked (with the minimum), not checked (why),
    and the coverage of the scan. Every gap is named; nothing unread is shown as empty."""
    lines = []
    for r in built.rows[:limit]:
        amount = "unreadable" if r.amount is None else str(r.amount)
        state = ("CHECKED" + (f" ≥ {r.min}" if r.min is not None else "")
                 + (" (value not checked)" if r.known is False else "")) if r.checked else \
            "NOT CHECKED (worth 0)"
        lines.append(f"{r.label}: {amount}{' · ' + r.token if r.token else ''} — {state}"
                     + (f" — {r.why}" if r.why and not r.checked else ""))
    if len(built.rows) > limit:
        lines.append(f"… +{len(built.rows) - limit} more row(s)")
    cov = snap.get("coverage")
    lines.append(("read every block since the collection's deploy block" if cov == "complete"
                  else f"NOT every block was read ({snap.get('why') or 'unknown'}): holdings may "
                       f"be missing — anything missing is not checked, count it as worth 0")
                 + f", at block {snap.get('block')}")
    return lines


# ---------------------------------------------------------------------------------------------
# The reader (111 §8.2 step 1): every holding of the account at ONE block.

@dataclass(frozen=True)
class ReadContext:
    """What a chain names for the reader: the v4 PositionManager (None = none pinned), the
    reviewed ERC-20s (always read, and their value is "known"), the reviewed pool hooks."""
    position_manager: Optional[str] = None
    reviewed_erc20: Tuple[str, ...] = ()
    reviewed_hooks: Tuple[str, ...] = (ZERO,)


def _word_of(raw, i: int = 0) -> int:
    if not isinstance(raw, str) or not raw.startswith("0x") or len(raw) < 2 + 64 * (i + 1):
        raise ValueError(f"unreadable word {i} of {str(raw)[:20]!r}")
    return int(raw[2 + 64 * i:2 + 64 * (i + 1)], 16)


def _addr_of(raw, i: int = 0) -> str:
    return "0x" + format(_word_of(raw, i), "064x")[-40:]


def _symbol(raw) -> str:
    """``symbol()`` as a string or a bytes32; '?' when it is neither."""
    try:
        text = str(raw or "")
        if len(text) >= 2 + 128 and _word_of(text, 0) == 32:
            n = _word_of(text, 1)
            if n <= 64:
                return bytes.fromhex(text[2 + 128:2 + 128 + 2 * n]).decode("utf-8", "replace")
        if len(text) == 66:
            return bytes.fromhex(text[2:]).rstrip(b"\x00").decode("utf-8", "replace") or "?"
    except Exception:  # noqa: BLE001
        pass
    return "?"


def _pad(addr: str) -> str:
    return "0" * 24 + _low(addr).removeprefix("0x")


def _uint_arg(n: int) -> str:
    return format(int(n), "064x")


def scan_logs(rpc: Rpc, flt: Dict[str, Any], start: int, end: int) -> Tuple[List[dict], bool, str]:
    """``eth_getLogs`` of *flt* over ``start..end`` in windows (halved on an error, down to
    :data:`LOG_WINDOW_FLOOR`; at most :data:`LOG_MAX_CALLS` calls). Returns ``(logs, complete,
    why)``: an unread range is NAMED, never read as "no logs"."""
    logs: List[dict] = []
    window, at, calls = LOG_WINDOW, int(start), 0
    while at <= end:
        if calls >= LOG_MAX_CALLS:
            return logs, False, f"stopped after {calls} log queries at block {at}"
        hi = min(end, at + window - 1)
        calls += 1
        try:
            got = rpc("eth_getLogs", [dict(flt, fromBlock=hex(at), toBlock=hex(hi))])
            if not isinstance(got, list):
                raise ValueError("no list")
        except Exception as exc:  # noqa: BLE001
            if window > LOG_WINDOW_FLOOR:
                window = max(LOG_WINDOW_FLOOR, window // 2)
                continue
            return logs, False, f"eth_getLogs {at}-{hi} failed ({type(exc).__name__})"
        logs.extend(got)
        at = hi + 1
    return logs, True, ""


def read_snapshot(rpc: Rpc, *, collection: str, token_id: int, account: str, deploy_block: int,
                  ctx: ReadContext, block: Optional[int] = None) -> Dict[str, Any]:
    """Every holding of *account* (the token's ERC-6551 account) at ONE block: its ETH; every
    ERC-20/721/1155/6909 that ever arrived (their transfer logs TO it since *deploy_block*) plus
    the reviewed ERC-20s, re-read live; the mint position (``positionOf``) and the identity
    (``hasIdentity`` → ``agentIdOf``) as the conditions read them. A failed read is ``None``
    (unreadable), never 0. The shape is the desk's ``saleSnapshot``."""
    blk = int(block if block is not None else int(rpc("eth_blockNumber", []), 16))
    tag = hex(blk)
    acct = _low(account)

    def call(to, data):
        return rpc("eth_call", [{"to": to, "data": data}, tag])

    snap: Dict[str, Any] = {"account": acct, "collection": _low(collection), "id": int(token_id),
                            "block": blk, "positionManager": _low(ctx.position_manager) or None,
                            "eth": int(rpc("eth_getBalance", [acct, tag]), 16)}
    # the identity registry is the COLLECTION's (where hasIdentity's agent ids live)
    try:
        has = _word_of(call(collection, HAS_IDENTITY_SELECTOR + _uint_arg(token_id))) == 1
    except Exception:  # noqa: BLE001 — no identity view: no identity condition
        has = False
    snap["identity"], snap["identityRegistry"] = None, None
    if has:
        reg = _addr_of(call(collection, IDENTITY_REGISTRY_SELECTOR))
        agent = _word_of(call(collection, AGENT_ID_OF_SELECTOR + _uint_arg(token_id)))
        snap["identityRegistry"] = reg
        ident: Dict[str, Any] = {"agentId": agent}
        for k, sel in (("owner", OWNER_OF_SELECTOR), ("approved", GET_APPROVED_SELECTOR),
                       ("wallet", GET_AGENT_WALLET_SELECTOR)):
            try:
                ident[k] = _addr_of(call(reg, sel + _uint_arg(agent)))
            except Exception:  # noqa: BLE001
                ident[k] = None
        snap["identity"] = ident
    try:
        pid = _word_of(call(collection, POSITION_OF_SELECTOR + _uint_arg(token_id)))
    except Exception:  # noqa: BLE001 — a collection without positionOf has no mint position
        pid = 0
    snap["positionId"] = pid
    pm = snap["positionManager"]
    snap["position"] = _position(call, pm, pid) if pid and pm else None
    # the holdings scan
    acct_t = "0x" + _pad(acct)
    end = blk
    a, ok_a, why_a = scan_logs(rpc, {"topics": [TOPIC_TRANSFER, None, acct_t]}, deploy_block, end)
    b, ok_b, why_b = scan_logs(rpc, {"topics": [[TOPIC_TRANSFER_SINGLE, TOPIC_TRANSFER_BATCH],
                                                None, None, acct_t]}, deploy_block, end)
    c, ok_c, why_c = scan_logs(rpc, {"topics": [TOPIC_TRANSFER_6909, None, acct_t]},
                               deploy_block, end)
    seen, cands = set(), []

    def cand(key, row):
        if key not in seen:
            seen.add(key)
            cands.append(row)
    for t in ctx.reviewed_erc20:
        cand("20:" + _low(t), {"kind": "erc20", "token": _low(t)})
    for lg in a:
        t, topics = _low(lg.get("address")), lg.get("topics") or []
        if len(topics) == 3:
            cand("20:" + t, {"kind": "erc20", "token": t})
        elif len(topics) == 4:
            i = int(topics[3], 16)
            cand(f"721:{t}:{i}", {"kind": "nft", "token": t, "id": i})
    for lg in b:
        t, data = _low(lg.get("address")), _low(lg.get("data")).removeprefix("0x")
        words = [int(data[k:k + 64], 16) for k in range(0, len(data) - 63, 64)]
        if (lg.get("topics") or [""])[0] == TOPIC_TRANSFER_SINGLE and words:
            cand(f"1155:{t}:{words[0]}", {"kind": "erc1155", "token": t, "id": words[0]})
        elif words:
            at = words[0] // 32
            n = words[at] if at < len(words) else 0
            for i in range(min(n, 256)):
                if at + 1 + i < len(words):
                    tid = words[at + 1 + i]
                    cand(f"1155:{t}:{tid}", {"kind": "erc1155", "token": t, "id": tid})
    for lg in c:
        topics = lg.get("topics") or []
        if len(topics) == 4:
            t, i = _low(lg.get("address")), int(topics[3], 16)
            cand(f"6909:{t}:{i}", {"kind": "erc6909", "token": t, "id": i})
    holdings = []
    for h in cands:
        try:
            h["symbol"] = _symbol(call(h["token"], SYMBOL_SELECTOR))
        except Exception:  # noqa: BLE001
            h["symbol"] = "?"
        if h["kind"] == "erc20":
            try:
                h["amount"] = _word_of(call(h["token"], BALANCE_OF_SELECTOR + _pad(acct)))
            except Exception:  # noqa: BLE001
                h["amount"] = None
            try:
                h["decimals"] = _word_of(call(h["token"], DECIMALS_SELECTOR))
            except Exception:  # noqa: BLE001
                h["decimals"] = 18
            if h["amount"] is None or h["amount"] > 0:
                holdings.append(h)
        elif h["kind"] in ("erc1155", "erc6909"):
            try:
                h["amount"] = _word_of(call(h["token"], BALANCE_OF_ID_SELECTOR + _pad(acct)
                                            + _uint_arg(h["id"])))
            except Exception:  # noqa: BLE001
                h["amount"] = None
            if h["amount"] is None or h["amount"] > 0:
                holdings.append(h)
        else:
            try:
                owner = _addr_of(call(h["token"], OWNER_OF_SELECTOR + _uint_arg(h["id"])))
                h["held"] = owner == acct
            except Exception:  # noqa: BLE001
                h["held"] = None
            if h["held"] is not False:
                if pm and h["token"] == pm and h["held"]:
                    h["position"] = _position(call, pm, h["id"])
                holdings.append(h)
    snap["holdings"] = holdings
    complete = ok_a and ok_b and ok_c
    snap["coverage"] = "complete" if complete else "incomplete"
    snap["why"] = why_a or why_b or why_c
    return snap


def _position(call, pm: str, pid: int) -> Optional[Dict[str, Any]]:
    """A v4 position as the conditions read it (owner, approved, liquidity, the pool hook =
    word 4 of ``getPoolAndPositionInfo``); None when unreadable."""
    try:
        info = call(pm, POOL_AND_POSITION_SELECTOR + _uint_arg(pid))
        return {"owner": _addr_of(call(pm, OWNER_OF_SELECTOR + _uint_arg(pid))),
                "approved": _addr_of(call(pm, GET_APPROVED_SELECTOR + _uint_arg(pid))),
                "liquidity": _word_of(call(pm, POSITION_LIQUIDITY_SELECTOR + _uint_arg(pid))),
                "hook": _addr_of(info, 4)}
    except Exception:  # noqa: BLE001
        return None


def read_context(chain: str) -> ReadContext:
    """The chain's PositionManager (the pinned v4 row) and reviewed tokens (the wrapped native,
    USDC and the pinned stables of the chain table). Lazy leaf imports only."""
    import core.wallet.chains as chains
    import core.wallet.dex_registry as dex_registry
    row = chains.get(chain)
    reviewed: List[str] = []
    for t in ((getattr(row, "wrapped_native", None), getattr(row, "usdc", None))
              + tuple(s[0] for s in (getattr(row, "stables", ()) or ()))):
        if t and str(t).startswith("0x") and _low(t) not in reviewed:
            reviewed.append(_low(t))
    pm = None
    try:
        v4 = dex_registry.row_for(chain, "v4")
        pm = _low(v4.position_manager) if v4 is not None and v4.position_manager else None
    except Exception:  # noqa: BLE001 — no pinned v4 row: no mint-position condition
        pm = None
    return ReadContext(position_manager=pm, reviewed_erc20=tuple(reviewed))


def read_and_build(rpc: Rpc, *, collection: str, token_id: int, account: str, deploy_block: int,
                   ctx: ReadContext, tolerance_bps: int = 0,
                   exclude: Optional[Dict[str, str]] = None,
                   block: Optional[int] = None) -> Tuple[Dict[str, Any], Built]:
    snap = read_snapshot(rpc, collection=collection, token_id=token_id, account=account,
                         deploy_block=deploy_block, ctx=ctx, block=block)
    return snap, build(snap, tolerance_bps=tolerance_bps, reviewed_erc20=ctx.reviewed_erc20,
                       reviewed_hooks=ctx.reviewed_hooks, exclude=exclude)


__all__ = ["ANY_ID_KINDS", "BuyCall", "Built", "Condition", "KINDS", "Listing", "ReadContext",
           "Row", "admissible", "build", "card_lines", "conditions_hash", "decode_buy",
           "decode_claim", "decode_link", "encode_buy", "encode_claim", "encode_conditions",
           "encode_link", "failed_condition_index", "listing_digest", "min_of", "read_and_build",
           "read_context", "read_snapshot", "scan_logs"]
