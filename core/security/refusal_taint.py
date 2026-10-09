"""Per-run refusal taint: a run whose money verb was refused reports to the owner only.

Prod 2026-09-26 04:02: the PNL buyback's ``defi_trade_swap`` was refused by the
identity gate, and the SAME cron run then posted the refusal internals to a
Telegram channel, a group and X. The owner rule ("guard refusals and failures go
to the owner only") lived in skill text, which the model read and ignored.

This module is the structural half. A money verb that fails or is refused marks
its RUN (the session id; a delegated sub-agent's virtual id ``{sid}__{child}``
marks its root) as tainted. Two readers hold the public rails shut for the rest
of that run:

- the Controller pre-hook (``tools/controller/refusal_taint_gate.py``) refuses a
  public / non-owner send; a message to the owner still goes;
- cron delivery (``cron/delivery.py``) re-routes a public ``deliver`` sink to the
  owner.

Narrow on purpose: an untainted run — a confirmed tranche with no refusal —
still posts publicly. A genuine owner turn clears the taint (the owner is
driving again; ``agents/task/agent/core/user_ingress.py``).

Persisted across restarts and shared across workers. The bounded process-local
map is for telemetry only; eviction cannot clear the durable safety state.
"""
import logging
import threading
from collections import OrderedDict
from typing import Optional

logger = logging.getLogger(__name__)

#: How many tainted runs are remembered at once (oldest evicted first).
MAX_RUNS = 512

#: The refusal every public send of a tainted run receives.
PUBLIC_REFUSAL_TEXT = (
    "a money action was refused in this run — refusals are reported to the "
    "owner only, never posted publicly. Tell the owner with send_message or "
    "message(target='owner'); a public post is only for a completed, confirmed "
    "trade in a run with no refusal.")

_LOCK = threading.Lock()
_TAINTED: "OrderedDict[str, dict]" = OrderedDict()
#: Runs whose taint could not be persisted. Per RUN, never process-wide: one
#: failed write must not hold the public rails of every other run (owner chat,
#: an owner's standing job) until a restart.
_STORE_FAILED: set = set()


def run_key(session_id: Optional[str]) -> str:
    """The RUN a session id belongs to: a sub-agent's virtual id
    (``{parent}__{child}``, ``sub_agent_manager._generate_virtual_session_id``)
    collapses to its parent, so a refusal inside delegated work taints the run
    that would post about it."""
    sid = str(session_id or "").strip()
    return sid.split("__", 1)[0] if sid else ""


def _emit(**attrs) -> None:
    """One ``run_refusal_tainted`` row per run. Fail-open."""
    try:
        from core.event_kinds import RUN_REFUSAL_TAINTED
        from core.event_log import emit
        emit(RUN_REFUSAL_TAINTED, source="gate",
             user_id=str(attrs.pop("user_id", "") or ""),
             session_id=str(attrs.pop("session_id", "") or ""),
             attrs=attrs)
    except Exception:
        logger.debug("refusal taint: emit skipped", exc_info=True)


#: A money verb's error that OUR code classified, at the line that built it, as
#: an unmet PRECONDITION or a MARKET / transport condition — not a policy
#: decision about whether the act is safe (insufficient allowance, no route, a
#: stale quote, a transaction the node rejected). Such an error does not taint
#: the run: the owner's standing buyback that hits "insufficient allowance",
#: approves and retries must still post its notice (prod 2026-10-08).
#:
#: The tag rides ``ActionResult.metadata[ERROR_KIND_KEY]`` (``BaseTool._ar(...,
#: error_kind=PRECONDITION)``). It is never inferred from the error TEXT: that
#: text can carry outside words (a token's name, a provider's message), and a
#: phrase match let a guard refusal that quoted "simulation reverted" pass as a
#: market error (verifier round 3, 2026-10-08).
ERROR_KIND_KEY = "error_kind"
PRECONDITION = "precondition"


class PreconditionUnmet(ValueError):
    """Raised by OUR code where it measured an unmet precondition (the wallet
    holds less than the act needs, an allowance is short). The catch site maps
    the exception TYPE, never its text, to ``PRECONDITION``."""


def kind_of(exc: BaseException) -> Optional[str]:
    """``PRECONDITION`` for a :class:`PreconditionUnmet`, else None."""
    return PRECONDITION if isinstance(exc, PreconditionUnmet) else None


def simulation_kind(deltas) -> Optional[str]:
    """``PRECONDITION`` when a simulation that passed every vetting step then
    REVERTED (``deltas.reverted``, set at the branch that read the node's
    structured ``err``): nothing was sent, and the act could not succeed with
    the wallet as it stands (insufficient balance, a program error). A vetting
    refusal (a disallowed program, a bound breach) leaves ``reverted`` False and
    still taints."""
    return PRECONDITION if getattr(deltas, "reverted", False) is True else None


def error_kind(result) -> Optional[str]:
    """The structured error kind our code put on an ``ActionResult``, or None."""
    meta = getattr(result, "metadata", None)
    if isinstance(meta, dict):
        kind = meta.get(ERROR_KIND_KEY)
        return str(kind) if kind else None
    return None


def taints(detail: Optional[str], kind: Optional[str] = None) -> bool:
    """True when a money verb's error is a refusal that must taint the run.

    Default TAINT: only an error our code tagged ``PRECONDITION`` at the line
    that built it is exempt. ``detail`` is kept for the caller's log only."""
    return kind != PRECONDITION


def mark(session_id: Optional[str], *, action: str = "", user_id: str = "",
         detail: str = "") -> None:
    """Taint the run ``session_id`` belongs to. The first mark per run emits."""
    key = run_key(session_id)
    if not key:
        return
    from core.security import refusal_taint_store
    try:
        first = not refusal_taint_store.read(key)
        refusal_taint_store.write(key, tainted=True)
    except Exception:
        with _LOCK:
            _STORE_FAILED.add(key)
        first = True
        logger.error('Refusal taint could not be persisted; public sends held', exc_info=True)
    with _LOCK:
        _TAINTED[key] = {"action": str(action or ""),
                         "detail": str(detail or "")[:200]}
        _TAINTED.move_to_end(key)
        while len(_TAINTED) > MAX_RUNS:
            _TAINTED.popitem(last=False)
    if first:
        logger.warning("⛔ run %s refusal-tainted by %s: public rails held for "
                       "this run", key[:8], action or "?")
        _emit(session_id=key, action=str(action or ""), user_id=user_id,
              detail=str(detail or "")[:200])


def is_tainted(session_id: Optional[str]) -> bool:
    key = run_key(session_id)
    if not key:
        return False
    with _LOCK:
        if key in _STORE_FAILED:
            return True
    from core.security import refusal_taint_store
    try:
        return refusal_taint_store.read(key)
    except Exception:
        logger.error('Refusal taint store unreadable; public sends held', exc_info=True)
        return True


def clear(session_id: Optional[str]) -> None:
    key = run_key(session_id)
    if not key:
        return
    with _LOCK:
        _TAINTED.pop(key, None)
        _STORE_FAILED.discard(key)
    from core.security import refusal_taint_store
    # A failed write raises (the caller logs it); the durable row then still
    # reads tainted, so this run stays held.
    refusal_taint_store.write(key, tainted=False)


def reset_for_tests() -> None:
    with _LOCK:
        _STORE_FAILED.clear()
        _TAINTED.clear()


__all__ = ["MAX_RUNS", "PUBLIC_REFUSAL_TEXT", "clear", "is_tainted", "mark",
           "run_key", "taints"]
