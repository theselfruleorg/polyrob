"""The collection paid-mint transaction shape — a mint through the pinned SALE of a collection
(069 v4; testnet-run 2026-09-29 finding F2; proposal 115 W8). A collection profile opts into it
with the ``mint`` capability AND its ``sale`` key (``core/wallet/collection_registry.py``); the
call shapes, the base price and the per-call cap are this module's.

Measurement helpers for ONE ``tx_guard`` shape (``TxIntent.is_collection_mint``), in the
``agent_nft_collection_reveal`` / ``deploy_guard`` pattern: this module DECIDES nothing on its own —
``tx_guard._authorize`` calls :func:`structural` before the simulation, :func:`runtime_refusal`
with the RPC pins and :func:`assert_simulation` after it, and returns the refusal they name.

Why a shape of its own. A paid mint sends native value AND receives ``qty`` ERC-721s. Every
existing branch refuses it: the NFT branch refuses any native movement (an NFT transfer is not
payable), and the native-send branch knows nothing of the tokens that must arrive.

The contract of record (polyrob-desk ``contracts/src/drop/PolyrobSale.sol``, ``src/Polyrob.sol``,
``src/kit/modules/AgentIdentity.sol``; SPEC §6.2, §6.7). The collection has NO public mint:
``Polyrob.mintFromSale`` accepts only its ``SALE()``. The sale has two buyer entry points:

* ``mint(address to, uint256 qty, uint256 minPnlOutPerToken)`` — the auction and, after the
  settlement, the base-price mint. Auction: ``qty × priceNow() <= msg.value <= qty ×
  START_PRICE`` and the sale KEEPS all of it inside the transaction (the excess above the
  clearing price is paid back to ``to`` later, by ``settle``; the division dust is BOOKED in
  ``owed`` by ``_owe``, never pushed). After ``settled()``: exactly ``qty × BASE``.
* ``mintWL(address to, uint256 qty, uint256 minPnlOutPerToken, uint256 allowance,
  bytes32[] proof)`` — the whitelist: exactly ``qty × BASE``; the contract bounds ``allowance``
  only by ``wlMinted[to] + qty <= allowance`` (any uint256 in the Merkle leaf).

So no ETH comes back to the signer inside a mint transaction, in any phase.

What the shape pins:

* the destination (intent and transaction) is the pinned SALE of a collection with the ``mint``
  capability — never the collection itself (it has no public mint);
* calldata is EXACTLY the canonical ABI encoding of one of the two calls, with ``to`` = the
  signing treasury (the guard must see every id ARRIVE in the paying wallet), ``qty`` = the
  number of declared ids, ``1 <= qty <= MAX_MINT_QTY``; a ``mintWL`` proof is at most
  :data:`MAX_PROOF_WORDS` words and ``qty <= allowance``;
* the value (``== amount_raw``): ``mintWL`` exactly ``qty × BASE_WEI``; ``mint`` at least
  ``qty × BASE_WEI`` and at most ``qty × sale.max_price_wei`` (the profile's cap — the phase and
  the live price are the contract's, and the simulation proves it accepts the value);
* the declared ids are consecutive (the collection mints ``nextId .. nextId+qty-1``);
* the treasury signs directly (no token-bound account) and declares no other shape;
* the simulation: the signer's native balance falls by exactly the value (± dust); per declared
  id the COLLECTION emits ``Transfer(0x0 → collection, id)`` then ``Transfer(collection → signer,
  id)`` (the token is minted to the collection, which registers the account's ERC-8004
  identity, then moves it — AgentIdentity) and one ``Minted(id, signer, …)``; the ids arrive
  from the PINNED collection address; no other movement or approval of the signer, and no log
  naming the signer in its topics except those events from the collection. No event of the sale
  indexes an address (they name ``to`` in their data), so none is allowlisted. A collection
  with the identity module (it emits ``IdentityRegistered`` / ``IdentitySkipped``) must show
  ``IdentityRegistered`` for every declared id; an ``IdentitySkipped`` for one refuses (permanent).

⚠️ ``BASE_WEI`` is a PINNED CONSTANT here, not a ``BASE()`` read: it is a Solidity ``constant``
of the sale, whose runtime sha256 is pinned, and a read is answered by the very contract the
guard is checking. The upper bound is the profile's ``sale.max_price_wei``, never a chain read.
"""
from __future__ import annotations

