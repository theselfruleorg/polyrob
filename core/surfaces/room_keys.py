"""044: room helpers — session keys, and "is this outbound target a ROOM?".

A group key is `agent:main:{surface}:{chat_type}:{chat_id}[...]` with a non-dm
chat_type (core/surfaces/session_chat_registry.py::build_session_key).

Pure core: the allowlist probes below open the SAME
``<data_dir>/group_allowlist.db`` every other reader does
(``access.py``/``ledger_ingest.py``/``group_admin.py``) — the allowlist is
instance-level, not tenant-scoped, so no container is required beyond the data
home.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

_ROOM_TYPES = frozenset({"group", "supergroup", "channel"})


def is_group_session_key(session_key: Optional[str]) -> bool:
    parts = (session_key or "").split(":")
    return len(parts) >= 5 and parts[0] == "agent" and parts[3] in _ROOM_TYPES


def owner_only_reply_target(session_key: Optional[str], chat_id: Optional[str]) -> Optional[str]:
    """Where an OWNER-ONLY reply for this session must go.

    A DM key answers in place. A room key answers in the owner's Telegram DM,
    never in the room; when no owner telegram id resolves, returns None so the
    caller drops the reply rather than posting it publicly.
    """
    if not is_group_session_key(session_key):
        return chat_id
    from core.instance import resolve_owner_telegram_id
    return resolve_owner_telegram_id()


def _allowlist(data_dir: Optional[str]):
    from core.runtime_paths import data_dir_or_home
    from core.surfaces.group_allowlist import GroupAllowlist
    return GroupAllowlist(os.path.join(data_dir_or_home(data_dir),
                                       "group_allowlist.db"))


def _data_dir_of(container: Any) -> Optional[str]:
    cfg = getattr(container, "config", None) if container is not None else None
    return getattr(cfg, "data_dir", None)


def is_room_target(container: Any, surface: str, target: Any) -> bool:
    """True when ``(surface, target)`` is an ALLOWLISTED room chat.

    044 T21: a room is not a correspondent and not an outbound-allowlist entry —
    it is a chat the OWNER put the agent in. The `message` tool asks this before
    it runs the third-party rail (correspondent seeding, the open-tier daily cap)
    on what is really the agent's own room.

    Fail-CLOSED: any fault reads as "not a room", so the caller falls back to the
    ordinary (stricter) outbound ladder rather than skipping it on a bad probe.
    """
    try:
        if not surface or target in (None, ""):
            return False
        return _allowlist(_data_dir_of(container)).is_allowed(surface, str(target))
    except Exception as e:
        logger.debug("room target probe failed (reading as not-a-room): %s", e)
        return False


def mark_room_left(data_dir: Optional[str], surface: str, chat_id: Any) -> bool:
    """Record that the bot is no longer in this room (``status='left'``).

    044 T21: called from the ONE outbound seam when a send fails with a
    whole-chat liveness signal (kicked / chat gone / write-forbidden). Fail-open
    — bookkeeping must never take down a delivery path. Returns True iff an
    active row was moved.
    """
    try:
        return _allowlist(data_dir).mark_left(surface, str(chat_id))
    except Exception as e:
        logger.warning("could not mark room %s:%s left: %s", surface, chat_id, e)
        return False
