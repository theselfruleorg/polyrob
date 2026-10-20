"""084: run a settled card group's wake row, and sweep overdue cards.

The wake drain (``autonomy_runtime._build_wake_drain_ticker``) hands every
``cards_settled`` row here (``core.approved_actions`` runner). By origin:

* **chat** — wake the session that raised the cards with each card's outcome
  (``deliver_self_wake``, source ``cards_settled``). While that session has a
  live owner turn the row waits (``None`` → released, no attempt spent): the
  owner's turn shows the outcomes itself (``card_groups.take_outcomes``) and
  the group is then ``consumed``, so the row is dropped on the next tick.
* **goal** — a goal that blocked waiting for the owner is unblocked; its next
  run shows the outcomes. No wake: the run session is autonomous.
* **cron** — nothing runs early: re-running a job repeats every step in it
  (prod 2026-10-08). The job's next run shows the outcomes.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: How often the drain tick also sweeps overdue cards (the tick is 20 s).
SWEEP_EVERY_SEC = 60.0
_last_sweep = 0.0


def register() -> None:
    from core.approved_actions import set_runner
    from core.surfaces.card_groups import WAKE_KIND
    set_runner(WAKE_KIND, run_card_wake)


def maybe_sweep(now: Optional[float] = None) -> int:
    """Expire overdue cards (and close stuck ones) at most once a minute, so a
    group the owner walked away from still settles (084 D3). Fail-open."""
    global _last_sweep
    now = time.time() if now is None else now
    if now - _last_sweep < SWEEP_EVERY_SEC:
        return 0
    _last_sweep = now
    try:
        from core.surfaces import card_groups as G
        from core.surfaces import cards
        st = cards.existing_store()      # a sweep never mints an empty cards.db
        if st is None:
            return 0
        moved = cards.sweep(st=st)
        G.heal(st)
        return moved
    except Exception:
        logger.debug("card sweep failed (fail-open)", exc_info=True)
        return 0


def _owner_turn_live(task_agent: Any, session_id: str) -> bool:
    """A human turn runs (any seat of this process — the REPL marker names no
    session — or this session's marker in another process), or owner input waits."""
    try:
        from core.interactive_gate import is_interactive_busy, read_turn_marker
        if is_interactive_busy():
            return True
        m = read_turn_marker() or {}
        if m.get("session_id") == session_id:
            return True
    except Exception:
        pass
    try:
        pending = getattr(task_agent, "_session_has_pending_input", None)
        return bool(pending(session_id)) if callable(pending) else False
    except Exception:
        return False


def _unblock_goal(group: Dict[str, Any], cards_db: str) -> None:
    goal_id = group.get("origin_ref") or ""
    from core.runtime_paths import goals_db_path
    path = goals_db_path(os.path.dirname(os.path.abspath(cards_db)))
    if not goal_id or not os.path.exists(path):
        return
    try:
        from agents.task.goals.board import GoalBoard
        if GoalBoard(path).unblock(goal_id, user_id=group["user_id"],
                                   rationale="the owner decided every card this goal raised"):
            logger.info("card group %s: goal %s unblocked", group["group_id"], goal_id)
    except Exception:
        logger.warning("card group %s: goal unblock failed", group.get("group_id"),
                       exc_info=True)


async def run_card_wake(metadata: Dict[str, Any], user_id: str,
                        task_agent: Any) -> Optional[bool]:
    """True = the row is finished; False = retry it later (spends an attempt);
    None = the session is busy with the owner — wait without spending one."""
    from core.surfaces import card_groups as G
    from core.surfaces import cards
    gid = str(metadata.get("group_id") or "")
    st = cards.store()
    gs = G.groups(st)
    g = gs.get(gid) if gid else None
    if g is None or g["state"] != G.G_SETTLED:
        logger.info("card wake %s: already shown to the agent (%s) — dropped", gid,
                    (g or {}).get("state", "gone"))
        return True
    if g["origin"] == G.ORIGIN_GOAL:
        _unblock_goal(g, st.db_path)
        return True
    if g["origin"] != G.ORIGIN_CHAT:
        return True
    session_id = g["session_id"]
    if _owner_turn_live(task_agent, session_id):
        return None
    if gs.open_count(session_id):
        # The agent raised more cards the owner has not decided (a batch the
        # owner began to tap before it was complete): one wake when all are.
        return None
    # Every settled chat group of the session goes in this one wake. Mark them
    # first: the woken turn's own first step must not show them again.
    gids = [x["group_id"] for x in gs.decided_for(session_id=session_id)
            if x["origin"] == G.ORIGIN_CHAT]
    if gid not in gids:
        gids.append(gid)
    woken = [x for x in gids if gs.transition(x, frozenset({G.G_SETTLED}), G.G_WOKEN)]
    if not woken:
        return True
    ok = False
    try:
        text = "\n\n".join(t for t in (G.render_outcomes(g["user_id"], x, st=st)
                                        for x in woken) if t)
        deliver = getattr(task_agent, "deliver_self_wake", None)
        if callable(deliver) and text:
            ok = bool(await deliver(session_id, g["user_id"], text,
                                    metadata={"source": G.WAKE_KIND, "kind": G.WAKE_KIND,
                                              "group_id": gid, "group_ids": woken}))
    except Exception:
        logger.warning("card wake %s: delivery raised", gid, exc_info=True)
    if not ok:
        for x in woken:
            gs.transition(x, frozenset({G.G_WOKEN}), G.G_SETTLED)
        return False
    logger.info("card wake %s: session %s woken (%d group(s))", gid, session_id, len(woken))
    return True


__all__ = ["maybe_sweep", "register", "run_card_wake"]
