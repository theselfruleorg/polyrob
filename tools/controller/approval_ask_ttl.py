"""How long an owner approval ask stays open (098 TS-14).

An ask with no clock could be approved weeks later, after the payee or price
moved. Each ``tool_approval`` ask carries ``payload.expires_at``; past it the
board's sweep closes it (``obsolete``, reason ``expired``) and a tap says
"expired — ask again". The one-shot grant an approval leaves is capped by the
same clock (:func:`grant_deadline`).

``APPROVAL_ASK_TTL_HOURS`` (default 72) sets the window; a money ask uses the
smaller of it and :data:`MONEY_ASK_TTL_HOURS`.
"""
from __future__ import annotations

from typing import Optional

DEFAULT_ASK_TTL_HOURS = 72.0
MONEY_ASK_TTL_HOURS = 24.0


def ask_ttl_hours(*, money: bool) -> float:
    from core.env import float_env
    ttl = float_env("APPROVAL_ASK_TTL_HOURS", DEFAULT_ASK_TTL_HOURS)
    if ttl <= 0:
        ttl = DEFAULT_ASK_TTL_HOURS
    return min(ttl, MONEY_ASK_TTL_HOURS) if money else ttl


def expires_at(now: float, *, money: bool) -> float:
    return float(now) + ask_ttl_hours(money=money) * 3600.0


def grant_deadline(*, created_at: float, completed_at: Optional[float],
                   grant_ttl_hours: float, ask_expires_at: Optional[float]) -> Optional[float]:
    """When a decided ask's one-shot grant stops being redeemable:
    ``completed_at + grant_ttl``, capped at ``expires_at + grant_ttl`` (an ask
    without a clock: ``created_at + ask TTL + grant_ttl``). None = no grant."""
    if completed_at is None:
        return None
    ttl = float(grant_ttl_hours) * 3600.0
    cap_base = (float(ask_expires_at) if ask_expires_at is not None
                else float(created_at) + DEFAULT_ASK_TTL_HOURS * 3600.0)
    return min(float(completed_at) + ttl, cap_base + ttl)


__all__ = ["DEFAULT_ASK_TTL_HOURS", "MONEY_ASK_TTL_HOURS", "ask_ttl_hours",
           "expires_at", "grant_deadline"]
