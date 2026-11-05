"""098 AG-08/AG-15: session residency — ``resident -> evicting -> evicted``.

The eviction holds the session's execution lock (and its recreate lock) for its
whole length, and removes the orchestrator from the registry FIRST: a run or a
message that arrives during ``evicting`` waits and then recreates the session
from disk instead of landing in an orchestrator that ``cleanup()`` tears down.
The HITL queue snapshot is written atomically and survives a crash until the
first drain of the recreated session.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, Optional

from core.state.files import atomic_write_json
from core.state.machine import StateMachine

logger = logging.getLogger(__name__)

RESIDENCY_MACHINE = StateMachine(
    "session_residency",
    states=frozenset({"resident", "evicting", "evicted"}),
    transitions={
        "resident": frozenset({"evicting"}),
        "evicting": frozenset({"evicted", "resident"}),
        "evicted": frozenset({"resident"}),
    },
    initial=frozenset({"resident"}),
)

SNAPSHOT = "hitl_state.json"
RESTORED = "hitl_state.restored.json"


def set_residency(task_agent: Any, session_id: str, to: str) -> bool:
    """Move ``session_id`` to residency ``to`` along the table (a session with
    no entry is ``resident``). False on an illegal edge."""
    states: Dict[str, str] = task_agent.__dict__.setdefault("_residency", {})
    cur = states.get(session_id, "resident")
    if cur != to and not RESIDENCY_MACHINE.check(cur, to, ctx={"session_id": session_id}):
        return False
    if to == "evicting":
        states[session_id] = to
    else:  # resident or evicted: the registry says which, so no row stays (102 3.5)
        states.pop(session_id, None)
    return True


def residency(task_agent: Any, session_id: str) -> str:
    state = task_agent.__dict__.get("_residency", {}).get(session_id)
    if state:
        return state
    registry = getattr(task_agent, "_registry", None)
    return "evicted" if registry is not None and registry.get(session_id) is None else "resident"


def lock_is_idle(lock: Optional[asyncio.Lock]) -> bool:
    """No holder and no waiter — only then may the lock object be dropped."""
    return lock is None or (not lock.locked() and not getattr(lock, "_waiters", None))


def _memory_dir(session_id: str, user_id: Optional[str]):
    from agents.task.path import pm
    return pm().create_file_path(session_id=session_id, subdir_name="memory",
                                 filename=SNAPSHOT, user_id=user_id).parent


def save_hitl_snapshot(orchestrator: Any, session_id: str) -> int:
    """Write the main agent's queued messages to ``memory/hitl_state.json``
    (atomic). Returns the number saved (0 = nothing written)."""
    user_id = getattr(orchestrator, "user_id", None)
    for agent in list(getattr(orchestrator, "agents", {}).values()):
        if getattr(agent, "_is_sub_agent", False):
            continue
        hitl = getattr(agent, "hitl_manager", None)
        if hitl is None:
            continue
        state = hitl.get_state()
        queued = state.get("queued_messages") or []
        if not queued:
            continue
        atomic_write_json(os.path.join(str(_memory_dir(session_id, user_id)), SNAPSHOT), state)
        logger.info(f"💾 Saved HITL state for {session_id} ({len(queued)} queued messages)")
        return len(queued)
    return 0


def restore_hitl_snapshot(hitl_manager: Any, session_id: str, user_id: Optional[str]) -> int:
    """Merge the saved queue into ``hitl_manager`` (saved messages first). The
    newest file wins: ``hitl_state.json`` (an eviction) over
    ``hitl_state.restored.json`` (a restore whose drain never ran — a crash).
    The file is renamed to ``.restored`` and deleted at the first drain that
    empties the queue (``HITLManager.drain_user_messages``)."""
    d = str(_memory_dir(session_id, user_id))
    snap, restored = os.path.join(d, SNAPSHOT), os.path.join(d, RESTORED)
    src = snap if os.path.exists(snap) else (restored if os.path.exists(restored) else None)
    if src is None:
        return 0
    with open(src, "r", encoding="utf-8") as fh:
        state = json.load(fh)
    hitl_manager.restore_state(state)
    if src == snap:
        os.replace(snap, restored)
    hitl_manager._restored_snapshot = restored
    return len(state.get("queued_messages") or [])


def drop_restored_snapshot(hitl_manager: Any) -> None:
    path = getattr(hitl_manager, "_restored_snapshot", None)
    if not path:
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.debug(f"restored HITL snapshot not removed: {e}")
        return
    hitl_manager._restored_snapshot = None
