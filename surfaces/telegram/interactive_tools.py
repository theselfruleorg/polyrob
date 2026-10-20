"""Toolset for an interactive OWNER chat session (Telegram and the surfaces its
harness serves).

The toolset itself is decided ONCE, in ``agents.task.tool_defaults``
(:func:`~agents.task.tool_defaults.owner_seat_tools`, 081 WS-3.1): the ``owner``
rig eager (files, TODO, web read, the goal board and the schedule) plus the money
tools the owner's grant holds; every other tool is listed in the
``<tool-catalog>`` and is one ``load_tool`` away. The console and the API on the
owner's identity read the same function. A non-owner keeps the conservative
default (``owner_interactive_tool_ids`` returns None), preserving tenant
least-privilege.

History (2026-07-03 audit): an owner session with the bare default
``['browser','filesystem','task']`` had no ``goal`` tool, so "review your goals"
fell back to reading the session sandbox and reported a false, empty board. The
``owner`` rig keeps ``goal`` and ``cronjob`` eager for that reason.
"""
from __future__ import annotations

from typing import List, Optional


def interactive_tool_ids() -> List[str]:
    """The interactive-owner toolset (``INTERACTIVE_TOOL_IDS`` overrides it)."""
    from core.instance import resolve_owner_principal
    from agents.task.tool_defaults import owner_seat_tool_ids
    return owner_seat_tool_ids(resolve_owner_principal()) or ["filesystem", "task"]


def owner_interactive_tool_ids(user_id: Optional[str]) -> Optional[List[str]]:
    """The interactive toolset when ``user_id`` is THIS instance's owner principal,
    else None (keep the conservative default toolset for a non-owner).

    A random surface sender is hashed to a ``u_…`` id and can never equal the
    owner principal, so this never elevates a stranger.
    """
    from agents.task.tool_defaults import owner_seat_tool_ids
    return owner_seat_tool_ids(user_id)


async def reconcile_owner_toolset(task_agent, user_id, session_id) -> List[str]:
    """Load whatever the OWNER'S configured grant has that this session lacks.

    A session freezes its toolset at CREATION, so a grant the owner makes later
    never reaches the chat he is already sitting in — and a MONEY tool can never
    close that gap on its own, because ``load_tool`` refuses the money set by
    design (explicit-grant-only). The only escape was ``/new``, which nothing
    told him.

    Live, 2026-09-12 (prod journal): the owner armed ``DEFI_AGENT_AUTONOMY=true``,
    ``owner_interactive_tool_ids()`` correctly returned a list containing
    ``defi_trade``, and his chat still could not bridge. Session 8e8c1d92 had to
    ``load_tool("defi_data")`` to read a balance — it started without the rail —
    and ``load_tool("defi_trade")`` was refused. The agent then reported a "hard
    architectural gate" and told him to type ``/bridge`` himself, which would
    have failed too.

    This is NOT the agent self-granting. The list comes from the operator's own
    configuration (:func:`interactive_tool_ids` — ``INTERACTIVE_TOOL_IDS``,
    ``full_autonomy_enabled``, ``DEFI_AGENT_AUTONOMY``) and the agent cannot
    influence it; a non-owner gets ``None`` and is never touched. Every gate
    downstream is unchanged: turn origin, the caps, the simulation and its
    asserted deltas, the owner queue, and the 031 pause.

    Fail-open — a reconcile problem must never cost the owner his turn.
    """
    try:
        granted = owner_interactive_tool_ids(user_id)
        if not granted:
            return []
        get_orch = getattr(task_agent, "get_orchestrator", None)
        orch = get_orch(session_id) if get_orch is not None else None
        controller = getattr(orch, "controller", None) if orch is not None else None
        if controller is None:
            return []
        have = set(controller.list_tools() or ())
        missing = [t for t in granted if t not in have]
        if not missing:
            return []
        await controller.load_tools_from_container(missing)
        now = set(controller.list_tools() or ())
        return [t for t in missing if t in now]
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "owner toolset reconcile skipped (non-fatal)", exc_info=True)
        return []
