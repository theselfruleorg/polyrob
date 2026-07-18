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
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass

from core.env import bool_env as _bool_env

_VENUE_FLAGS = {
    "polymarket": "POLYMARKET_TRADING_ENABLED",
    "hyperliquid": "HYPERLIQUID_TRADING_ENABLED",
}
_VENUE_CAPS = {
    "polymarket": "POLYMARKET_TRADE_MAX_USD",
    "hyperliquid": "HYPERLIQUID_TRADE_MAX_USD",
}
DEFAULT_LIVE_CAP_USD = 5.0


def _float_env(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    # M10: a non-finite cap (nan/inf) makes `amount > cap` always False, silently
    # voiding the per-trade cap — clamp garbage back to the safe default.
    if not math.isfinite(value) or value <= 0:
        return default
    return value


@dataclass(frozen=True)
class TradeGateDecision:
    live: bool      # True => submit to the venue; False => dry-run / blocked
    reason: str


def trade_turn_refusal(execution_context, tool_self) -> str | None:
    """H11: return a refusal reason if this turn must NOT run a value-moving / mutating
    trade verb — else None. Two independent bars (either one refuses):

      1. The owner kill-switch (``AutonomyConfig.autonomy_halted()``) halts ALL agent
         trading, exactly like x402 spend (``x402/service.py``). Applies regardless of
         turn origin (a direct/CLI call is halted too).
      2. A forged self-wake / async-delegation-result / leaf / autonomous turn can never
         place/mutate an order — parity with the owner-queue payment approver
         (``approval_queue.py``). The trade methods historically took no
         ``execution_context`` so origin was invisible; a ``None`` context means a
         direct/programmatic/CLI call (not an agent-loop turn), so the forged check is
         skipped there (flag + cap gates still apply) — but the kill-switch above STILL
         applies.

    Fail CLOSED on every probe error (money-safe): if we cannot prove the turn is
    genuine, or cannot read the kill-switch, we refuse."""
    # (1) Owner kill-switch — refuse ALL trading while halted (parity with x402_fetch).
    try:
        from core.config_policy import AutonomyConfig
        if AutonomyConfig.autonomy_halted():
            return ("live trade refused: autonomy is HALTED (owner kill-switch) — "
                    "the order was not submitted")
    except Exception as e:
        return f"live trade refused: kill-switch probe failed ({e}); failing closed"
    # (2) Forged / autonomous turn origin — only meaningful when a context is present.
    if execution_context is None:
        return None
    try:
        from tools.controller.action_registration import _is_forged_or_autonomous_turn
        forged = _is_forged_or_autonomous_turn(execution_context, tool_self)
    except Exception:
        forged = True  # cannot prove the turn is genuine → refuse
    if forged:
        return ("live trade refused: a forged/autonomous turn (self-wake, "
                "delegation-result, leaf, or autonomous run) cannot place orders — "
                "the owner must drive trades")
    return None


def evaluate_live_trade(venue: str, amount_usd: float | None) -> TradeGateDecision:
    """Decide whether an order may be submitted live, or must dry-run."""
    if not _bool_env("CRYPTO_TRADE_LIVE_ENABLED", False):
        return TradeGateDecision(False, "live trading disabled (CRYPTO_TRADE_LIVE_ENABLED off) — dry-run")

    flag = _VENUE_FLAGS.get(venue)
    if not flag or not _bool_env(flag, False):
        return TradeGateDecision(False, f"live trading disabled for {venue} ({flag} off) — dry-run")

    cap = _float_env(_VENUE_CAPS.get(venue, ""), DEFAULT_LIVE_CAP_USD)
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
