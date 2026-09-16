"""Toolset for an interactive OWNER telegram session.

An inbound telegram message spawns a session via ``create_session(request=<text>)`` — a
plain string — so it inherits ``SessionRequest``'s bare default
``['browser','filesystem','task']``. That default has NO ``goal`` tool, so when the
owner asks "review your goals" the agent cannot call ``goal_list`` and falls back to
reading its per-session sandbox (where the goal DB and repo don't live), then reports
"goal database is empty / repo missing" — a false, bleak picture (2026-07-03 audit).

Fix: the OWNER principal's interactive session gets a toolset that can introspect the
board (``goal``) and act on-mission (``twitter``/``web_fetch``). Recall actions
(``recent_activity``/``session_search``) are NOT tool-gated — they ride env/backend
flags — so they are not listed here. A non-owner keeps the conservative default
(``owner_interactive_tool_ids`` returns None), preserving tenant least-privilege.

Kept in the surfaces layer (not ``core``) so it may import both ``core.instance`` and
env config without violating the core→agents/server import boundary.
"""
from __future__ import annotations

import os
from typing import List, Optional

from core.instance import resolve_owner_principal


def interactive_tool_ids() -> List[str]:
    """The interactive-owner toolset (env ``INTERACTIVE_TOOL_IDS``-overridable).

    Supervised default: the historical 5-tool string (byte-identical).
    Effective ``AUTONOMY_MODE=autonomous``: the full ``AUTONOMOUS_MODE_TOOLS``
    grant — the owner chat KEEPS ``goal``/``cronjob``, it is exactly where
    scheduling belongs — plus the posture-gated compute tools
    (``with_compute_tools``). An explicit env always wins (014 A2).
    """
    raw = os.getenv("INTERACTIVE_TOOL_IDS")
    if raw is not None:
        ids = [t.strip() for t in raw.split(",") if t.strip()]
        return ids or ["filesystem", "task"]
    from agents.task.constants import autonomous_mode_tools, full_autonomy_enabled
    from agents.task.tool_defaults import with_compute_tools, resolve_toolset
    if full_autonomy_enabled():
        # `autonomous_mode_tools()`, not the bare constant: it folds in the defi
        # rail when the operator has armed DEFI_AGENT_AUTONOMY. Without that, the
        # OWNER'S OWN CHAT SESSION had no defi tool at all, so asking his agent to
        # bridge got "no bridge verb in the tool catalog" about a working rail
        # (live, 2026-09-12).
        return with_compute_tools(list(autonomous_mode_tools()))
    # Supervised default: the SSOT "owner_interactive" toolset (byte-identical to the
    # historical goal,twitter,web_fetch,filesystem,task string).
    return resolve_toolset("owner_interactive")


def owner_interactive_tool_ids(user_id: Optional[str], env=None) -> Optional[List[str]]:
    """Return the interactive toolset when ``user_id`` is THIS instance's owner principal,
    else None (keep the conservative default toolset for a non-owner sender).

    The owner principal is ``resolve_owner_principal`` (the instance id — the id the
    owner's telegram chat is aliased to, and the tenant of autonomy's own goals/memory).
    A random surface sender is hashed to a ``u_…`` id and can never equal it, so this
    never elevates a stranger.
    """
    owner = resolve_owner_principal(env)
    if owner and user_id and str(user_id) == str(owner):
        return interactive_tool_ids()
    return None


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
