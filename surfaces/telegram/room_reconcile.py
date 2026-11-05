"""101: what a room session's toolset must hold NOW.

The sibling of ``interactive_tools.reconcile_owner_toolset``. A session freezes its
toolset at creation, so a grant made later loads here, at the next turn, and a
recreated session gets its additions from the same call (``effective_tools`` keeps
its one writer). A REVOKE is not applied here: the room gate refuses it at the next
call (``core.surfaces.room_grants``).
"""
from __future__ import annotations

import logging
from typing import Any, List

logger = logging.getLogger(__name__)


def _policy(task_agent: Any, source: Any) -> Any:
    from surfaces.telegram.room_turn import _room_policy
    return _room_policy(getattr(task_agent, "container", None),
                        str(getattr(source, "surface_id", "") or ""),
                        str(getattr(source, "chat_id", "") or ""))


def room_session_tools(task_agent: Any, source: Any, room_admin: Any = None) -> List[str]:
    """The toolset of a new room session."""
    from core.surfaces.room_grants import bundle_tools, live_admin_bundles
    from core.surfaces.room_policy import room_tool_ids
    policy = _policy(task_agent, source)
    tools = room_tool_ids(policy=policy)
    if room_admin is not None:
        tools.extend(t for t in bundle_tools(live_admin_bundles(policy)) if t not in tools)
    return tools


async def reconcile_room_toolset(task_agent: Any, session_id: str, source: Any,
                                 *, admin: bool = False) -> List[str]:
    """Load what the live policy added and this session lacks. Never raises: a
    reconcile fault costs the addition, and the gate still decides each call."""
    try:
        from core.surfaces.room_grants import (bundle_tools, live_admin_bundles,
                                               live_room_tools)
        policy = _policy(task_agent, source)
        wanted = list(live_room_tools(policy))
        if admin:
            wanted.extend(bundle_tools(live_admin_bundles(policy)))
        if not wanted:
            return []
        orch = task_agent.get_orchestrator(session_id)
        controller = getattr(orch, "controller", None)
        if controller is None:
            return []
        have = set(controller.list_tools() or ())
        missing = [t for t in wanted if t not in have]
        if missing:
            await controller.load_tools_from_container(missing)
        return missing
    except Exception:
        logger.warning("room toolset reconcile skipped", exc_info=True)
        return []
