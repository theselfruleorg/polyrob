"""099: the composition root's commerce wiring — the offer gate and the fulfiller.

* :func:`core.money.offer_gate.register` hands the money kernel its offer step
  (core never imports commerce on its own; a process that does not wire it
  refuses every order run's spend — fail closed).
* The fulfiller writes ONE goal per paid order on the board the dispatcher
  reads: the confirmed playbook pinned, the confirmed tools granted
  (``actor=owner_seat`` — the owner confirmed them on the live card), the order id
  as the run's mandate, and the buyer's text fenced as DATA.
* The goal canceller stops the goal an order took back (a retry, a passed deadline):
  a board cancel, so its run stops at once here and at its next heartbeat elsewhere.

Called by ``start_autonomy`` when goals run; the order service retries a paid
order on every settlement tick until a fulfiller exists.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _board(data_dir: Optional[str]):
    from agents.task.goals.board import GoalBoard
    from core.runtime_config import get_data_root
    from core.runtime_paths import goals_db_path
    return GoalBoard(goals_db_path(data_dir or get_data_root()))


def _stop_earlier_goals(b, order_id: str, user_id: str) -> None:
    """Cancel every still-open goal an earlier claim of this order wrote. A new claim
    means none of them is the order's goal any more (a crash between the goal write
    and the order's ``set_goal`` leaves one behind, which would run its other tools)."""
    path = getattr(b, "db_path", None)
    if not path:
        return
    from core.sqlite_util import execute_retry
    rows = execute_retry(path, "SELECT id FROM goals WHERE kind='goal' AND user_id=? AND "
                               "json_extract(payload, '$.mandate')=? AND status NOT IN "
                               "('done','cancelled','failed')",
                         (str(user_id), str(order_id)), fetch="all") or []
    for r in rows:
        logger.warning("commerce: goal %s of order %s is not the order's goal; stopped",
                       r["id"], order_id)
        b.cancel(r["id"])


def make_fulfiller(data_dir: Optional[str] = None, *, board=None):
    def _fulfil(order, manifest, token) -> Optional[str]:
        from core.commerce import store as st
        from modules.commerce.fulfillment import goal_payload, goal_title, task_text
        b = board if board is not None else _board(data_dir)
        _stop_earlier_goals(b, order.id, order.user_id)
        goal = b.create(user_id=order.user_id, title=goal_title(order, manifest),
                        body=task_text(order, manifest, st.resources(order.offer_id)),
                        payload=goal_payload(order, manifest), actor="owner_seat",
                        force=True, max_retries=0)
        return getattr(goal, "id", None)
    return _fulfil


def make_goal_canceller(data_dir: Optional[str] = None, *, board=None):
    def _cancel(goal_id: str) -> bool:
        b = board if board is not None else _board(data_dir)
        return b.cancel(goal_id)
    return _cancel


def wire(task_agent: Any = None, data_dir: Optional[str] = None) -> None:
    from core.commerce.hooks import register_fulfiller, register_goal_canceller
    from core.money import offer_gate
    offer_gate.register()
    register_fulfiller(make_fulfiller(data_dir))
    register_goal_canceller(make_goal_canceller(data_dir))
    try:
        from tools.commerce.market_adapter import register as register_markets
        kinds = register_markets(getattr(task_agent, "container", None), data_dir)
    except Exception:
        logger.warning("commerce: marketplace adapters not registered", exc_info=True)
        kinds = []
    logger.info("commerce: offer gate and fulfiller wired; markets: %s", kinds or "none")


__all__ = ["make_fulfiller", "make_goal_canceller", "wire"]
