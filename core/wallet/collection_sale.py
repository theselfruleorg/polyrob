"""The pinned SALE's two pay-back calls — ``claim(address who)`` and ``settle(uint256[] ids)`` on
the ``sale`` of a collection profile with the ``mint`` capability (proposal 115 W8, item 5 of the
sale work order; ``core/wallet/collection_registry.py`` pins the sale's address and code).

Measurement helpers for the ``tx_guard`` CLAIM shape (``TxIntent.is_claim`` +
``min_native_inflow_wei``, the shape ``launchpad_claim`` uses) when its target
is a pinned sale. This module DECIDES nothing on its own: ``tx_guard_steps.shape`` calls
:func:`gate` before any RPC and :func:`runtime_refusal` with the RPC pins, and
``tx_guard_steps.measure`` calls :func:`assert_simulation` after the generic claim checks.

The contract of record (polyrob-desk ``contracts/src/drop/PolyrobSale.sol``; SPEC §6.7):

* ``mint`` before ``settled()`` keeps the whole value; the division dust ``msg.value mod qty`` is
  booked in ``owed[to]`` (``_owe``) and one ``Record{to, firstId, qty, paidEach, done}`` is pushed.
* ``settle(uint256[] ids)`` — ANYONE, after ``settled()`` (else ``NotSettled``): per record not
  done, ``(paidEach − clearing) × qty`` is PUSHED to the record's ``to`` and a top-up to each
  token's account; ``Settled(record, refund, topUpEach)``; a done record is skipped with no
  event; a push that fails is booked in ``owed`` (``Owed(who, amount)``).
* ``claim(address who)`` — ANYONE: pays ``owed[who]`` to ``who`` (``NothingOwed`` at 0);
  ``Claimed(who, amount)``.

So the treasury gets its auction excess back ONLY when its own records are settled (a push, to
the record's ``to`` = the treasury the mint shape forces), and its dust (or a failed push) only
by ``claim(treasury)``. Both send nothing and RECEIVE native: the claim shape, restricted here.

What this module adds to the claim shape for a pinned sale:

* the gate — every intent that names a pinned sale (``to`` or the called contract) and is not
  the mint is one of these two calls or refuses: no generic write verb may call a pinned sale;
* calldata is EXACTLY ``claim(treasury)`` or ``settle(ids)`` (1..:data:`MAX_SETTLE_RECORDS`,
  strictly ascending); value 0; signed by the treasury directly (no account: a POLYROB account
  cannot refuse a top-up, SPEC §6.7, so nothing is ever owed to it);
* the RPC pins — the collection's and the sale's code each hash to its own pin, and for a settle
  ``settled()`` is true and every record is the treasury's own and not done (``record(i)``);
* the simulation — no token, NFT or approval of the signer moves; every log comes from the sale;
  no log names the signer in its topics; a claim emits exactly one ``Claimed(treasury, ≥ min)``,
  a settle exactly one ``Settled`` per declared record with the refunds summing to ≥ min, and no
  ``Owed`` to the treasury. The generic claim checks measure the native receipt ≥ the minimum.
"""
from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from core.wallet import abi

CLAIM_SIGNATURE = "claim(address)"
CLAIM_SELECTOR = "0x1e83409a"    # abi.selector(CLAIM_SIGNATURE), a literal for a bare install
SETTLE_SIGNATURE = "settle(uint256[])"
SETTLE_SELECTOR = "0x9b2db373"   # abi.selector(SETTLE_SIGNATURE)
OWED_SELECTOR = "0xdf18e047"     # owed(address)
PHASE_SELECTOR = "0xb1c9fe6e"    # phase()
SETTLED_SELECTOR = "0x8f775839"  # settled()
CLEARING_SELECTOR = "0x027cb7c6"  # clearing()
RECORD_SELECTOR = "0x2c16cd8a"   # record(uint256)

KIND_CLAIM = "claim"
KIND_SETTLE = "settle"

#: Most records in one settle (a qty-10 record measured 173,779 gas, SPEC §11).
MAX_SETTLE_RECORDS = 20

