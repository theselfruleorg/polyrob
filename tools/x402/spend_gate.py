"""Turn-origin gate for the x402 spend path (H1a, audit 2026-08-22).

`x402_fetch` is the ONE money verb that had no turn-origin refusal. Every other
one has it: the on-chain verbs through `core/wallet/tx_guard.py` step 2, the venue
order verbs through `polyrob_markets/trade_gate.py::trade_turn_refusal` (the markets pack). So a forged
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


def _autonomous_ok_fn(execution_context, tool_self) -> bool:
    """068 X4: the ONE forged-shaped origin that may pay — a goal/cron-dispatched
    turn on the MAIN agent (the strict detector ``tx_guard`` uses). A leaf, a
    sub-agent, a self-wake and a delegation-result re-entry stay refused."""
    from tools.controller.turn_origin import _is_autonomous_goal_turn
    return bool(_is_autonomous_goal_turn(execution_context, tool_self))


def _autonomous_lane_refusal(max_amount_usd) -> Optional[str]:
    """An autonomous payment above ``X402_AUTONOMOUS_MAX_USD`` is paid only
    after the owner approved it, and the approval is the owner-queue HOOK that
    runs before this tool (``core/verb_policy_rows.py`` x402 lane,
    ``spend_lane._x402_exemption``). If that hook is not guaranteed for this
    verb, refuse the above-ceiling payment here instead of paying it silently."""
    try:
        import os
        from core.wallet.config import _load_per_venue_caps
        if _load_per_venue_caps(os.environ).get("x402") is None:
            return "payment refused: autonomous x402 payments require an explicit WALLET_VENUE_DAILY_CAP_X402_USD"
        from core.config_policy import PAYMENT_APPROVAL_TOOLS
        from core.config_policy.spend_lane import x402_autonomous_ceiling_usd
        ceiling = x402_autonomous_ceiling_usd()
        hooked = "x402_pay_x402_fetch" in set(PAYMENT_APPROVAL_TOOLS)
    except Exception as exc:
        return f"payment refused: the autonomous x402 lane could not be resolved ({exc}); failing closed"
    try:
        amount = float(max_amount_usd)
    except (TypeError, ValueError):
        return "payment refused: an autonomous payment must declare max_amount_usd"
    if amount <= ceiling or hooked:
        return None
    return (f"payment refused: ${amount:.2f} is above the ${ceiling:.2f} autonomous "
            f"x402 limit and no owner-approval gate covers this verb")


def _turn_bars():
    from core.money.authorize import SpendIntent
    return SpendIntent(tool="x402_pay", leaf=False, turn=True, pause=False)


def x402_spend_refusal(execution_context, tool_self, *,
                       max_amount_usd=None) -> Optional[str]:
    """Refusal reason when this turn must NOT auto-pay — else ``None``.

    ``execution_context is None`` means a direct/programmatic/CLI call rather than
    an agent-loop turn, which is allowed (exact parity with
    ``crypto_trade_gate.trade_turn_refusal``; the caller's kill-switch still runs).

    Fails CLOSED on any probe error: if we cannot prove the turn is genuine, we
    refuse.
    """
    # 067 P1b: the kernel runs principal -> forged turn (no leaf, no pause:
    # the kill-switch stays in the service with its own sentence); the x402
    # sentences are mapped from the deciding step.
    from core.money.authorize import authorize_spend
    # 068 X4: an autonomous goal/cron turn is ADMITTED (autonomous_ok_fn). Below
    # X402_AUTONOMOUS_MAX_USD it pays and reports; above it the owner-queue hook
    # has already held it for the owner's tap before this tool ran. The daily
    # WALLET_VENUE_DAILY_CAP_X402_USD is the PolicyGate check in the service.
    verdict = authorize_spend(
        _turn_bars(), execution_context, tool_self=tool_self,
        forged_fn=lambda ctx, tool: _forged_fn(ctx, tool),
        autonomous_ok_fn=lambda ctx, tool: _autonomous_ok_fn(ctx, tool))
    if not verdict.refused:
        if verdict.autonomous:
            return _autonomous_lane_refusal(max_amount_usd)
        return None
    if verdict.step == "turn_probe":
        return (f"payment refused: could not prove the turn is genuine ({verdict.detail}); "
                f"failing closed")
    if verdict.step == "turn":
        return ("payment refused: a forged turn (self-wake, delegation-result, "
                "leaf or sub-agent) cannot spend — only the owner, or an "
                "autonomous goal/cron run on the main agent, may pay")
    return verdict.reason
