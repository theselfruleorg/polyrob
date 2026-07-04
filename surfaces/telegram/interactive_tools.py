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

# filesystem + task are auto-merged by the orchestrator, but list them for clarity.
_DEFAULT_INTERACTIVE_TOOL_IDS = "goal,twitter,web_fetch,filesystem,task"


def interactive_tool_ids() -> List[str]:
    """The interactive-owner toolset (env ``INTERACTIVE_TOOL_IDS``-overridable)."""
    raw = os.getenv("INTERACTIVE_TOOL_IDS", _DEFAULT_INTERACTIVE_TOOL_IDS)
    ids = [t.strip() for t in raw.split(",") if t.strip()]
    return ids or ["filesystem", "task"]


def owner_interactive_tool_ids(user_id: Optional[str], env=None) -> Optional[List[str]]:
    """Return the interactive toolset when ``user_id`` is THIS instance's owner principal,
    else None (keep the conservative default toolset for a non-owner sender).

    The owner principal is ``resolve_owner_principal`` (e.g. ``"rob"`` — the id the
    owner's telegram chat is aliased to, and the tenant of autonomy's own goals/memory).
    A random surface sender is hashed to a ``u_…`` id and can never equal it, so this
    never elevates a stranger.
    """
    owner = resolve_owner_principal(env)
    if owner and user_id and str(user_id) == str(owner):
        return interactive_tool_ids()
    return None