# keccak256 of each event signature, as literals (a bare install lacks eth_utils);
# tests/unit/core/wallet/test_collection_sale.py pins each value.
TOPIC_SETTLED = "0x321f57f44af402708b8b3bad0bd10012cd568d8696946e26edeb86178bb7c27e"  # Settled(uint256,uint256,uint256)
TOPIC_CLAIMED = "0xd8138f8a3f377c5259ca548e70e4c2de94f129f5a11036a15b69513cba2b426a"  # Claimed(address,uint256)
TOPIC_OWED = "0x7393c924ac30f9256e6cebcfb78841b1e7ef11bf8b09d87c2257b32b8063c979"  # Owed(address,uint256)
TOPIC_BID = "0x76b552d768564e2bf5325e2d83fe7184b2838e6e58fc33173aef30980e0269c8"  # Bid(uint256,address,uint256,uint256,uint256)

#: The verbs that may call a pinned sale (named in the refusal).
SALE_VERBS = "agent_nft_collection_mint, agent_nft_sale_claim and agent_nft_sale_settle"


def encode_claim(who: str) -> str:
    return abi.encode_call("claim", [{"type": "address"}], [who])


def encode_settle(ids: Sequence[int]) -> str:
    return abi.encode_call("settle", [{"type": "uint256[]"}], [[int(i) for i in ids]])


def decode_call(data: str) -> Tuple[str, object]:
    """``(KIND_CLAIM, who)`` or ``(KIND_SETTLE, [ids])``. Raises ValueError unless *data* is
    EXACTLY the canonical encoding of one of the two calls."""
    text = str(data or "").lower()
    if text.startswith(CLAIM_SELECTOR):
        (who,) = abi.decode([{"type": "address"}], "0x" + text[10:])
        if encode_claim(who).lower() != text:
            raise ValueError(f"the calldata is not the canonical encoding of {CLAIM_SIGNATURE}")
        return KIND_CLAIM, str(who).lower()
    if text.startswith(SETTLE_SELECTOR):
        body = text[10:]
        # Bound the array length BEFORE decoding (a forged length never walks a huge array).
        try:
            offset = int(body[0:64], 16)
            if offset % 32 or len(body) < 2 * offset + 64:
                raise ValueError
            count = int(body[2 * offset:2 * offset + 64], 16)
        except ValueError:
            raise ValueError(f"the calldata is not a {SETTLE_SIGNATURE} encoding") from None
        if count > MAX_SETTLE_RECORDS:
            raise ValueError(f"the settle names {count} records; at most {MAX_SETTLE_RECORDS}")
        (ids,) = abi.decode([{"type": "uint256[]"}], "0x" + body)
        ids = [int(i) for i in ids]
        if encode_settle(ids).lower() != text:
            raise ValueError(f"the calldata is not the canonical encoding of {SETTLE_SIGNATURE}")
        return KIND_SETTLE, ids
    raise ValueError(f"the calldata selector {text[:10] or '(none)'} is neither {CLAIM_SIGNATURE} "
                     f"({CLAIM_SELECTOR}) nor {SETTLE_SIGNATURE} ({SETTLE_SELECTOR})")


def check_ids(ids: Sequence[int]) -> Optional[str]:
    if not ids:
        return "a settle must name at least one record"
    if len(ids) > MAX_SETTLE_RECORDS:
        return f"a settle names {len(ids)} records; at most {MAX_SETTLE_RECORDS} per transaction"
    if any(int(b) <= int(a) for a, b in zip(ids, ids[1:])):
        return "the record ids must be strictly ascending (no repeats)"
    return None


def _sale_profiles(chain: str):
    from core.wallet import collection_registry as registry
    return {p.sale.address: p for p in registry.profiles_on(registry.chain_id_of(chain))
            if p.sale is not None}