from typing import Optional, Sequence, Tuple

from core.wallet import abi

MINT_SIGNATURE = "mint(address,uint256,uint256)"
MINT_SELECTOR = "0x156e29f6"  # abi.selector(MINT_SIGNATURE), a literal for a bare install
MINT_WL_SIGNATURE = "mintWL(address,uint256,uint256,uint256,bytes32[])"
MINT_WL_SELECTOR = "0x1e29517f"  # abi.selector(MINT_WL_SIGNATURE)

#: The sale's ``uint256 public constant BASE = 0.033 ether`` (the whitelist price, the auction
#: floor, the price after the settlement).
BASE_WEI = 33 * 10 ** 15
#: The sale's ``MAX_PER_TX = 10`` (``_checkPublic`` reverts ``BadQty`` above it).
MAX_MINT_QTY = 10
#: The most words a whitelist proof may carry (a tree of 2**32 leaves; the real one is ~10).
MAX_PROOF_WORDS = 32

KIND_PUBLIC = "mint"
KIND_WHITELIST = "mintWL"

_ZERO_WORD = "0x" + "0" * 64


# keccak256 of each event signature, as literals: computing them at import needs
# eth_utils, which a bare install lacks (pack discovery imports this module).
# tests/unit/core/wallet/test_collection_topics.py pins each value.
TOPIC_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"  # Transfer(address,address,uint256)
#: The collection's spec — ``Minted(uint256 indexed id, address indexed to, address account,
#: uint256 pnl, uint256 eth, uint64 revealBlock)``, one per id.
TOPIC_MINTED = "0x4ef2d867c71b71127c9eb99425de0cf63abb4a0ed115e644e764379f1fe793f8"  # Minted(uint256,address,address,uint256,uint256,uint64)
#: The identity module (polyrob-desk ``kit/modules/AgentIdentity.sol:27-28``): at mint each id's
#: account registers an ERC-8004 identity — ``IdentityRegistered(uint256 indexed id, uint256
#: indexed agentId)`` — or the mint goes on without one, ``IdentitySkipped(uint256 indexed id)``.
#: A skipped identity is PERMANENT: only the mint registers (``_register``, ``:60-90``).
TOPIC_IDENTITY_REGISTERED = "0x053db8a054ebd7bf161422604c2f0481191c0c8b36115a8ea972d80f1f6e3d43"  # IdentityRegistered(uint256,uint256)
TOPIC_IDENTITY_SKIPPED = "0x480302a3b107bd3910f447869f148a65c2b163133bd53e6287fbf5fea9cdf168"  # IdentitySkipped(uint256)

#: Dust on the signer's native balance — the same constant ``tx_guard`` uses everywhere.
_NATIVE_DUST_WEI = 10 ** 12

_MINT_ARGS = [{"type": "address"}, {"type": "uint256"}, {"type": "uint256"}]
_MINT_WL_ARGS = _MINT_ARGS + [{"type": "uint256"}, {"type": "bytes32[]"}]


def encode_mint(to: str, qty: int, min_pnl_out_per_token: int = 0) -> str:
    return abi.encode_call("mint", _MINT_ARGS, [to, int(qty), int(min_pnl_out_per_token)])


def encode_mint_wl(to: str, qty: int, min_pnl_out_per_token: int, allowance: int,
                   proof: Sequence[str]) -> str:
    return abi.encode_call("mintWL", _MINT_WL_ARGS,
                           [to, int(qty), int(min_pnl_out_per_token), int(allowance), list(proof)])


def decode_mint(data: str):
    """``(to, qty, min_pnl_out_per_token)`` of a ``mint(address,uint256,uint256)`` calldata.
    Raises ValueError unless *data* is EXACTLY the canonical encoding."""
    text = str(data or "").lower()
    if not text.startswith(MINT_SELECTOR):
        raise ValueError(f"the calldata selector {text[:10] or '(none)'} is not "
                         f"{MINT_SIGNATURE} ({MINT_SELECTOR})")
    to, qty, min_out = abi.decode(_MINT_ARGS, "0x" + text[10:])
    if encode_mint(to, qty, min_out).lower() != text:
        raise ValueError("the calldata is not the canonical encoding of "
                         f"{MINT_SIGNATURE} for the arguments it decodes to")
    return str(to), int(qty), int(min_out)


