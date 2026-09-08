"""Ambient identity for the action batch in flight (033 T0.1).

Several durable-telemetry emitters have no execution context of their own.
``PolicyGate.record`` (``core/wallet/policy.py``) is a shared singleton reached
from seven money call sites; the app supervisor and the settlement watcher run on
their own loops. Historically they simply omitted ``user_id``, which made their
rows invisible to every tenant-scoped reader: all 114 production ``wallet_spend``
rows were tenantless, so ``modules/credits/unified_ledger.py::_wallet_leg``
matched none of them and reported ``wallet_spend_usd: 0.0`` while
``wallet_metering`` claimed to be ``"on"`` — a confident wrong zero, which the
status SSOT forbids.

This module is the ONE fallback. ``Controller.multi_act`` binds the batch's
identity; an emitter that was handed no context reads it. An explicit argument
ALWAYS wins — this never overrides a known tenant.

ContextVars are per-task, so a delegated sub-agent running in its own asyncio
task binds its own value and can never leak into a sibling. Fail-open
throughout: telemetry plumbing must never raise into the action loop.
"""
from contextvars import ContextVar, Token
from typing import Optional, Tuple

_IDENTITY: ContextVar[Tuple[str, str]] = ContextVar(
    "polyrob_exec_identity", default=("", ""))


def set_exec_identity(user_id: Optional[str], session_id: Optional[str]) -> Token:
    """Bind the ambient ``(user_id, session_id)``. Always pair with
    :func:`reset_exec_identity` in a ``finally``."""
    return _IDENTITY.set((str(user_id or ""), str(session_id or "")))


def reset_exec_identity(token: Token) -> None:
    """Restore the previous binding. Fail-open — a stale token (a bind and reset
    that crossed task boundaries) must never raise into the action loop."""
    try:
        _IDENTITY.reset(token)
    except Exception:
        pass


def current_exec_identity() -> Tuple[str, str]:
    """The ambient ``(user_id, session_id)``, or ``("", "")`` when nothing is bound."""
    try:
        return _IDENTITY.get()
    except Exception:
        return ("", "")