def gate(intent, tx: dict, holder: str) -> Tuple[Optional[str], Optional[str]]:
    """``(kind, refusal)`` for any intent that is not the mint. ``(None, None)``: the intent names
    no pinned sale. ``(kind, None)``: a well-formed claim/settle of a pinned sale. ``(None, why)``:
    refuse. An untrusted registry refuses only what it guards (owner decision 2026-10-01) — the
    sale verbs read the registry themselves and refuse first. No RPC."""
    try:
        sales = _sale_profiles(intent.chain)
    except Exception:  # noqa: BLE001
        return None, None
    to_intent = str(intent.to or "").lower()
    to_tx = str((tx or {}).get("to") or "").lower()
    hit = next((a for a in (to_intent, to_tx) if a and a in sales), None)
    if hit is None:
        return None, None
    if not intent.is_claim:
        return None, (f"{hit} is the pinned sale of a collection — only the guarded {SALE_VERBS} "
                      f"call it; no other transaction may name it")
    other = [name for name, on in (
        ("a deployment", intent.is_deploy), ("an NFT operation", intent.is_nft_op),
        ("a registration", intent.is_registration),
        ("an allowance operation", intent.is_allowance_op),
        ("a liquidity operation", intent.is_liquidity_op),
        ("a collection reveal", intent.is_collection_reveal),
        ("a resource call", intent.is_resource_call), ("a journal entry", intent.is_journal_entry),
        ("an account call (via_account)", bool(intent.via_account)),
        ("an approve-spend-reset batch", intent.via_account_batch)) if on]
    if other:
        return None, (f"a claim from the pinned sale cannot also be {', '.join(other)} — the "
                      f"treasury signs it directly (records pay the treasury, and a POLYROB "
                      f"account is never owed)")
    if to_intent != hit or to_tx != hit:
        return None, (f"a claim from the pinned sale {hit} must be addressed to it in the intent "
                      f"({intent.to}) and in the signed transaction ({(tx or {}).get('to')})")
    declared = [name for name, v in (
        ("token", intent.token), ("inflow_token", intent.inflow_token),
        ("min_inflow_raw", intent.min_inflow_raw), ("amount_raw", intent.amount_raw),
        ("allowance grants", intent.expected_allowance_grants),
        ("nft_out", intent.nft_out), ("expected_nft_in", intent.expected_nft_in),
        ("mint_ids", intent.mint_ids), ("reveal_ids", intent.reveal_ids),
        ("expected_events", intent.expected_events)) if v]
    if declared:
        return None, (f"a claim from the pinned sale receives native value only, but this intent "
                      f"declares {', '.join(declared)}")
    if not (intent.min_native_inflow_wei or 0) > 0:
        return None, ("a claim from the pinned sale must declare the native it receives "
                      "(min_native_inflow_wei > 0, read from the sale)")
    try:
        value = int((tx or {}).get("value") or 0)
    except (TypeError, ValueError):
        return None, "the transaction value is unreadable"
    if value != 0:
        return None, f"a claim from the pinned sale carries no value; this transaction sends {value} wei"
    try:
        kind, arg = decode_call((tx or {}).get("data") or "")
    except Exception as exc:  # noqa: BLE001
        return None, f"the calldata is not a claim or settle of the pinned sale ({exc})"
    if kind == KIND_CLAIM:
        if arg != str(holder or "").lower():
            return None, (f"the claim pays {arg}, not the signing treasury {holder} — the guard "
                          f"measures what arrives in the signing wallet only")
        return kind, None
    why = check_ids(arg)
    return (None, why) if why else (kind, None)


def _uint(rpc, to: str, data: str) -> int:
    raw = rpc("eth_call", [{"to": to, "data": data}, "latest"])
    if not isinstance(raw, str) or not raw.startswith("0x") or len(raw) < 66:
        raise ValueError(f"unreadable word {raw!r}")
    return int(raw[2:66], 16)


def read_record(rpc, sale: str, record_id: int) -> Tuple[str, int, int, int, bool]:
    """``(to, firstId, qty, paidEach, done)`` of ``record(i)`` (a static struct: five words)."""
    raw = rpc("eth_call", [{"to": sale, "data": RECORD_SELECTOR + format(int(record_id), "064x")},
                           "latest"])
    to, first, qty, paid, done = abi.decode(
        [{"type": "address"}, {"type": "uint256"}, {"type": "uint256"}, {"type": "uint256"},
         {"type": "bool"}], raw)
    return str(to).lower(), int(first), int(qty), int(paid), bool(done)


def read_settled(rpc, sale: str) -> bool:
    return _uint(rpc, sale, SETTLED_SELECTOR) == 1


def read_clearing(rpc, sale: str) -> int:
    return _uint(rpc, sale, CLEARING_SELECTOR)


def read_phase(rpc, sale: str) -> int:
    return _uint(rpc, sale, PHASE_SELECTOR)


def read_owed(rpc, sale: str, who: str) -> int:
    return _uint(rpc, sale, OWED_SELECTOR + "0" * 24 + str(who).lower().removeprefix("0x"))


def runtime_refusal(kind: str, intent, tx: dict, holder: str, rpc) -> Optional[str]:
    """The RPC pins: both codes, and for a settle the records are the treasury's and open."""
    from core.wallet import collection_mint
    why = collection_mint.runtime_refusal(intent.chain, intent.to, rpc)
    if why or kind != KIND_SETTLE:
        return why
    sale = str(intent.to).lower()
    try:
        _kind, ids = decode_call((tx or {}).get("data") or "")
        if not read_settled(rpc, sale):
            return "the sale is not settled yet — settle() reverts NotSettled before settled()"
        for i in ids:
            to, _first, _qty, _paid, done = read_record(rpc, sale, i)
            if to != str(holder).lower():
                return (f"record {i} pays {to}, not this treasury {holder} — a settle of another "
                        f"buyer's record costs gas and pays this wallet nothing")
            if done:
                return f"record {i} is already settled (the sale skips it with no event)"
    except Exception as exc:  # noqa: BLE001 — an unread record is never the treasury's
        return f"the sale's records could not be read ({exc}); failing closed"
    return None


