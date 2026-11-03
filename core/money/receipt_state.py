"""The receipt outcome of a booked money transaction (098 close-out TS-23).

A money verb books its broadcast on the spend ledger before the outcome is
known when the receipt wait ends early: a cancel / Ctrl-C during the wait
(``tools/defi/receipt_wait.py``) or a ``pending`` receipt after the timeout.
That entry carries ``receipt_state = "pending_receipt"``; a resolver
(``core/wallet/receipt_resolver.py``) reads the receipt later and appends ONE
``action = "receipt"`` entry with the final state:

    pending_receipt ──▶ confirmed   (status 1)
                    ├─▶ reverted    (status 0: the entry no longer counts
                    │                against the rolling caps — voided)
                    └─▶ unknown     (no receipt after 24 h: the owner is told)

The audit sink is append-only, so the state of a tx is the newest receipt
entry for its ``result_ref``. Pure: no I/O.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Set

from core.state.machine import StateMachine

PENDING = "pending_receipt"

TX_RECEIPT = StateMachine(
    name="money.tx_receipt",
    states=frozenset({PENDING, "confirmed", "reverted", "unknown"}),
    transitions={PENDING: frozenset({"confirmed", "reverted", "unknown"})},
    terminal=frozenset({"confirmed", "reverted", "unknown"}),
    initial=frozenset({PENDING}),
)

#: A pending receipt older than this is given up as ``unknown`` (owner notice).
UNKNOWN_AFTER_SEC = 24 * 3600


def receipt_states(entries: Iterable[dict]) -> Dict[str, str]:
    """``result_ref -> state`` for every tx booked with a receipt state."""
    out: Dict[str, str] = {}
    for e in entries:
        ref, state = e.get("result_ref"), e.get("receipt_state")
        if not ref or not state:
            continue
        if e.get("action") == "receipt" or ref not in out:
            out[str(ref)] = str(state)
    return out


def voided_refs(entries: Iterable[dict]) -> Set[str]:
    """The tx refs a reverted receipt took out of the rolling caps."""
    return {ref for ref, state in receipt_states(entries).items() if state == "reverted"}


def open_receipts(entries: List[dict]) -> List[dict]:
    """The booked entries whose receipt is still ``pending_receipt``."""
    states = receipt_states(entries)
    return [e for e in entries if e.get("action") != "receipt"
            and e.get("receipt_state") == PENDING
            and states.get(str(e.get("result_ref"))) == PENDING]


def receipt_entry(entry: dict, state: str, *, current: str, ts: float,
                  block: Optional[int] = None) -> Optional[dict]:
    """The ``action = "receipt"`` entry that moves ``entry`` from ``current``
    (its state in :func:`receipt_states`) to ``state``, or None when the table
    forbids the edge (a second resolution)."""
    if not TX_RECEIPT.check(current, state, ctx={"ref": entry.get("result_ref")}):
        return None
    out = {"ts": float(ts), "venue": entry.get("venue"), "action": "receipt",
           "amount_usd": 0.0, "counterparty": None, "idempotency_key": None,
           "result_ref": entry.get("result_ref"), "chain": entry.get("chain"),
           "asset": None, "receipt_state": state}
    if block is not None:
        out["block"] = int(block)
    return out
