"""Live-trade kill-switch for the crypto trading tools (T11).

A real order is submitted to a venue ONLY when ALL hold:
  - the master switch ``CRYPTO_TRADE_LIVE_ENABLED`` is on, AND
  - the per-venue switch (``POLYMARKET_TRADING_ENABLED`` / ``HYPERLIQUID_TRADING_ENABLED``)
    is on, AND
  - the order size is within the per-venue live cap
    (``POLYMARKET_TRADE_MAX_USD`` / ``HYPERLIQUID_TRADE_MAX_USD``, default $5).

Otherwise the trade tools dry-run: build/validate/route through PolicyGate but never
submit. All switches default OFF, so the default posture is "never trade real money."
This is ANDed with — never a replacement for — TradingLimits, PolicyGate, exposure caps
and the confirmation gate.

A non-order venue MUTATION (cancel, leverage change — ``evaluate_live_mutation``, M10)
is gated by the same master + per-venue switches but NOT the size cap (a cancel has no
amount). ``risk_reducing=True`` does NOT mean "this cannot increase risk" — cancelling a
protective stop plainly can (cancelling a resting stop-loss, or ``cancel_all_orders``
wiping every protective order at once, both raise exposure). It means "this is a
position-management action the OWNER may still perform BY HAND while halted", so pulling
the kill-switch can never strand a position the owner can no longer close. It is an
OWNER-only carve-out (R16, 2026-08-22): it applies only to a direct/CLI/programmatic call
(``execution_context is None`` in ``trade_turn_refusal``) — it never relaxes anything for
an agent-loop turn, including a genuine, non-forged main-agent turn. A leverage change
(not a position-management action) is blocked while halted like an order, for anyone.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from core.env import bool_env as _bool_env
from core.env import float_env

_VENUE_FLAGS = {
    "polymarket": "POLYMARKET_TRADING_ENABLED",
    "hyperliquid": "HYPERLIQUID_TRADING_ENABLED",
}
_VENUE_CAPS = {
    "polymarket": "POLYMARKET_TRADE_MAX_USD",
    "hyperliquid": "HYPERLIQUID_TRADE_MAX_USD",
}
DEFAULT_LIVE_CAP_USD = 5.0


@dataclass(frozen=True)
class TradeGateDecision:
    live: bool      # True => submit to the venue; False => dry-run / blocked
    reason: str


def trade_turn_refusal(execution_context, tool_self, *, risk_reducing: bool = False) -> str | None:
    """H11: return a refusal reason if this turn must NOT run a value-moving / mutating
    trade verb — else None. Two independent bars (either one refuses):

      1. The owner kill-switch (``AutonomyConfig.autonomy_halted()``) halts ALL agent
         trading, exactly like x402 spend (``x402/service.py``). Applies regardless of
         turn origin (a direct/CLI call is halted too) — UNLESS BOTH ``risk_reducing=True``
         AND ``execution_context is None`` (R16, 2026-08-22, narrowing M10): the ONLY
         rationale for this carve-out is that the OWNER, acting directly (CLI/programmatic
         call — see bar 2's note on what ``None`` means), must still be able to close a
         position by hand while halted. Nothing in that rationale extends to an agent-loop
         turn — an ``execution_context`` being present means this is an agent-loop turn
         (LLM-driven, whether genuine-owner-initiated or not), and the kill-switch bar
         applies to it exactly as before, `risk_reducing` or not. A genuine, non-forged
         main-agent turn (``role="orchestrator"``) gets ZERO relaxation here — only a
         literal ``None`` context does. Every verb other than the four cancels keeps the
         default ``risk_reducing=False`` and is fully unaffected by this bar's narrowing.
      2. A forged self-wake / async-delegation-result / leaf / autonomous turn can never
         place/mutate an order — parity with the owner-queue payment approver
         (``approval_queue.py``). The trade methods historically took no
         ``execution_context`` so origin was invisible; a ``None`` context means a
         direct/programmatic/CLI call (not an agent-loop turn), so the forged check is
         skipped there (flag + cap gates still apply) — but the kill-switch above STILL
         applies (subject to the bar-1 owner-only carve-out). This bar is UNCONDITIONAL —
         ``risk_reducing`` never weakens it; a forged turn still cannot cancel an order.

    Fail CLOSED on every probe error (money-safe): if we cannot prove the turn is
    genuine, or cannot read the kill-switch, we refuse."""
    # (1) Owner kill-switch — refuse ALL trading while halted (parity with x402_fetch),
    # except a risk-reducing mutation made by the OWNER directly (R16: `execution_context
    # is None` — an agent-loop turn, even a genuine one, is NEVER exempt from this bar).
    if not (risk_reducing and execution_context is None):
        try:
            from core.config_policy import AutonomyConfig
            if AutonomyConfig.autonomy_halted():
                return ("live trade refused: autonomy is HALTED (owner kill-switch) — "
                        "the order was not submitted")
        except Exception as e:
            return f"live trade refused: kill-switch probe failed ({e}); failing closed"
    # (2) Forged / autonomous turn origin — only meaningful when a context is present.
    if execution_context is None:
        from core.wallet.authority import turn_refusal
        return turn_refusal(None)
    try:
        from tools.controller.action_registration import _is_forged_or_autonomous_turn
        forged = _is_forged_or_autonomous_turn(execution_context, tool_self)
    except Exception:
        forged = True  # cannot prove the turn is genuine → refuse
    if forged:
        return ("live trade refused: a forged/autonomous turn (self-wake, "
                "delegation-result, leaf, or autonomous run) cannot place orders — "
                "the owner must drive trades")
    from core.wallet.authority import turn_refusal
    principal_error = turn_refusal(execution_context)
    if principal_error:
        return principal_error

    return None


def evaluate_live_trade(venue: str, amount_usd: float | None) -> TradeGateDecision:
    """Decide whether an order may be submitted live, or must dry-run."""
    if not _bool_env("CRYPTO_TRADE_LIVE_ENABLED", False):
        return TradeGateDecision(False, "live trading disabled (CRYPTO_TRADE_LIVE_ENABLED off) — dry-run")

    flag = _VENUE_FLAGS.get(venue)
    if not flag or not _bool_env(flag, False):
        return TradeGateDecision(False, f"live trading disabled for {venue} ({flag} off) — dry-run")

    # core.env.float_env already clamps unset/blank/unparsable/non-finite to the
    # default; the M10 `<= 0` clamp stays EXPLICIT here (a zero/negative cap would
    # silently void `amount > cap`, never arm it on garbage).
    cap = float_env(_VENUE_CAPS.get(venue, ""), DEFAULT_LIVE_CAP_USD)
    cap = DEFAULT_LIVE_CAP_USD if cap <= 0 else cap
    # M10: an unpriceable order (amount_usd is None) or a non-finite amount can't be
    # checked against the cap — fail CLOSED (dry-run), never arm live on an unknown value.
    if amount_usd is None or not math.isfinite(amount_usd):
        return TradeGateDecision(False, f"order value could not be determined for {venue} — dry-run (fail-closed)")
    if amount_usd > cap:
        return TradeGateDecision(False, f"order ${amount_usd:.2f} exceeds the {venue} live cap ${cap:.2f} — blocked")

    # H5: the owner kill-switch halts ALL agent spend — including live trades, not just
    # x402 payments. Checked only on the would-be-live path; fail CLOSED (a probe error
    # blocks the trade). This is the single central seam that covers both venues.
    try:
        from core.config_policy import AutonomyConfig
        if AutonomyConfig.autonomy_halted():
            return TradeGateDecision(False, f"live trade refused: autonomy is HALTED (owner kill-switch) — {venue} order not submitted")
    except Exception as e:
        return TradeGateDecision(False, f"live trade refused: kill-switch probe failed ({e}) — failing closed")

    return TradeGateDecision(True, f"live trading enabled for {venue} (within ${cap:.2f} cap)")


def evaluate_live_mutation(venue: str, *, risk_reducing: bool) -> TradeGateDecision:
    """Whether a non-order venue MUTATION (cancel, leverage change) may go live.

    M10 (audit 2026-08-22): `cancel_*` and `update_leverage` reached the venue
    whenever credentials allowed it, even with CRYPTO_TRADE_LIVE_ENABLED off —
    contradicting this module's stated contract.

    Deliberately NOT `evaluate_live_trade`: that one fails CLOSED on an
    unpriceable amount, and a cancel has no amount, so reusing it would block
    every cancel and could strand an open position the owner can no longer close.

    `risk_reducing=True` is NOT a claim that the action cannot increase risk —
    cancelling a protective stop plainly can (R16, 2026-08-22, correcting the
    original M10 framing). It means "the owner may still perform this by hand
    while halted." This function has no `execution_context` and cannot itself
    tell an owner-direct call from an agent-loop one — that narrowing is
    `trade_turn_refusal`'s job (its bar 1 only lifts for `risk_reducing=True`
    AND `execution_context is None`). Callers MUST run `trade_turn_refusal`
    first; this function alone does not restrict `risk_reducing=True` to the
    owner.
    """
    if not _bool_env("CRYPTO_TRADE_LIVE_ENABLED", False):
        return TradeGateDecision(False, "live trading disabled (CRYPTO_TRADE_LIVE_ENABLED off)")
    flag = _VENUE_FLAGS.get(venue)
    if not flag or not _bool_env(flag, False):
        return TradeGateDecision(False, f"live trading disabled for {venue} ({flag} off)")
    if risk_reducing:
        # Deliberately returns here WITHOUT probing the halt switch (Minor 6, R16):
        # this function only encodes the switches+cap policy, not turn origin, so the
        # halt decision for a risk-reducing mutation is entirely `trade_turn_refusal`'s
        # (owner-only) responsibility — probing it again here would be redundant and,
        # since this function can't see `execution_context`, could not be narrowed to
        # the owner anyway. A raising halt probe therefore never refuses a cancel HERE;
        # it is still enforced upstream for every agent-loop turn.
        return TradeGateDecision(True, f"risk-reducing mutation permitted for {venue}")
    try:
        from core.config_policy import AutonomyConfig
        if AutonomyConfig.autonomy_halted():
            return TradeGateDecision(
                False, f"refused: autonomy is HALTED (owner kill-switch) — {venue}")
    except Exception as e:
        return TradeGateDecision(False, f"refused: kill-switch probe failed ({e}) — failing closed")
    return TradeGateDecision(True, f"mutation permitted for {venue}")