def _proof_word(w) -> str:
    raw = bytes(w) if isinstance(w, (bytes, bytearray)) else bytes.fromhex(str(w).removeprefix("0x"))
    if len(raw) != 32:
        raise ValueError("a proof word is not 32 bytes")
    return "0x" + raw.hex()


def decode_mint_wl(data: str):
    """``(to, qty, min_pnl_out_per_token, allowance, proof)`` of a ``mintWL`` calldata (proof =
    a tuple of 0x-hex words). Raises ValueError unless *data* is EXACTLY the canonical encoding
    and the proof is at most :data:`MAX_PROOF_WORDS` words."""
    text = str(data or "").lower()
    if not text.startswith(MINT_WL_SELECTOR):
        raise ValueError(f"the calldata selector {text[:10] or '(none)'} is not "
                         f"{MINT_WL_SIGNATURE} ({MINT_WL_SELECTOR})")
    body = text[10:]
    # The proof length word sits at the offset the head names; bound it BEFORE decoding, so a
    # forged length never makes the decoder walk a huge array.
    try:
        offset = int(body[256:320], 16)
        if offset % 32 or len(body) < 2 * offset + 64:
            raise ValueError
        count = int(body[2 * offset:2 * offset + 64], 16)
    except ValueError:
        raise ValueError(f"the calldata is not a {MINT_WL_SIGNATURE} encoding") from None
    if count > MAX_PROOF_WORDS:
        raise ValueError(f"the whitelist proof has {count} words; at most {MAX_PROOF_WORDS}")
    to, qty, min_out, allowance, proof = abi.decode(_MINT_WL_ARGS, "0x" + body)
    words = tuple(_proof_word(w) for w in proof)
    if encode_mint_wl(to, qty, min_out, allowance, words).lower() != text:
        raise ValueError("the calldata is not the canonical encoding of "
                         f"{MINT_WL_SIGNATURE} for the arguments it decodes to")
    return str(to), int(qty), int(min_out), int(allowance), words


def decode_call(data: str) -> Tuple[str, str, int, int]:
    """``(kind, to, qty, min_pnl_out_per_token)`` of either sale call; the selector picks the
    decoder. Raises ValueError for anything else."""
    text = str(data or "").lower()
    if text.startswith(MINT_WL_SELECTOR):
        to, qty, min_out, allowance, _proof = decode_mint_wl(text)
        if not 1 <= qty <= allowance:
            raise ValueError(f"the whitelist mint of {qty} is above its allowance {allowance} "
                             f"(the sale reverts OverAllowance)")
        return KIND_WHITELIST, to, qty, min_out
    if text.startswith(MINT_SELECTOR):
        return (KIND_PUBLIC,) + decode_mint(text)
    raise ValueError(f"the calldata selector {text[:10] or '(none)'} is neither {MINT_SIGNATURE} "
                     f"({MINT_SELECTOR}) nor {MINT_WL_SIGNATURE} ({MINT_WL_SELECTOR})")


def value_refusal(kind: str, qty: int, value: int, max_price_wei: int) -> Optional[str]:
    """Why *value* is not a value the sale's *kind* call of *qty* accepts within the pin, or None."""
    floor = int(qty) * BASE_WEI
    if kind == KIND_WHITELIST:
        if value != floor:
            return (f"the transaction sends {value} wei; a whitelist mint of {qty} is exactly "
                    f"{floor} wei (qty × {BASE_WEI} — the sale reverts WrongPrice otherwise)")
        return None
    cap = int(qty) * int(max_price_wei)
    if not floor <= value <= cap:
        return (f"the transaction sends {value} wei; a public mint of {qty} pays at least "
                f"{floor} wei (qty × the base {BASE_WEI}) and at most {cap} wei (qty × the "
                f"pinned sale.max_price_wei {max_price_wei})")
    return None


def pinned_collections(chain: str):
    """The pinned collection(s) on *chain* with the ``mint`` capability (raises when the
    registry cannot be trusted)."""
    from core.wallet import collection_reveal
    return collection_reveal.pinned_collections(chain, "mint")


def _mint_profiles(chain: str):
    from core.wallet import collection_registry as registry
    return registry.profiles_on(registry.chain_id_of(chain), "mint")


