"""The collection reveal transaction shape — ``reveal(uint256[] ids)`` on a pinned collection
(069 v4). A collection profile opts into it with the ``reveal`` capability
(``core/wallet/collection_registry.py``); the call shape itself is this module's, and a profile
naming any other shape is refused.

Measurement helpers for ONE ``tx_guard`` shape (``TxIntent.is_collection_reveal``), in the
``liquidity_guard`` / ``deploy_guard`` pattern: this module DECIDES nothing on its own —
``tx_guard._authorize`` calls :func:`structural` before the simulation and
:func:`assert_simulation` after it, and returns the refusal they name.

Why a shape of its own. ``reveal`` is callable by anyone, takes no value and moves no
asset: every other branch of the guard refuses it for a different reason (``amount_raw ==
0``; the native branch reads a zero outflow as a failed measurement), and — the reason
that matters — nothing else would check that the call DID anything. So, like the
registration shape (046), the receipt IS the shape: every declared id must emit exactly
one ``Revealed`` or ``Recommitted`` from the collection, and the transaction may touch
nothing else at all.

What the shape pins:

* the destination is a collection the owner pinned for the chain with the ``reveal``
  capability (``collection_registry``; empty by default — so every reveal refuses);
* value 0; calldata is EXACTLY the canonical ABI encoding of ``reveal(uint256[])`` for
  the declared ids (1..``MAX_REVEAL_IDS``, strictly ascending, each in 1..the profile's
  ``max_supply``);
* the treasury signs directly (no token-bound account in between) and declares no other shape;
* the simulation: no native, token, NFT or approval movement of the signer, no log that
  names the signer, every log emitted BY the collection, only the four reveal events, and
  one ``Revealed``/``Recommitted`` per declared id — no more, no fewer.
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from core.wallet import abi

REVEAL_SIGNATURE = "reveal(uint256[])"
REVEAL_SELECTOR = abi.selector(REVEAL_SIGNATURE)          # 0xb93f208a

#: Hard ceiling on ids in ONE reveal transaction. The verb's own default is 10
#: (``reveal(10)`` measured 369,566 gas on a 4663 fork, ≈35k per id).
MAX_REVEAL_IDS = 64


def _topic(signature: str) -> str:
    from eth_utils import keccak
    return "0x" + keccak(signature.encode()).hex()


#: The events a reveal emits (the collection's spec). ``Revealed`` or ``Recommitted`` is the
#: receipt (one per id); ``MetadataUpdate`` (ERC-4906) and ``TraitUpdated`` (ERC-7496)
#: ride a ``Revealed``. Nothing else may appear in the log.
TOPIC_REVEALED = _topic("Revealed(uint256,string)")
TOPIC_RECOMMITTED = _topic("Recommitted(uint256,uint64)")
TOPIC_METADATA_UPDATE = _topic("MetadataUpdate(uint256)")
TOPIC_TRAIT_UPDATED = _topic("TraitUpdated(bytes32,uint256,bytes32)")
_ALLOWED_TOPICS = frozenset({TOPIC_REVEALED, TOPIC_RECOMMITTED,
                             TOPIC_METADATA_UPDATE, TOPIC_TRAIT_UPDATED})

#: Dust on the signer's native balance (the simulation charges no gas) — the same
#: constant ``tx_guard`` uses for every zero-value shape.
_NATIVE_DUST_WEI = 10 ** 12


def encode_reveal(ids: Sequence[int]) -> str:
    return abi.encode_call("reveal", [{"type": "uint256[]"}], [[int(i) for i in ids]])


def decode_reveal(data: str) -> List[int]:
    """The ids of a ``reveal(uint256[])`` calldata. Raises ValueError unless *data* is
    EXACTLY the canonical encoding (no trailing bytes, no odd offset)."""
    text = str(data or "").lower()
    if not text.startswith(REVEAL_SELECTOR):
        raise ValueError(f"the calldata selector {text[:10] or '(none)'} is not "
                         f"{REVEAL_SIGNATURE} ({REVEAL_SELECTOR})")
    (ids,) = abi.decode([{"type": "uint256[]"}], "0x" + text[10:])
    ids = [int(i) for i in ids]
    if encode_reveal(ids).lower() != text:
        raise ValueError("the calldata is not the canonical encoding of reveal(uint256[]) "
                         "for the ids it decodes to")
    return ids


def pinned_collections(chain: str, capability: str = "reveal") -> Tuple[str, ...]:
    """The pinned collection(s) on *chain* declaring *capability* (lowercase). Empty = none
    pinned. Raises ``collection_registry.CollectionRegistryError`` when the registry cannot be
    trusted (callers refuse)."""
    from core.wallet import collection_registry as registry
    return tuple(p.address for p in registry.profiles_on(registry.chain_id_of(chain), capability))


def max_supply_of(chain: str, collection: str) -> int:
    """The pinned profile's ``max_supply`` for *collection* on *chain* (raises when not pinned)."""
    from core.wallet import collection_registry as registry
    prof = registry.profile_for(registry.chain_id_of(chain), collection)
    if prof is None:
        raise ValueError(f"{collection} is not a pinned collection on {chain}")
    return prof.max_supply


