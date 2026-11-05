"""101: what the room gate reads about ONE session, live, at each call.

The IO half of ``core/surfaces/room_grants.py``. Only the session wiring in the agents
tier builds one; the gate reads it by duck type, so the gate stays out of these stores.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional, Tuple

from core.surfaces.room_grants import is_bundle_call

logger = logging.getLogger(__name__)


class RoomSeat:
    """What the room gate reads about ONE session, live, at each call.

    ``files_ok`` and ``browser_ok`` come from the wiring in the agents tier: core may
    not read the path manager or the browser config.
    """

    def __init__(self, orchestrator: Any, *, files_ok: Optional[Callable[[], bool]] = None,
                 browser_ok: Optional[Callable[[], bool]] = None) -> None:
        self.orch = orchestrator
        self.files_ok = files_ok or (lambda: False)
        self.browser_ok = browser_ok or (lambda: False)

    @property
    def stamp(self) -> Optional[dict]:
        found = getattr(self.orch, "_room_admin", None)
        return found if isinstance(found, dict) and found.get("chat_id") else None

    @property
    def tainted(self) -> bool:
        return getattr(self.orch, "_room_admin_tainted", False) is not False

    @property
    def line(self) -> str:
        return str(getattr(self.orch, "_room_admin_line", "") or "")

    @property
    def reply_author(self) -> Optional[dict]:
        return getattr(self.orch, "_room_admin_reply_author", None)

    def _home(self) -> str:
        from core.runtime_paths import container_data_home
        return container_data_home(getattr(self.orch, "container", None))

    def room(self) -> Tuple[str, str]:
        """``(surface, chat_id)``: the stamp, else the session's own binding key."""
        stamp = self.stamp
        if stamp:
            return str(stamp.get("surface") or ""), str(stamp.get("chat_id") or "")
        from core.surfaces.session_chat_registry import row_from_session_key
        row = row_from_session_key(str(getattr(self.orch, "_chat_session_key", "") or "")) or {}
        return str(row.get("surface_id") or ""), str(row.get("chat_id") or "")

    def policy(self) -> Any:
        from core.surfaces.chat_policy import load_for_chat
        surface, chat_id = self.room()
        return load_for_chat(self._home(), surface, chat_id)

    def allowed(self) -> bool:
        from core.runtime_paths import store_db_path
        from core.surfaces.group_allowlist import GroupAllowlist
        surface, chat_id = self.room()
        return GroupAllowlist(store_db_path("group_allowlist.db", self._home())).is_allowed(
            surface, chat_id)

    def role(self) -> str:
        """The stamped speaker's role NOW. Fail closed: the store reads ``blocked``."""
        stamp = self.stamp or {}
        from core.surfaces.access import is_room_owner
        if is_room_owner(stamp.get("user_id")):
            return "owner"
        from core.runtime_paths import surfaces_db_path
        from core.surfaces.group_roles import GroupRoles
        surface, chat_id = self.room()
        return GroupRoles(surfaces_db_path(self._home())).role(
            surface, chat_id, str(stamp.get("uid") or ""), is_owner=False)

    def room_tools(self) -> list:
        from core.surfaces.room_policy import room_tool_ids
        return room_tool_ids(policy=self.policy())


#: Reading these brings outside words into the admin session (101 Step 3.5).
_TAINTING = frozenset({"web_fetch", "http_request", "room_read"})


def note_admin_line(orchestrator: Any, line: str, reply_author: Optional[dict],
                    outside_words: bool = False, *, mid_run: bool = False) -> None:
    """A delivered ``/admin`` line: a genuine speaker line, and the line the moderation
    pin reads. ⚠️ R19: a reply, a quote or an absorbed file brings outside words IN this
    turn, so that line starts tainted. ⚠️ 101 R15: a line that joins a RUNNING turn moves
    no pin and clears no taint, or it would launder what the run already read; it can
    only add taint."""
    if mid_run:
        if outside_words:
            orchestrator._room_admin_tainted = True
        return
    orchestrator._room_admin_line = str(line or "")
    orchestrator._room_admin_reply_author = reply_author
    orchestrator._room_admin_tainted = bool(reply_author or outside_words)


def make_admin_post_hook(seat: RoomSeat, caps_fn: Callable[[], Any]):
    """Post-tool hook of a stamped session: the taint, then the audit row."""
    def _hook(action_name, params, result, context) -> None:
        stamp = seat.stamp
        if not stamp:
            return
        from core.tool_capabilities import CATALOG_ALIASES, row_field
        tool = getattr(context, "tool_id", None)
        name = str(action_name or "")
        details = getattr(getattr(seat.orch, "controller", None), "get_action_details", None)
        if details is not None:
            try:
                tool = getattr(details(name), "tool", tool)
            except Exception:
                logger.debug("room grants: tool unresolved for %s", name)
        tool_id = CATALOG_ALIASES.get(tool, tool) if tool else tool
        if (name in _TAINTING or tool_id in _TAINTING
                or (tool_id and row_field(tool_id, "untrusted_output"))):
            seat.orch._room_admin_tainted = True
        if is_bundle_call(name, tool):
            caps = caps_fn()
            if caps is not None:
                # No params are stored: the audit says who ran what, not with which text.
                caps.record_admin_tool(str(stamp.get("surface") or ""),
                                       str(stamp.get("chat_id") or ""), name,
                                       str(stamp.get("uid") or ""))
    return _hook