def profile_of_sale(chain: str, sale: Optional[str]):
    """The pinned profile (``mint`` capability) whose sale is *sale*, or None. Raises when the
    registry cannot be trusted."""
    s = str(sale or "").lower()
    return next((p for p in _mint_profiles(chain) if p.sale is not None and p.sale.address == s),
                None)


def check_ids(ids: Sequence[int], max_supply: int) -> Optional[str]:
    ids = [int(i) for i in ids]
    if not ids:
        return "a mint must declare the ids it creates (`mint_ids`)"
    if len(ids) > MAX_MINT_QTY:
        return f"a mint declares {len(ids)} ids; at most {MAX_MINT_QTY} per transaction (the contract reverts BadQty)"
    if ids != list(range(ids[0], ids[0] + len(ids))):
        return (f"the declared ids {ids} are not consecutive ascending — one mint creates "
                f"nextId .. nextId+qty-1")
    if ids[0] < 1 or ids[-1] > int(max_supply):
        return f"the declared ids {ids[0]}..{ids[-1]} are outside 1..{max_supply}"
    return None


def structural(intent, tx: dict, holder: str) -> Optional[str]:
    """The refusal for a malformed mint intent/transaction, or None. No RPC."""
    other = [name for name, on in (
        ("a deployment", intent.is_deploy), ("a claim", intent.is_claim),
        ("an NFT operation", intent.is_nft_op), ("a registration", intent.is_registration),
        ("an allowance operation", intent.is_allowance_op),
        ("a liquidity operation", intent.is_liquidity_op),
        ("a collection reveal", intent.is_collection_reveal),
        ("an account call (via_account)", bool(intent.via_account)),
        ("an approve-spend-reset batch", intent.via_account_batch)) if on]
    if other:
        return (f"a collection mint cannot also be {', '.join(other)} — the mint shape asserts "
                f"exactly what leaves and what arrives, and the treasury signs it directly")
    declared = [name for name, v in (
        ("token", intent.token), ("allowance grants", intent.expected_allowance_grants),
        ("watch_spenders", intent.watch_spenders), ("inflow_token", intent.inflow_token),
        ("min_inflow_raw", intent.min_inflow_raw),
        ("min_native_inflow_wei", intent.min_native_inflow_wei),
        ("nft_out", intent.nft_out), ("expected_nft_in", intent.expected_nft_in),
        ("nft_operator_ops", intent.nft_operator_ops),
        ("nft_approval_revokes", intent.nft_approval_revokes),
        ("permit2_revokes", intent.permit2_revokes), ("reveal_ids", intent.reveal_ids),
        ("expected_events", intent.expected_events)) if v]
    if declared:
        return (f"a collection mint sends native value and receives the declared ids only, but this "
                f"intent also declares {', '.join(declared)}")
    try:
        profiles = _mint_profiles(intent.chain)
    except Exception as exc:  # noqa: BLE001 — an untrusted registry pins nothing
        return f"the collection registry cannot be trusted ({exc}); refusing"
    if not profiles:
        return (f"no collection with the mint capability is pinned on {intent.chain} (the "
                f"owner's collection registry is empty by default) — a mint may only pay the "
                f"pinned sale of a pinned collection")
    to_intent = str(intent.to or "").lower()
    to_tx = str((tx or {}).get("to") or "").lower()
    direct = next((p for p in profiles if p.address == to_intent), None)
    if direct is not None:
        return (f"the intent is addressed to the collection {intent.to} itself — the collection "
                f"has no public mint (mintFromSale accepts only its SALE()); a mint pays the "
                f"pinned sale {direct.sale.address if direct.sale else '(none)'}")
    profile = next((p for p in profiles if p.sale is not None and p.sale.address == to_intent),
                   None)
    if profile is None:
        return (f"the intent is addressed to {intent.to!r}, not the pinned sale of a collection "
                f"with the mint capability")
    if to_tx != to_intent:
        return (f"the TRANSACTION is addressed to {(tx or {}).get('to')!r} but the intent names "
                f"{intent.to} — the signed destination must be the pinned sale")
    why = check_ids(intent.mint_ids, profile.max_supply)
    if why:
        return why
    try:
        kind, to, qty, _min_out = decode_call((tx or {}).get("data") or "")
    except Exception as exc:  # noqa: BLE001
        return f"the calldata is not a sale mint ({exc})"
    if to.lower() != str(holder or "").lower():
        return (f"the mint sends the tokens to {to}, not to the signing treasury {holder} — "
                f"the guard verifies the tokens that ARRIVE in the paying wallet, and a mint to "
                f"another address is a payment whose return cannot be measured")
    if not 1 <= qty <= MAX_MINT_QTY:
        return f"the calldata mints {qty}; a mint is 1..{MAX_MINT_QTY} (the contract reverts BadQty)"
    if qty != len(intent.mint_ids):
        return (f"the calldata mints {qty} but the intent declares {len(intent.mint_ids)} ids "
                f"{list(intent.mint_ids)}")
    try:
        value = int((tx or {}).get("value") or 0)
    except (TypeError, ValueError):
        return "the transaction value is unreadable"
    why = value_refusal(kind, qty, value, profile.sale.max_price_wei)
    if why:
        return why
    if int(intent.amount_raw) != value:
        return (f"the intent declares amount_raw {intent.amount_raw}; the mint pays exactly the "
                f"value it sends, {value} wei")
    return None


