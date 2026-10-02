"""The prepaid-request billing context — rail-neutral (067 P1b).

CR-M15: prepaid (per-request) billing is decided by THIS request having
settled a payment, never by a stored profile tier. The rail that settles the
payment (today the x402 middleware, ``modules/x402/x402_integration.py``)
marks the request around its downstream call; asyncio tasks spawned inside
that call (the session run) inherit the mark through contextvars. The LLM
usage tracker (``modules/credits/usage_tracker.py``) reads it here and never
imports the rail.

``prepaid_budget()`` is the token budget one prepaid request buys: the budget
the rail marked, else :func:`prepaid_token_budget`, the ONE reader of
``X402_MAX_TOKENS_PER_REQUEST`` (the price and the runtime cap must read the
same number). The env name keeps its x402 prefix; renaming the ``x402`` tier
to ``prepaid`` is a separate change.

Tier-0: stdlib + ``core.env``.
"""
from contextvars import ContextVar, Token
from typing import Optional

from core.env import int_env

DEFAULT_PREPAID_TOKEN_BUDGET = 200_000

#: None = not a prepaid request; 0 = prepaid at the configured budget;
#: > 0 = prepaid at that explicit budget.
_PREPAID: ContextVar[Optional[int]] = ContextVar("prepaid_request_budget", default=None)


def prepaid_token_budget() -> int:
    """Tokens one prepaid request buys (``X402_MAX_TOKENS_PER_REQUEST``,
    default 200k). A missing, unparsable, zero or negative value is the default."""
    value = int_env("X402_MAX_TOKENS_PER_REQUEST", DEFAULT_PREPAID_TOKEN_BUDGET)
    return value if value > 0 else DEFAULT_PREPAID_TOKEN_BUDGET


def mark_prepaid(budget: Optional[int] = None) -> Token:
    """Mark the current context as a SETTLED prepaid request. Returns the
    reset token. With no *budget* (or a non-positive one) the request's budget
    is :func:`prepaid_token_budget`, read when it is enforced."""
    value = int(budget) if budget is not None else 0
    return _PREPAID.set(value if value > 0 else 0)


def reset_prepaid(token: Token) -> None:
    _PREPAID.reset(token)


def is_prepaid() -> bool:
    """True only inside a request whose payment settled (CR-M15)."""
    return _PREPAID.get() is not None


def prepaid_budget() -> int:
    """The token budget of the current prepaid request: the budget the rail
    marked, else (no explicit budget, or outside a request — a session that
    already metered under one keeps its cap) the configured budget."""
    marked = _PREPAID.get()
    return marked if marked else prepaid_token_budget()
