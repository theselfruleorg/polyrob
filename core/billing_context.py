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
from typing import Awaitable, Callable, Optional

from core.env import int_env

DEFAULT_PREPAID_TOKEN_BUDGET = 200_000

#: None = not a prepaid request; 0 = prepaid at the configured budget;
#: > 0 = prepaid at that explicit budget.
_PREPAID: ContextVar[Optional[int]] = ContextVar("prepaid_request_budget", default=None)
_BILLED: ContextVar[object] = ContextVar("billed_compute_request", default=False)


def mark_billed(reserve: Optional[Callable[[str, str], Awaitable[str]]] = None) -> Token:
    """Mark credit-paid compute; child tasks inherit the billing restriction."""
    return _BILLED.set(reserve if reserve is not None else True)


def reset_billed(token: Token) -> None:
    _BILLED.reset(token)


def is_billed() -> bool:
    return bool(_BILLED.get()) or is_prepaid()


async def reserve_billed_call(model: str, provider: str) -> Optional[str]:
    """The authenticated entry point injects the credit ledger's reservation.

    A billed marker without a ledger is a restriction, never spending authority.
    Prepaid requests use their separately enforced token budget.
    """
    if not is_billed() or is_prepaid():
        return None
    reserve = _BILLED.get()
    if not callable(reserve):
        raise ValueError("Credit reservation unavailable; no model request was sent")
    return await reserve(model, provider)


def billed_tool_refusal(tool_id: Optional[str]) -> Optional[str]:
    """Billed tenants may use the standard session profile, not operator rails."""
    if not is_billed() or tool_id in (None, "", "default", "controller"):
        return None
    from core.config_policy.profiles import profile
    if tool_id not in profile("default:session"):
        return "Billed requests cannot use tools outside the standard session profile"
    return None


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
