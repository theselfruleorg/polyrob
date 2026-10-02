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

Process-local and bounded (``MAX_RUNS``, oldest evicted): the cron runner, the
agent loop and the controller share one process, and a run is minutes long.
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


def mark(session_id: Optional[str], *, action: str = "", user_id: str = "",
         detail: str = "") -> None:
    """Taint the run ``session_id`` belongs to. The first mark per run emits."""
    key = run_key(session_id)
    if not key:
        return
    with _LOCK:
        first = key not in _TAINTED
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
        return key in _TAINTED


def clear(session_id: Optional[str]) -> None:
    key = run_key(session_id)
    if not key:
        return
    with _LOCK:
        _TAINTED.pop(key, None)


def reset_for_tests() -> None:
    with _LOCK:
        _TAINTED.clear()


__all__ = ["MAX_RUNS", "PUBLIC_REFUSAL_TEXT", "clear", "is_tainted", "mark",
           "run_key"]