def runtime_refusal(chain: str, sale: Optional[str], rpc) -> Optional[str]:
    """069 v4 A3 for a mint: the live code of BOTH the pinned collection and its pinned sale
    hashes to its own pin (fail closed)."""
    from core.wallet import collection_registry as registry
    try:
        profile = profile_of_sale(chain, sale)
    except Exception as exc:  # noqa: BLE001
        return f"the owner's collection registry cannot be trusted ({exc}); failing closed"
    if profile is None:
        return f"{sale} is not the pinned sale of a collection with the mint capability on {chain}"
    return registry.runtime_refusal(rpc, profile) or registry.sale_runtime_refusal(rpc, profile)


def sale_payment_refusal(intent, tx: dict) -> Optional[str]:
    """A pinned sale is paid ONLY by the guarded mint shape: any other intent that sends value
    to it (a native send, a token send to it, a payable call) refuses. A zero-value call is not
    a payment and passes to its own shape's rules. An untrusted registry refuses only what it
    guards (owner decision 2026-10-01), and the mint shape refuses in :func:`structural`."""
    if intent.is_collection_mint:
        return None
    try:
        value = int((tx or {}).get("value") or 0)
    except (TypeError, ValueError):
        value = 0
    if value <= 0 and int(intent.amount_raw or 0) <= 0:
        return None
    try:
        from core.wallet import collection_registry as registry
        chain_id = registry.chain_id_of(intent.chain)
        sales = {p.sale.address for p in registry.profiles_on(chain_id) if p.sale is not None}
    except Exception:  # noqa: BLE001
        return None
    for addr in (intent.to, (tx or {}).get("to")):
        a = str(addr or "").lower()
        if a and a in sales:
            return (f"{a} is the pinned sale of a collection — it is paid only by the guarded "
                    f"mint (agent_nft_collection_mint), never by another transaction")
    return None


def _word(addr: str) -> str:
    return "0x" + "0" * 24 + str(addr).lower().removeprefix("0x")


