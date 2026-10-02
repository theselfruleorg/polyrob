"""Which flags a same-process write may apply LIVE (026 P5, owner D3).

Every env flag used to answer "takes effect: restart", because no writer
touched ``os.environ``. The autonomy loop group is deliberately read at ACCESS
time (``policy.py``), so a live ``os.environ`` write DOES reach its gates —
the REPL can turn autonomy on without a restart.

v1 scope (owner decision D3, the proposal recommendation): the autonomy loop
group and the posture groups only, plus their two master axes
(``AUTONOMY_ENABLED``, ``AUTONOMY_POSTURE``). Never live, whatever a future
edit to the groups adds:

- a flag frozen at import (compute posture, the approval/payment snapshot);
- a MONEY flag (wallet, caps, x402, payment, trading, treasury, spend);
- an APPROVAL flag (whether a verb needs an owner tap);
- an INGRESS flag (who may reach the agent: surfaces, correspondents,
  allowlists, pairing, owner binding);
- ``AUTONOMY_MODE`` (it moves the payment-approval default) and
  ``POLYROB_LOCAL`` (the trust profile).

:func:`live_apply_allowed` applies the exclusions at call time too, so a
group edit that sweeps a money flag into the loop group still cannot make it
live. ``tests/unit/core/test_live_apply.py`` pins both.
"""
from __future__ import annotations

from typing import FrozenSet

from core.config_policy.autonomy_posture import _POSTURE_FULL_FLAGS
from core.config_policy.local_profile import _AUTONOMY_LOCAL_FLAGS

#: Name segments (split on ``_``) that mark a money / approval / ingress flag.
_NEVER_LIVE_SEGMENTS: FrozenSet[str] = frozenset({
    # money
    "WALLET", "USD", "CAP", "X402", "PAYMENT", "PAYMENTS", "TRADE", "TRADING",
    "DEFI", "SPEND", "TREASURY", "INVOICE", "INVOICING", "SETTLE",
    "SWAP", "HYPERLIQUID", "POLYMARKET", "CREDITS", "BILLING",
    # approval
    "APPROVAL", "APPROVALS", "GRANT",
    # ingress
    "SURFACE", "CORRESPONDENT", "INGRESS", "ALLOWLIST", "ALLOWED", "PAIRING",
    "WEBHOOK", "INBOUND", "GATEWAY",
})
_NEVER_LIVE_EXACT: FrozenSet[str] = frozenset({
    "AUTONOMY_MODE", "POLYROB_LOCAL", "ROB_LOCAL", "POLYROB_OWNER_USER_ID",
    "AGENT_COMPUTE_POSTURE", "OUTBOUND_POLICY",
    # the cross-chain bridge (a "continuity bridge" is a memory feature, so the
    # segment alone cannot decide it)
    "BRIDGE_WATCHER_ENABLED",
})

#: The v1 live-apply set (D3): the autonomy loop + posture groups + masters.
LIVE_APPLY_SAFE: FrozenSet[str] = frozenset(
    set(_AUTONOMY_LOCAL_FLAGS) | set(_POSTURE_FULL_FLAGS)
    | {"AUTONOMY_ENABLED", "AUTONOMY_POSTURE"}
)


def never_live(key: str) -> bool:
    """True for a flag no writer may ever apply live (see module docstring)."""
    name = str(key or "").strip().upper()
    if not name or name in _NEVER_LIVE_EXACT:
        return True
    if set(name.split("_")) & _NEVER_LIVE_SEGMENTS:
        return True
    try:
        from core.config_service import _IMPORT_FROZEN_FLAGS
        if name in _IMPORT_FROZEN_FLAGS:
            return True
    except Exception:            # pragma: no cover — fail CLOSED
        return True
    return False


def live_apply_allowed(key: str) -> bool:
    """Whether a same-process write of ``key`` may also set ``os.environ``."""
    return str(key or "") in LIVE_APPLY_SAFE and not never_live(key)


__all__ = ["LIVE_APPLY_SAFE", "live_apply_allowed", "never_live"]
