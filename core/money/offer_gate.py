"""The offer step of the money kernel (099 W2, §3.2) — an order's spend envelope.

A fulfillment goal carries ``metadata["mandate"] = <order id>`` (the ``ord_``
prefix). For such a run ``core.money.authorize.authorize_spend`` (step 4c) asks
:func:`refusal` before ANY turn-origin-checked spend, and the guard supplies
the facts it measured (``SpendIntent.offer_scope``):

* the order is ``fulfilling`` (paid, not yet delivered) and belongs to the run's
  tenant — one order, one fulfillment;
* every destination is the order's ``recipient`` input or a contract RESOURCE
  PINNED to the order's confirmed offer version (``store.pinned_contract`` — a
  resource added after the owner's Confirm has no authority);
* a zero-value resource call (``TxIntent.is_resource_call``) addresses a
  resource contract, never anything else; ANY call to a resource contract (with
  value or a token spend too) uses a function the owner pinned;
* the spend fits the confirmed per-order ``spend_usd`` budget: each admitted
  spend RESERVES its declared maximum (a CAS; never given back — the safe side).

What it is NOT: a way in for any other run. A run without an order id never
reaches this step, and a self-wake or forged turn is refused before it (step 3).

Tier-0: imports nothing above ``core``.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from core.commerce.orders import ORDER_PREFIX, is_order_id

NO_SCOPE_TEXT = ("refused: this run fulfils an order, and an order spends only through the "
                 "guarded on-chain verbs (the guard measures where the value goes)")


def order_recipient(order_id: str) -> Optional[str]:
    """The order's ``recipient`` input (lower-case address), or ``None``."""
    from core.commerce import orders as ords
    from core.commerce import store as st
    o = ords.get(order_id)
    if o is None:
        return None
    m = st.live_version(o.offer_id, o.offer_version)
    field = m.role_field("recipient") if m else None
    v = (o.inputs or {}).get(field) if field else None
    return str(v).lower() if v else None


def _run_goal(session_id: Optional[str]) -> Optional[str]:
    from core.autonomy_marker import goal_for_session
    return goal_for_session(session_id)


def refusal(order_id: str, *, user_id: str, scope: Optional[Mapping[str, Any]],
            usd: Optional[float], dry_run: bool = False,
            key: Optional[str] = None, session_id: Optional[str] = None) -> Optional[str]:
    """``None`` = inside the order's envelope; else the refusal sentence."""
    from core.commerce import orders as ords
    from core.commerce import store as st
    if not is_order_id(order_id):
        return f"refused: {order_id!r} is not an order"
    if not scope:
        return NO_SCOPE_TEXT
    o = ords.get(order_id)
    if o is None:
        return f"refused: order {order_id} does not exist"
    if str(o.user_id) != str(user_id or ""):
        return f"refused: order {order_id} belongs to another tenant"
    if o.status != ords.FULFILLING:
        return (f"refused: order {order_id} is {o.status} — only an order being fulfilled may "
                f"spend (one order, one fulfillment)")
    # One order, ONE run: a retry re-opens the order with a new goal, and the old
    # goal (if it still runs) may no longer spend from the shared envelope.
    if not o.goal_id or _run_goal(session_id) != o.goal_id:
        return (f"refused: this run is not order {order_id}'s current fulfillment goal")
    m = st.live_version(o.offer_id, o.offer_version)
    if m is None:
        return f"refused: the confirmed version of offer {o.offer_id} cannot be read"
    chain = str(scope.get("chain") or "").lower()
    dests = [str(d).lower() for d in (scope.get("destinations") or ()) if d]
    recipient = order_recipient(order_id)
    for d in dests:
        if recipient and d == recipient:
            continue
        if st.pinned_contract(o.offer_id, o.offer_version, chain, d) is not None:
            continue
        return (f"refused: {d} is neither this order's recipient nor a contract resource of "
                f"offer {o.offer_id} on {chain or '?'} — an order pays nobody else")
    # The function pin binds EVERY call to a pinned contract, not only the zero-value
    # resource-call shape: a call with value or a declared spend_token would otherwise
    # reach any function of the offer's contract (transferOwnership, withdraw…).
    to = str(scope.get("contract") or "").lower()
    sels = st.pinned_contract(o.offer_id, o.offer_version, chain, to) if to else None
    if scope.get("is_resource_call") or sels is not None:
        if sels is None:
            return (f"refused: a resource call must address a contract resource of offer "
                    f"{o.offer_id}; {to or '(none)'} is not one")
        sel = str(scope.get("selector") or "").lower()
        if sel not in sels:
            return (f"refused: function {sel or '(none)'} is not one the owner confirmed for "
                    f"{to} (only {', '.join(sels)})")
    if usd is None:
        return "refused: the spend has no USD bound, so the order's envelope cannot hold it"
    if dry_run:
        remaining = o.spend_budget - o.spent_usd
        return None if float(usd) <= remaining + 1e-9 else (
            f"refused: ${float(usd):.2f} exceeds order {order_id}'s remaining budget "
            f"${max(remaining, 0):.2f}")
    if not ords.reserve_spend(order_id, float(usd), key=key or scope.get("key")):
        return (f"refused: ${float(usd):.2f} would exceed order {order_id}'s spend budget "
                f"(${o.spend_budget:.2f}, ${o.spent_usd:.2f} already reserved)")
    return None


def register() -> None:
    """Hand the kernel its offer step. Called by the composition root
    (``polyrob.app.commerce_wiring``) — a process that never registers it refuses
    every order run's spend (fail closed), and core imports nothing of commerce."""
    from core.money import hooks
    hooks.register_offer_gate(refusal)
    hooks.register_offer_recipient(order_recipient)

__all__ = ["NO_SCOPE_TEXT", "ORDER_PREFIX", "order_recipient", "refusal", "register"]
