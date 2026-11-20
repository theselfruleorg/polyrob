"""The owner-queue exemption with the turn in view.

``spend_exemption`` (core) decides from the params alone. One more case needs
the turn: a GENUINE owner turn that transfers to an address that is ours (the
owner's configured address or the agent's own wallet) waits on no second tap —
it cannot pay a third party. Every other gate still holds: the forged/autonomous
bar, the leaf bar, the pause, tx_guard and the PolicyGate caps.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

#: The plain value transfers. A swap, bridge or contract call is not here: its
#: counterparty is a contract, not the recipient field.
SELF_TRANSFER_VERBS = frozenset({"defi_trade_transfer", "defi_trade_solana_transfer"})


def owner_self_transfer_exemption(action_name: str, params: Dict[str, Any],
                                  context) -> Optional[str]:
    if action_name not in SELF_TRANSFER_VERBS or context is None:
        return None
    from core.wallet.own_addresses import own_destination
    label = own_destination((params or {}).get("to"))
    if label is None:
        return None
    from core.security.owner_turn import is_genuine_owner_turn
    if not is_genuine_owner_turn(context):
        return None
    return f"owner transfer to {label} — no third party is paid"


def mandate_exemption(action_name: str, params: Dict[str, Any], context) -> Optional[str]:
    """122 W5 / 124 W5: a live owner mandate covers this act of an autonomous main-agent run —
    one use reserved, and the owner's grant stamped for this call so the money guard's card
    rules (payout, an unpinned account, a nesting) read it as the owner's approval. The caps,
    the pause and the simulation still bind."""
    from core.wallet.standing_grant import exemption_for_turn
    try:
        from core.wallet.simulation import _default_rpc_for
    except Exception:  # noqa: BLE001
        _default_rpc_for = None
    reason = exemption_for_turn(action_name, params, context, rpc_fn=_default_rpc_for)
    if reason:
        from tools.controller.approval import _stamp_owner_grant
        _stamp_owner_grant(context, params or {}, action=action_name)
        _name_the_mandate(context, reason)
    return reason


def _name_the_mandate(context, reason: str) -> None:
    """Name the mandate on the grant: the guard holds the act to the autonomous ``why`` rule
    and the act record names the mandate as its source (audit 2026-11-21 L-3)."""
    import re
    try:
        from core.wallet.tx_guard import OWNER_GRANT_KEY
        grant = (getattr(context, "metadata", None) or {}).get(OWNER_GRANT_KEY)
        m = re.match(r"mandate (m_[0-9a-f]+)", str(reason or ""))
        if isinstance(grant, dict) and m:
            grant["mandate"] = m.group(1)
    except Exception:  # noqa: BLE001 — a missing name never widens anything
        pass


def spend_exemption_with_turn(action_name: str, params: Dict[str, Any],
                              context=None) -> Optional[str]:
    from core.config_policy.spend_lane import spend_exemption
    return (spend_exemption(action_name, params)
            or owner_self_transfer_exemption(action_name, params, context)
            or mandate_exemption(action_name, params, context))


spend_exemption_with_turn.takes_context = True
#: 085 W2: the payout predicate prices a declared inflow (network on a cache miss).
spend_exemption_with_turn.off_loop = True
