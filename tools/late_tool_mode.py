"""E-4 (2026-10-14 review) + 081 WS-3.1: the late-tool mode for ONE session.

A leaf module on purpose: the Registry and the ``tool_call`` bridge read it, and
``tools/tool_disclosure.py`` (which re-exports it) sits inside the large import
cycle the layering ratchet bounds. Imports stay lazy and point only downward.
"""
from typing import Optional


def disclosure_bridge(session_id: Optional[str]) -> bool:
    """Does THIS session reach a late tool through ``tool_call`` (the F9 bridge),
    whatever ``TOOL_SCHEMAS_FROZEN`` says? Its emitted ``tools[]`` is then pinned
    to its EAGER set and stays byte-stable (the cached prefix survives a load).

    True for the sessions that run on a narrow rig and widen it with
    ``load_tool``: an AUTONOMOUS run with disclosure on (E-4: under ``grow`` a
    cron run's ``load_tool`` sent a larger tool array and colded the cached
    prefix — prod: 137 + 20 + 54 ``tools_changed`` calls at 43-57 % cache), and
    an OWNER chat seat (081 WS-3.1, ``core.autonomy_marker.is_owner_seat``: one
    experience on both — the same catalog, the same ``load_tool`` result, the
    same ``tool_call``). Money tools stay in the eager set; they are never
    loadable. Any other session keeps the global mode. Fail-open to False.
    """
    if not isinstance(session_id, str) or not session_id:
        return False
    # The same reading as ``agents.task.session_class.tool_disclosure_enabled``,
    # taken from its core seams directly: importing session_class from here
    # would pull this module into the import cycle.
    try:
        from core.autonomy_marker import is_autonomous, is_owner_seat
        if is_owner_seat(session_id):
            return True
        if not is_autonomous(session_id):
            return False
        from core.config_policy.capability_toggles import (
            autonomous_tool_disclosure, tool_progressive_disclosure)
        return bool(tool_progressive_disclosure() or autonomous_tool_disclosure())
    except Exception:
        return False


#: Back-compat name (pre-081: the bridge was for autonomous runs only).
autonomous_bridge = disclosure_bridge


def session_late_tool_mode(provider: str, session_id: Optional[str]) -> str:
    """The late-tool mode for THIS session: :func:`late_tool_mode`, except that a
    :func:`disclosure_bridge` session on the ``grow`` path is ``bridge``. A capable
    Anthropic seat keeps ``deferred`` (also prefix-stable)."""
    from modules.llm.deferred_tools import (
        LATE_TOOL_MODE_BRIDGE, LATE_TOOL_MODE_GROW, late_tool_mode)
    mode = late_tool_mode(provider)
    if mode == LATE_TOOL_MODE_GROW and disclosure_bridge(session_id):
        return LATE_TOOL_MODE_BRIDGE
    return mode
