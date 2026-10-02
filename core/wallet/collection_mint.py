"""The collection paid-mint transaction shape — ``mint(address to, uint256 qty, uint256
minPnlOutPerToken)`` on a pinned collection (069 v4; testnet-run 2026-09-29 finding F2). A
collection profile opts into it with the ``mint`` capability
(``core/wallet/collection_registry.py``); the call shape, price and per-call cap are this
module's, and a profile naming any other shape is refused.

Measurement helpers for ONE ``tx_guard`` shape (``TxIntent.is_collection_mint``), in the
``agent_nft_collection_reveal`` / ``deploy_guard`` pattern: this module DECIDES nothing on its own —
``tx_guard._authorize`` calls :func:`structural` before the simulation and
:func:`assert_simulation` after it, and returns the refusal they name.

Why a shape of its own. A paid mint sends ``qty × PRICE`` of NATIVE value AND receives
``qty`` ERC-721s. Every existing branch refuses it: the NFT branch refuses any native
movement (an NFT transfer is not payable), and the native-send branch knows nothing of the
tokens that must arrive. Found by the 46630 rehearsal: ``agent_nft_collection_mint`` could never pass the
guard on any chain (the unit tests faked the guard; the fork e2e minted with ``cast``).

What the shape pins:

* the destination is a collection the owner pinned for the chain with the ``mint``
  capability (``collection_registry``; empty by default — so every mint refuses);
* calldata is EXACTLY the canonical ABI encoding of ``mint(address,uint256,uint256)`` with
  ``to`` = the signing treasury (the guard must see every id ARRIVE in the paying wallet)
  and ``qty`` = the number of declared ids, ``1 <= qty <= MAX_MINT_QTY``;
* ``value == amount_raw == qty × MINT_PRICE_WEI`` (the contract reverts ``WrongPrice`` on
  anything else, so a different value is not this transaction);
* the declared ids are consecutive (the contract mints ``nextId .. nextId+qty-1``);
* the treasury signs directly (no token-bound account) and declares no other shape;
* the simulation: the signer's native balance falls by exactly the value (± dust), exactly
  ``qty`` ERC-721 ``Transfer(0x0 → signer, id)`` from the collection for the declared ids and
  one ``Minted(id, signer, …)`` each, no other movement or approval of the signer, and no
  log naming the signer except those two events from the collection.

⚠️ The price is a PINNED CONSTANT here, not a ``PRICE()`` read. ``PRICE`` is a Solidity
``constant`` (immutable in the contract, per the collection's spec), so a read adds no fact the pin does not
carry — and a read is answered by the very contract the guard is checking, so it would echo
whatever that contract says. The constant bounds the value independently of the chain; the
simulation proves the pinned collection accepts it (``WrongPrice`` reverts otherwise); the
4663 fork test asserts ``PRICE() == MINT_PRICE_WEI`` on the real bytecode.
"""
from __future__ import annotations

from typing import Optional, Sequence

from core.wallet import abi

MINT_SIGNATURE = "mint(address,uint256,uint256)"
MINT_SELECTOR = abi.selector(MINT_SIGNATURE)

#: The collection's spec — ``uint256 public constant PRICE = 0.042 ether``.
MINT_PRICE_WEI = 42 * 10 ** 15
#: The collection's spec — ``MAX_PER_TX = 10`` (the contract reverts ``BadQty`` above it).
MAX_MINT_QTY = 10

_ZERO_WORD = "0x" + "0" * 64


def _topic(signature: str) -> str:
    from eth_utils import keccak
    return "0x" + keccak(signature.encode()).hex()


TOPIC_TRANSFER = _topic("Transfer(address,address,uint256)")
#: The collection's spec — ``Minted(uint256 indexed id, address indexed to, address account,
#: uint256 pnl, uint256 eth, uint64 revealBlock)``, one per id.
TOPIC_MINTED = _topic("Minted(uint256,address,address,uint256,uint256,uint64)")

#: Dust on the signer's native balance — the same constant ``tx_guard`` uses everywhere.
_NATIVE_DUST_WEI = 10 ** 12

_MINT_ARGS = [{"type": "address"}, {"type": "uint256"}, {"type": "uint256"}]


def encode_mint(to: str, qty: int, min_pnl_out_per_token: int = 0) -> str:
    return abi.encode_call("mint", _MINT_ARGS, [to, int(qty), int(min_pnl_out_per_token)])


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


def mint_value(qty: int) -> int:
    return int(qty) * MINT_PRICE_WEI


