"""Which room an owner is administering from a DM.

⚠️ This is a UI POINTER, not policy. It decides which room the word ``here``
means when there is no room around the message; it never decides what the
caller may DO there. Every verb still resolves the caller's role for the
resolved room through ``group_ops._resolve_role`` → ``group_admin.room_role``,
so focusing a room you are only a member of buys you exactly nothing.

Why it exists: the owner asked (2026-09-15) that the only line ever typed in a
public room be ``/groups allow here``. Prices, caps, member verbs and enabling
the sale are administration, and the room is the one place a shoulder-surfer is
guaranteed. The alternative — the existing explicit ``<surface> <chat_id>``
grammar — works from a DM already but means retyping ``telegram
-1002002374383`` on every line.

Keyed per OWNER-PRINCIPAL. A shared focus would let one admin's ``/groups use``
silently redirect another's ``here``, which is the shape of an accident with
money attached.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

#: One file, all principals. Small, rewritten whole, read on every use — the
#: same shape as the pause record, and for the same reason: there is exactly
#: one of it and a half-written one must not be believed.
_FILENAME = "room_focus.json"


def _path(home_dir: str) -> Path:
    return Path(home_dir) / _FILENAME


def _safe_key(user_id: Any) -> str:
    """A principal key we are willing to write. Empty/anonymous is refused so a
    focus can never be shared by everyone who failed to authenticate."""
    key = str(user_id or "").strip()
    if not key or key in (".", ".."):
        return ""
    return "".join(c for c in key if c.isalnum() or c in "-_.@")[:128]


def _read(home_dir: str) -> dict:
    path = _path(home_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as e:
        # A pointer we cannot read is NO pointer — `here` then says so rather
        # than resolving to a room nobody selected.
        logger.warning("room focus: %s unreadable (%s) — reading as unset",
                       path.name, e)
        return {}


def get(home_dir: str, user_id: Any) -> Optional[Tuple[str, str]]:
    """``(surface, chat_id)`` this principal is administering, or ``None``."""
    key = _safe_key(user_id)
    if not key:
        return None
    row = _read(home_dir).get(key)
    if not isinstance(row, dict):
        return None
    surface = str(row.get("surface") or "").strip()
    chat_id = str(row.get("chat_id") or "").strip()
    if not surface or not chat_id:
        return None
    return surface, chat_id


def set(home_dir: str, user_id: Any, surface: str, chat_id: str) -> bool:
    """Point this principal's ``here`` at one room. Returns False if refused."""
    key = _safe_key(user_id)
    surface = str(surface or "").strip().lower()
    chat_id = str(chat_id or "").strip()
    if not key or not surface or not chat_id:
        return False
    data = _read(home_dir)
    data[key] = {"surface": surface, "chat_id": chat_id}
    return _write(home_dir, data)


def clear(home_dir: str, user_id: Any) -> bool:
    key = _safe_key(user_id)
    if not key:
        return False
    data = _read(home_dir)
    if key not in data:
        return True
    data.pop(key, None)
    return _write(home_dir, data)


def _write(home_dir: str, data: dict) -> bool:
    """Atomic temp+replace. Fail-open to False: losing a UI pointer is a
    nuisance, and raising into a chat verb is worse."""
    try:
        path = _path(home_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except Exception as e:
        logger.warning("room focus: could not write (%s)", e)
        return False