def check_ids(ids: Sequence[int], max_supply: int) -> Optional[str]:
    if not ids:
        return "a reveal must name at least one id"
    if len(ids) > MAX_REVEAL_IDS:
        return f"a reveal names {len(ids)} ids; at most {MAX_REVEAL_IDS} per transaction"
    prev = 0
    for i in ids:
        if not (1 <= int(i) <= int(max_supply)):
            return f"id {i} is outside 1..{max_supply}"
        if int(i) <= prev:
            return "the ids must be strictly ascending (id order, no repeats)"
        prev = int(i)
    return None


def structural(intent, tx: dict) -> Optional[str]:
    """The refusal for a malformed reveal intent/transaction, or None. No RPC."""
    other = [name for name, on in (
        ("a deployment", intent.is_deploy), ("a claim", intent.is_claim),
        ("an NFT operation", intent.is_nft_op), ("a registration", intent.is_registration),
        ("an allowance operation", intent.is_allowance_op),
        ("a liquidity operation", intent.is_liquidity_op),
        ("an account call (via_account)", bool(intent.via_account)),
        ("an approve-spend-reset batch", intent.via_account_batch)) if on]
    if other:
        return (f"a collection reveal cannot also be {', '.join(other)} — the reveal shape asserts "
                f"that nothing moves, and the treasury signs it directly")
    declared = [name for name, v in (
        ("token", intent.token), ("allowance grants", intent.expected_allowance_grants),
        ("watch_spenders", intent.watch_spenders), ("inflow_token", intent.inflow_token),
        ("min_inflow_raw", intent.min_inflow_raw),
        ("min_native_inflow_wei", intent.min_native_inflow_wei),
        ("nft_out", intent.nft_out), ("expected_nft_in", intent.expected_nft_in),
        ("nft_operator_ops", intent.nft_operator_ops),
        ("nft_approval_revokes", intent.nft_approval_revokes),
        ("permit2_revokes", intent.permit2_revokes),
        ("expected_events", intent.expected_events)) if v]
    if declared or intent.amount_raw != 0:
        what = ", ".join(declared + (["amount_raw"] if intent.amount_raw != 0 else []))
        return (f"a collection reveal sends and receives nothing, but this intent declares {what}")
    try:
        collections = pinned_collections(intent.chain)
    except Exception as exc:  # noqa: BLE001 — an untrusted registry pins nothing
        return f"the collection registry cannot be trusted ({exc}); refusing"
    if not collections:
        return (f"no collection with the reveal capability is pinned on {intent.chain} (the "
                f"owner's collection registry is empty by default) — a reveal may only call a "
                f"pinned collection")
    to_intent = str(intent.to or "").lower()
    to_tx = str((tx or {}).get("to") or "").lower()
    if to_intent not in collections:
        return f"the intent is addressed to {intent.to!r}, not a pinned collection"
    if to_tx != to_intent:
        return (f"the TRANSACTION is addressed to {(tx or {}).get('to')!r} but the intent names "
                f"{intent.to} — the signed destination must be the pinned collection")
    try:
        value = int((tx or {}).get("value") or 0)
    except (TypeError, ValueError):
        return "the transaction value is unreadable"
    if value != 0:
        return f"a reveal carries no value; this transaction sends {value} wei"
    try:
        cap = max_supply_of(intent.chain, to_intent)
    except Exception as exc:  # noqa: BLE001
        return f"the pinned profile of {intent.to} cannot be read ({exc}); refusing"
    why = check_ids(intent.reveal_ids, cap)
    if why:
        return why
    try:
        ids = decode_reveal((tx or {}).get("data") or "")
    except Exception as exc:  # noqa: BLE001
        return f"the calldata is not reveal(uint256[]) ({exc})"
    if ids != [int(i) for i in intent.reveal_ids]:
        return (f"the calldata reveals {ids} but the intent declares "
                f"{list(intent.reveal_ids)}")
    return None