def pinned_collections(chain: str):
    """The pinned collection(s) on *chain* with the ``mint`` capability (raises when the
    registry cannot be trusted)."""
    from core.wallet import collection_reveal
    return collection_reveal.pinned_collections(chain, "mint")


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
        collections = pinned_collections(intent.chain)
    except Exception as exc:  # noqa: BLE001 — an untrusted registry pins nothing
        return f"the collection registry cannot be trusted ({exc}); refusing"
    if not collections:
        return (f"no collection with the mint capability is pinned on {intent.chain} (the "
                f"owner's collection registry is empty by default) — a mint may only pay a "
                f"pinned collection")
    to_intent = str(intent.to or "").lower()
    to_tx = str((tx or {}).get("to") or "").lower()
    if to_intent not in collections:
        return f"the intent is addressed to {intent.to!r}, not a pinned collection"
    if to_tx != to_intent:
        return (f"the TRANSACTION is addressed to {(tx or {}).get('to')!r} but the intent names "
                f"{intent.to} — the signed destination must be the pinned collection")
    try:
        from core.wallet import collection_reveal
        cap = collection_reveal.max_supply_of(intent.chain, to_intent)
    except Exception as exc:  # noqa: BLE001
        return f"the pinned profile of {intent.to} cannot be read ({exc}); refusing"
    why = check_ids(intent.mint_ids, cap)
    if why:
        return why
    try:
        to, qty, _min_out = decode_mint((tx or {}).get("data") or "")
    except Exception as exc:  # noqa: BLE001
        return f"the calldata is not {MINT_SIGNATURE} ({exc})"
    if to.lower() != str(holder or "").lower():
        return (f"the mint sends the tokens to {to}, not to the signing treasury {holder} — "
                f"the guard verifies the tokens that ARRIVE in the paying wallet, and a mint to "
                f"another address is a payment whose return cannot be measured")
    if not 1 <= qty <= MAX_MINT_QTY:
        return f"the calldata mints {qty}; a mint is 1..{MAX_MINT_QTY} (the contract reverts BadQty)"
    if qty != len(intent.mint_ids):
        return (f"the calldata mints {qty} but the intent declares {len(intent.mint_ids)} ids "
                f"{list(intent.mint_ids)}")
    want = mint_value(qty)
    try:
        value = int((tx or {}).get("value") or 0)
    except (TypeError, ValueError):
        return "the transaction value is unreadable"
    if value != want:
        return (f"the transaction sends {value} wei; a mint of {qty} is exactly {want} wei "
                f"(qty × {MINT_PRICE_WEI} — the contract reverts WrongPrice otherwise)")
    if int(intent.amount_raw) != want:
        return (f"the intent declares amount_raw {intent.amount_raw}; a mint of {qty} pays "
                f"exactly {want} wei")
    return None


def _word(addr: str) -> str:
    return "0x" + "0" * 24 + str(addr).lower().removeprefix("0x")


def assert_simulation(intent, deltas, holder: str) -> Optional[str]:
    """The refusal the simulated transaction earns, or None."""
    collection = str(intent.to or "").lower()
    ids = [int(i) for i in intent.mint_ids]
    value = int(intent.amount_raw)
    native = int(getattr(deltas, "native_delta", 0) or 0)
    if native > 0:
        return (f"the mint RETURNS {native} wei to the signer — a mint at the fixed price "
                f"refunds nothing, so this is not the transaction that was priced")
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
    arrived = sorted((str(c).lower(), str(s), str(frm).lower(), int(i), int(a))
                     for (c, s, frm, i, a) in (getattr(deltas, "holder_nft_in", ()) or ()))
    zero = "0x" + "0" * 40
    expected = sorted((collection, "erc721", zero, i, 1) for i in ids)
    if arrived != expected:
        got = [f"{c} #{i} from {frm}" for (c, _s, frm, i, _a) in arrived] or ["nothing"]
        return (f"the simulation delivers {', '.join(got)} — the mint must deliver exactly "
                f"ids {ids} minted (from 0x0) by {intent.to}, and nothing else")
    word = _word(holder)
    zero_word = _ZERO_WORD
    transfers, minted = [], []
    for log in getattr(deltas, "logs", None) or ():
        emitter = str(log.get("address") or "").lower()
        topics = [str(t).lower() for t in (log.get("topics") or [])]
        if word not in topics[1:]:
            continue
        if emitter != collection:
            return (f"a log from {emitter} names the signer — in a mint only the collection "
                    f"{intent.to} may address the treasury")
        if (len(topics) == 4 and topics[0] == TOPIC_TRANSFER and topics[1] == zero_word
                and topics[2] == word):
            transfers.append(int(topics[3], 16))
        elif len(topics) == 3 and topics[0] == TOPIC_MINTED and topics[2] == word:
            minted.append(int(topics[1], 16))
        else:
            return (f"the collection emits an event naming the signer that is not part of a "
                    f"mint ({topics[0][:10] if topics else 'anonymous'})")
    if sorted(transfers) != ids:
        return (f"the collection's Transfer(0x0 → signer) events carry ids {sorted(transfers)}, "
                f"not the declared {ids}")
    if sorted(minted) != ids:
        return (f"the collection emits Minted(id, signer) for ids {sorted(minted)}, not the "
                f"declared {ids} — one Minted per id is the receipt of a real mint")
    return None
