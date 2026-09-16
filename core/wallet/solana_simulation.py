"""Solana pre-broadcast simulation and the SPL threat taxonomy. Phase 3.

`core/wallet/simulation.py` measures what an EVM transaction *actually does* by
bundling it through `eth_simulateV1` and reading balance and allowance deltas.
The CONCEPT ports; the implementation does not, and neither does the threat
model it was built around.

## Why the EVM defense does not translate

`tx_guard` step 6 refuses an undeclared ERC-20 `Approval`, because a standing
allowance is a future drain the current transaction does not perform. On Solana
there is usually no allowance at all — Jupiter needs none — so
`approve -> swap -> revoke` is meaningless and a port of that check would guard
a door nobody uses while the real ones stand open.

SPL's drain vectors are different **in kind**:

* **`SetAuthority` on the account owner** — the token account stops being ours.
  Nothing is transferred; ownership simply moves. No EVM analogue exists.
* **`CloseAccount`** — drains the balance *and* reclaims the rent lamports.
* **A `delegate`** — the closest thing to an allowance, and still not the same:
  it is per token-account, not per (token, spender).
* **Freeze** — a frozen account cannot be sold. The Solana shape of a honeypot,
  and it can be applied AFTER we buy.

So the invariant is re-derived rather than translated: *"a hidden approve is a
future drain"* becomes *"a hidden authority change, account close, or freeze is
a future drain"*. `authority_grants` is what `expected_allowance_grants` is on
the EVM side — the thing a caller must DECLARE, so that anything undeclared can
be refused.

## Rent

`tx_guard` refuses native movement above dust, because on EVM an unexplained ETH
outflow is a drain. On Solana a first-time token transfer legitimately spends
~0.002 SOL creating the recipient's associated token account. That rule would
refuse every first transfer to a new counterparty, so rent must be *classified*,
not banned — see `is_plausible_rent`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: Rent-exemption for a 165-byte SPL token account is ~0.00204 SOL. The ceiling
#: is generous (a few accounts' worth) because the point is to separate "rent"
#: from "someone is draining the SOL balance", not to price rent exactly.
MAX_PLAUSIBLE_RENT_LAMPORTS = 10_000_000          # 0.01 SOL

SPL_TOKEN_PROGRAMS = frozenset({
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",          # SPL Token
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",          # Token-2022
})


@dataclass(frozen=True)
class SolanaDeltas:
    """What the simulation says the transaction actually did.

    ``ok=False`` means nothing may be asserted from it and the caller must
    refuse — a simulation that did not run is not a simulation that passed.
    """
    ok: bool
    reason: str = ""
    native_delta: int = 0                     # lamports
    token_deltas: Dict[str, int] = field(default_factory=dict)
    #: Re-derived from the EVM ``allowance_deltas``. Tuples of
    #: ``(kind, mint, party)`` where kind is one of ``delegate``,
    #: ``owner_changed``, ``close_authority``, ``frozen``, ``closed``.
    authority_grants: Tuple[tuple, ...] = ()
    compute_units: Optional[int] = None

    def grants_authority(self) -> bool:
        """The Solana analogue of ``Deltas.grants_allowance``."""
        return bool(self.authority_grants)


def is_plausible_rent(native_delta: int) -> bool:
    """Is this native outflow explainable as account rent + fees?

    Only ever a CLASSIFICATION, never a permission: the caller still bounds the
    USD value. It exists so that creating a recipient's token account — an
    ordinary, necessary act — is not mistaken for a drain.
    """
    if native_delta >= 0:
        return True
    return abs(int(native_delta)) <= MAX_PLAUSIBLE_RENT_LAMPORTS


def _parsed(entry: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(entry, dict):
        return None
    data = entry.get("data")
    if not isinstance(data, dict):
        return None
    parsed = data.get("parsed")
    if not isinstance(parsed, dict):
        return None
    info = parsed.get("info")
    return info if isinstance(info, dict) else None


def _is_ours(info: Dict[str, Any], owner: str) -> bool:
    return str(info.get("owner") or "") == owner


def _amount(info: Dict[str, Any]) -> Optional[int]:
    try:
        return int(info["tokenAmount"]["amount"])
    except (KeyError, TypeError, ValueError):
        return None


def parse_deltas(sim: Any, *, owner: str,
                 owned_pubkeys: Sequence[str],
                 ours: Optional[Sequence[str]] = None) -> SolanaDeltas:
    """Turn a ``simulateTransaction`` result into asserted deltas.

    Pure: the caller supplies the RPC result and the pre-state, so this is
    testable without a network. ``sim["_pre"]`` carries the pre-state account
    list in the same order as the post-state ``value.accounts``.

    ⚠️ ``owned_pubkeys`` is REQUIRED, not optional, and that is deliberate.
    Native lamport accounting only runs for accounts the caller names as ours,
    so an omitted list does not degrade the answer — it silently returns
    ``native_delta = 0``, which every downstream check reads as a MEASURED
    "no SOL moved". `simulate()` omitted it, so on prod (2026-09-08) every SOL
    sell refused with "no SOL leaving" while `is_plausible_rent(0)` returned
    True on every transaction, leaving the drain assertion permanently
    disarmed. That is the EVM bug this port was supposed to avoid — see
    ``core/wallet/simulation.py``: "dead code shaped like a defense". Passing
    an EMPTY list is still allowed, but now it has to be typed on purpose.

    ⚠️ ``owned_pubkeys`` is INDEX-ALIGNED with ``_pre``/``value.accounts`` — it
    names WHICH account each position is. ``ours`` says which of those are the
    wallet's. They are different questions, and conflating them was a live money
    bug (prod 2026-09-11, found by the first bridge dry run): the native sum ran
    for every position as long as the list was non-empty, so a transfer from the
    owner INTO a counterparty account in the same list cancelled itself. Owner
    -900,005,000 plus Relay vault +900,000,000 was reported as -5,000 — "only
    the fee moved" about a transaction moving 0.9 SOL, with the drain assertion
    passing. ``ours=None`` keeps the old behaviour (every named account counts)
    so existing callers are unchanged.
    """
    if not isinstance(sim, dict):
        return SolanaDeltas(False, "no simulation result")
    value = sim.get("value")
    if not isinstance(value, dict):
        return SolanaDeltas(False, "simulation returned no value block")
    if value.get("err") is not None:
        return SolanaDeltas(False, f"simulation reverted: {value['err']}")

    pre: List[Any] = list(sim.get("_pre") or [])
    post: List[Any] = list(value.get("accounts") or [])
    owned = set(owned_pubkeys or ())
    aligned: List[str] = list(owned_pubkeys or ())
    ours_set = owned if ours is None else set(ours)

    native_delta = 0
    token_deltas: Dict[str, int] = {}
    grants: List[tuple] = []

    for index, after in enumerate(post):
        before = pre[index] if index < len(pre) else None

        # --- native lamports, for accounts the caller says are ours ---------
        # The position must map to one of OUR pubkeys. Without this the sum ran
        # for every account in the list and a counterparty's inflow cancelled
        # our outflow (see the docstring).
        _addr = aligned[index] if index < len(aligned) else None
        if owned and _addr in ours_set and \
                isinstance(before, dict) and isinstance(after, dict):
            if _parsed(before) is None and _parsed(after) is None:
                try:
                    native_delta += int(after.get("lamports", 0)) - int(before.get("lamports", 0))
                except (TypeError, ValueError):
                    pass

        info_before = _parsed(before)
        info_after = _parsed(after)

        # --- an account of OURS that vanished = closed ----------------------
        if info_before is not None and _is_ours(info_before, owner) and after is None:
            grants.append(("closed", str(info_before.get("mint") or "?"), owner))
            continue
        # An account CREATED by this transaction has no pre-entry at all. That
        # is the ordinary shape of a FIRST trade — a wallet that has never held
        # a token has no associated token account, and the swap creates one — so
        # skipping it made every first trade invisible to the delta check, which
        # then refused it as "no token movement".
        if info_before is None and info_after is not None:
            if _is_ours(info_after, owner):
                amount = _amount(info_after)
                if amount:
                    mint = str(info_after.get("mint") or "?")
                    token_deltas[mint] = token_deltas.get(mint, 0) + amount
            continue
        if info_before is None or info_after is None:
            continue

        was_ours = _is_ours(info_before, owner)
        if not was_ours:
            continue                     # someone else's account is not our delta
        mint = str(info_before.get("mint") or "?")

        # --- balance --------------------------------------------------------
        a, b = _amount(info_before), _amount(info_after)
        if a is not None and b is not None and b != a:
            token_deltas[mint] = token_deltas.get(mint, 0) + (b - a)

        # --- the taxonomy ---------------------------------------------------
        if not _is_ours(info_after, owner):
            # SetAuthority moved the account out from under us. The balance may
            # not have changed at all, which is precisely why a balance-only
            # check would miss it.
            grants.append(("owner_changed", mint, str(info_after.get("owner") or "?")))
        new_delegate = info_after.get("delegate")
        if new_delegate and new_delegate != info_before.get("delegate"):
            grants.append(("delegate", mint, str(new_delegate)))
        new_close = info_after.get("closeAuthority")
        if new_close and new_close != info_before.get("closeAuthority"):
            grants.append(("close_authority", mint, str(new_close)))
        if (str(info_after.get("state") or "") == "frozen"
                and str(info_before.get("state") or "") != "frozen"):
            grants.append(("frozen", mint, str(info_after.get("owner") or "?")))

    return SolanaDeltas(
        ok=True, native_delta=native_delta, token_deltas=token_deltas,
        authority_grants=tuple(grants),
        compute_units=value.get("unitsConsumed"))