def assert_simulation(intent, deltas, holder: str) -> Optional[str]:
    """The refusal the simulated transaction earns, or None."""
    try:
        profile = profile_of_sale(intent.chain, intent.to)
    except Exception as exc:  # noqa: BLE001
        return f"the collection registry cannot be trusted ({exc}); refusing"
    if profile is None:
        return f"{intent.to} is not the pinned sale of a collection with the mint capability"
    collection = profile.address
    ids = [int(i) for i in intent.mint_ids]
    value = int(intent.amount_raw)
    native = int(getattr(deltas, "native_delta", 0) or 0)
    if native > 0:
        return (f"the mint RETURNS {native} wei to the signer — the sale refunds nothing inside "
                f"a mint (the excess comes back at settle), so this is not the transaction that "
                f"was priced")
    if abs(-native - value) > _NATIVE_DUST_WEI:
        return (f"the mint moves {-native} wei of the signer's native balance but pays exactly "
                f"{value} wei — a different outflow is not this transaction")
    moved = [name for name, rows in (
        ("token transfers", getattr(deltas, "holder_transfers", ())),
        ("token approvals", getattr(deltas, "holder_approvals", ())),
        ("NFT moves out", getattr(deltas, "holder_nft_out", ())),
        ("operator approvals", getattr(deltas, "holder_operator_grants", ())),
        ("NFT approvals", getattr(deltas, "holder_nft_approvals", ())),
        ("Permit2 approvals", getattr(deltas, "holder_permit2_grants", ()))) if rows]
    moved += [f"token {t} {d:+d}" for t, d in (getattr(deltas, "token_deltas", None) or {}).items() if d]
    if moved:
        return (f"the mint touches the signer's assets beyond the payment ({', '.join(moved)}) — "
                f"a mint pays native value and receives the declared ids, nothing else")
    # The ids arrive FROM the pinned collection: it mints each to itself, registers the
    # account's identity, then transfers it (AgentIdentity._mintWithAccountAndIdentity).
    arrived = sorted((str(c).lower(), str(s), str(frm).lower(), int(i), int(a))
                     for (c, s, frm, i, a) in (getattr(deltas, "holder_nft_in", ()) or ()))
    expected = sorted((collection, "erc721", collection, i, 1) for i in ids)
    if arrived != expected:
        got = [f"{c} #{i} from {frm}" for (c, _s, frm, i, _a) in arrived] or ["nothing"]
        return (f"the simulation delivers {', '.join(got)} — the mint must deliver exactly "
                f"ids {ids} of {collection}, each minted to the collection and moved from it "
                f"to the signer, and nothing else")
    word = _word(holder)
    coll_word = _word(collection)
    minted_in, transfers, moved_at, minted = {}, [], {}, []
    registered, skipped = set(), set()
    for n, log in enumerate(getattr(deltas, "logs", None) or ()):
        emitter = str(log.get("address") or "").lower()
        topics = [str(t).lower() for t in (log.get("topics") or [])]
        if emitter == collection and topics and topics[0] in (TOPIC_IDENTITY_REGISTERED,
                                                              TOPIC_IDENTITY_SKIPPED):
            if len(topics) >= 2:
                (registered if topics[0] == TOPIC_IDENTITY_REGISTERED else skipped).add(
                    int(topics[1], 16))
            continue
        if (emitter == collection and len(topics) == 4 and topics[0] == TOPIC_TRANSFER
                and topics[1] == _ZERO_WORD and topics[2] == coll_word):
            minted_in.setdefault(int(topics[3], 16), n)
            continue
        if word not in topics[1:]:
            continue
        if emitter != collection:
            return (f"a log from {emitter} names the signer — in a mint only the collection "
                    f"{collection} may address the treasury")
        if (len(topics) == 4 and topics[0] == TOPIC_TRANSFER and topics[1] == coll_word
                and topics[2] == word):
            transfers.append(int(topics[3], 16))
            moved_at.setdefault(int(topics[3], 16), n)
        elif len(topics) == 3 and topics[0] == TOPIC_MINTED and topics[2] == word:
            minted.append(int(topics[1], 16))
        else:
            return (f"the collection emits an event naming the signer that is not part of a "
                    f"mint ({topics[0][:10] if topics else 'anonymous'})")
    if sorted(transfers) != ids:
        return (f"the collection's Transfer(collection → signer) events carry ids "
                f"{sorted(transfers)}, not the declared {ids}")
    fresh = [i for i in ids if i in minted_in and minted_in[i] < moved_at[i]]
    if fresh != ids:
        return (f"the collection's Transfer(0x0 → collection) events, each before the move to the "
                f"signer, carry ids {fresh}, not the declared {ids} — an id the collection did "
                f"not mint in this transaction is not a mint")
    if sorted(minted) != ids:
        return (f"the collection emits Minted(id, signer) for ids {sorted(minted)}, not the "
                f"declared {ids} — one Minted per id is the receipt of a real mint")
    # The identity (115 W7 queued fix): a collection with the identity module emits one of the
    # two events per minted id. A profile does not say whether a collection has the module, so
    # the simulation does: any identity event at all means every declared id must register.
    lost = sorted(i for i in ids if i in skipped)
    if lost:
        return (f"the collection mints ids {lost} WITHOUT an ERC-8004 identity (IdentitySkipped: "
                f"the registry failed, has no code or ran out of its gas cap) — a skipped "
                f"identity is permanent, nothing registers it later; refusing")
    if registered or skipped:
        missing = [i for i in ids if i not in registered]
        if missing:
            return (f"the collection registers identities but shows no IdentityRegistered for "
                    f"ids {missing} — every minted id's account registers one at mint")
    return None