def _signer_word(holder: str) -> str:
    return "0x" + "0" * 24 + str(holder).lower().removeprefix("0x")


def assert_simulation(intent, deltas, holder: str) -> Tuple[Optional[str], Tuple[int, ...], Tuple[int, ...]]:
    """``(refusal, revealed_ids, recommitted_ids)`` from the simulated transaction."""
    none = ((), ())
    native = int(getattr(deltas, "native_delta", 0) or 0)
    if abs(native) > _NATIVE_DUST_WEI:
        return (f"the reveal moved {native} wei of the signer's native balance — reveal() "
                f"is not payable, so any native movement is not this transaction"), *none
    moved = [name for name, rows in (
        ("token transfers", getattr(deltas, "holder_transfers", ())),
        ("token approvals", getattr(deltas, "holder_approvals", ())),
        ("NFT moves out", getattr(deltas, "holder_nft_out", ())),
        ("NFT arrivals", getattr(deltas, "holder_nft_in", ())),
        ("operator approvals", getattr(deltas, "holder_operator_grants", ())),
        ("NFT approvals", getattr(deltas, "holder_nft_approvals", ())),
        ("Permit2 approvals", getattr(deltas, "holder_permit2_grants", ()))) if rows]
    moved += [f"token {t} {d:+d}" for t, d in (getattr(deltas, "token_deltas", None) or {}).items() if d]
    if moved:
        return (f"the reveal touches the signer's assets ({', '.join(moved)}) — a reveal "
                f"moves nothing but gas"), *none
    collection = str(intent.to or "").lower()
    word = _signer_word(holder)
    revealed: List[int] = []
    recommitted: List[int] = []
    for log in getattr(deltas, "logs", None) or ():
        emitter = str(log.get("address") or "").lower()
        topics = [str(t).lower() for t in (log.get("topics") or [])]
        if word in topics[1:]:
            return (f"a log from {emitter} names the signer — a reveal leaves the signer's "
                    f"wallet untouched in every direction"), *none
        if emitter != collection:
            return (f"the reveal makes {emitter} emit an event — reveal() touches only the "
                    f"collection {intent.to}"), *none
        if not topics or topics[0] not in _ALLOWED_TOPICS:
            return (f"the collection emits an event that is not part of a reveal "
                    f"({topics[0][:10] if topics else 'anonymous'})"), *none
        if topics[0] in (TOPIC_REVEALED, TOPIC_RECOMMITTED):
            if len(topics) < 2:
                return "a Revealed/Recommitted event carries no indexed id", *none
            (revealed if topics[0] == TOPIC_REVEALED else recommitted).append(int(topics[1], 16))
    got = sorted(revealed + recommitted)
    want = sorted(int(i) for i in intent.reveal_ids)
    if not got:
        return (f"the simulation shows no Revealed or Recommitted event from {intent.to} — a "
                f"reveal that reveals nothing is a call that only burns gas, and nothing "
                f"else can tell the two apart"), *none
    if got != want:
        return (f"the simulation reveals/recommits ids {got} but the intent declares {want} — "
                f"every declared id must be due (one event each), and no other id may move"), *none
    return None, tuple(revealed), tuple(recommitted)
