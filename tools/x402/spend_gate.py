"""Turn-origin gate for the x402 spend path (H1a, audit 2026-08-22).

`x402_fetch` is the ONE money verb that had no turn-origin refusal. Every other
one has it: the on-chain verbs through `core/wallet/tx_guard.py` step 2, the venue
order verbs through `tools/crypto_trade_gate.py::trade_turn_refusal`. So a forged
self-wake, an async-delegation-result re-entry, a leaf sub-agent, or an autonomous
goal run could auto-sign an EIP-3009 payment to a `payTo` the challenge named —
and asset-pin/network-pin bind the ASSET and CHAIN, never the RECIPIENT.

This module does turn origin ONLY. The owner kill-switch stays in
`tools/x402/service.py` where it already lives with its own message; duplicating
it here would give one condition two different refusal strings.

Pure policy, no closures — safe under `from __future__ import annotations`.
"""
from __future__ import annotations

from typing import Optional

# Module-level indirection so a test can monkeypatch the detector (and so a
# raising detector is provably handled). The real import is lazy inside, because
# tools.controller.action_registration is heavy and re-exports the predicate.
def _forged_fn(execution_context, tool_self) -> bool:
    from tools.controller.action_registration import _is_forged_or_autonomous_turn
    return bool(_is_forged_or_autonomous_turn(execution_context, tool_self))


def x402_spend_refusal(execution_context, tool_self) -> Optional[str]:
    """Refusal reason when this turn must NOT auto-pay — else ``None``.

    ``execution_context is None`` means a direct/programmatic/CLI call rather than
    an agent-loop turn, which is allowed (exact parity with
    ``crypto_trade_gate.trade_turn_refusal``; the caller's kill-switch still runs).

    Fails CLOSED on any probe error: if we cannot prove the turn is genuine, we
    refuse.
    """
    from core.wallet.authority import turn_refusal
    principal_error = turn_refusal(execution_context)
    if principal_error:
        return principal_error
    if execution_context is None:
        return None
    try:
        forged = _forged_fn(execution_context, tool_self)
    except Exception as exc:
        return (f"payment refused: could not prove the turn is genuine ({exc}); "
                f"failing closed")
    if forged:
        return ("payment refused: a forged/autonomous turn (self-wake, "
                "delegation-result, leaf, or autonomous run) cannot spend — "
                "the owner must drive payments")
    return None
