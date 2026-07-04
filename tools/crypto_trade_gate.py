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
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class TradeGateDecision:
    live: bool      # True => submit to the venue; False => dry-run / blocked
    reason: str


def evaluate_live_trade(venue: str, amount_usd: float | None) -> TradeGateDecision:
    """Decide whether an order may be submitted live, or must dry-run."""
    if not _bool_env("CRYPTO_TRADE_LIVE_ENABLED", False):
        return TradeGateDecision(False, "live trading disabled (CRYPTO_TRADE_LIVE_ENABLED off) — dry-run")

    flag = _VENUE_FLAGS.get(venue)
    if not flag or not _bool_env(flag, False):
        return TradeGateDecision(False, f"live trading disabled for {venue} ({flag} off) — dry-run")

    cap = _float_env(_VENUE_CAPS.get(venue, ""), DEFAULT_LIVE_CAP_USD)
    if amount_usd is not None and amount_usd > cap:
        return TradeGateDecision(False, f"order ${amount_usd:.2f} exceeds the {venue} live cap ${cap:.2f} — blocked")

    return TradeGateDecision(True, f"live trading enabled for {venue} (within ${cap:.2f} cap)")