def _word_addr(word: str) -> str:
    return "0x" + word[-40:].lower()


def _data_words(log) -> List[str]:
    data = str(log.get("data") or "0x").lower().removeprefix("0x")
    return [data[i:i + 64] for i in range(0, len(data) - 63, 64)]


def assert_simulation(kind: str, intent, tx: dict, deltas, holder: str) -> Optional[str]:
    """The refusal the simulated claim/settle earns beyond the generic claim checks, or None."""
    moved = [name for name, rows in (
        ("token transfers", getattr(deltas, "holder_transfers", ())),
        ("token approvals", getattr(deltas, "holder_approvals", ())),
        ("NFT moves out", getattr(deltas, "holder_nft_out", ())),
        ("NFT arrivals", getattr(deltas, "holder_nft_in", ())),
        ("operator approvals", getattr(deltas, "holder_operator_grants", ())),
        ("NFT approvals", getattr(deltas, "holder_nft_approvals", ())),
        ("Permit2 approvals", getattr(deltas, "holder_permit2_grants", ()))) if rows]
    moved += [f"token {t} {d:+d}" for t, d in (getattr(deltas, "token_deltas", None) or {}).items() if d]
    moved += [f"allowance {t}→{s} {d:+d}"
              for (t, s), d in (getattr(deltas, "allowance_deltas", None) or {}).items() if d]
    if moved:
        return (f"the {kind} touches the signer's assets beyond the native receipt "
                f"({', '.join(moved)}) — a claim from the sale receives native value, nothing else")
    sale = str(intent.to or "").lower()
    me = str(holder).lower()
    word = "0x" + "0" * 24 + me.removeprefix("0x")
    claimed: List[Tuple[str, int]] = []
    settled: List[Tuple[int, int]] = []
    for log in getattr(deltas, "logs", None) or ():
        emitter = str(log.get("address") or "").lower()
        topics = [str(t).lower() for t in (log.get("topics") or [])]
        if word in topics[1:]:
            return f"a log from {emitter} names the signer — no event of the sale indexes an address"
        if emitter != sale:
            return (f"the {kind} makes {emitter} emit an event — every log of a claim from the "
                    f"sale comes from the sale {sale}")
        words = _data_words(log)
        head = topics[0] if topics else ""
        if head == TOPIC_CLAIMED and kind == KIND_CLAIM and len(words) == 2:
            claimed.append((_word_addr(words[0]), int(words[1], 16)))
        elif head == TOPIC_SETTLED and kind == KIND_SETTLE and len(words) == 3:
            settled.append((int(words[0], 16), int(words[1], 16)))
        elif head == TOPIC_OWED and kind == KIND_SETTLE and len(words) == 2:
            if _word_addr(words[0]) == me:
                return ("the settle books the treasury's refund in owed (Owed(treasury)) instead "
                        "of paying it — the push failed, so nothing arrives")
        else:
            return (f"the sale emits an event that is not part of a {kind} "
                    f"({head[:10] or 'anonymous'})")
    floor = int(intent.min_native_inflow_wei or 0)
    if kind == KIND_CLAIM:
        if len(claimed) != 1 or claimed[0][0] != me:
            return (f"the simulation shows Claimed events {claimed or 'none'} — a claim must emit "
                    f"exactly one Claimed(treasury, amount)")
        if claimed[0][1] < floor:
            return (f"the sale pays {claimed[0][1]} wei, below the declared minimum {floor} — it "
                    f"does not pay what owed() said")
        return None
    _kind, ids = decode_call((tx or {}).get("data") or "")
    got = sorted(r for r, _refund in settled)
    if got != sorted(int(i) for i in ids):
        return (f"the simulation settles records {got}, not the declared {list(ids)} — one Settled "
                f"per record (a done record is skipped with no event)")
    refunds = sum(refund for _r, refund in settled)
    if refunds < floor:
        return (f"the settle refunds {refunds} wei in total, below the declared minimum {floor}")
    return None
