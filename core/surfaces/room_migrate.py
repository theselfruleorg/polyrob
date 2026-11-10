"""101: a Telegram group upgraded to a supergroup gets a new chat id. Keep the room.

Without this the allowlist, policy, roles and ledger stayed on the old id: grants
vanished and blocked members became unblocked. ``surfaces/telegram/room_migration.py``
calls it on Telegram's own service message.

⚠️ Only an allowlisted OLD chat moves, so a stranger's chat cannot claim a room. A
conflict keeps the new row. ⚠️ 101 R13: a session binding is DROPPED, never rewritten.
The bound session and an admin stamp still name the old id, so the next line starts
fresh on the new id. Idempotent. Pure core.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any, List

from core.runtime_paths import container_data_home, store_db_path, surfaces_db_path

logger = logging.getLogger(__name__)


def _surface_column(columns: List[str]) -> str:
    return "surface" if "surface" in columns else "surface_id" if "surface_id" in columns else ""


def _move_tables(db: str, surface: str, old: str, new: str) -> List[str]:
    """Every table in this store keyed on the room, so a later table is not missed."""
    moved = []
    conn = sqlite3.connect(db, timeout=10)
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            quoted = '"' + table.replace('"', '""') + '"'
            columns = [r[1] for r in conn.execute(f"PRAGMA table_info({quoted})")]
            col = _surface_column(columns)
            if "chat_id" not in columns or not col:
                continue
            where = f"{col}=? AND chat_id=?"
            if table != "session_chat_map":
                done = conn.execute(f"UPDATE OR IGNORE {quoted} SET chat_id=? WHERE {where}",
                                    (new, surface, old)).rowcount
                if done > 0:
                    moved.append(table)
            conn.execute(f"DELETE FROM {quoted} WHERE {where}", (surface, old))
        conn.commit()
    finally:
        conn.close()
    return moved


def _move_policy(home: str, surface: str, old: str, new: str) -> bool:
    from core.instance import resolve_owner_user_id
    from core.surfaces import chat_policy
    owner = resolve_owner_user_id()
    src = chat_policy.chat_preferences_path(home, owner, surface, old)
    dst = chat_policy.chat_preferences_path(home, owner, surface, new)
    if src is None or dst is None or not src.is_file():
        return False
    if not dst.is_file():
        os.replace(src, dst)
    else:
        src.unlink()
    chat_policy._CACHE.pop(str(src), None)
    chat_policy._CACHE.pop(str(dst), None)
    return True


def _move_focus(home: str, surface: str, old: str, new: str) -> None:
    from core.surfaces import room_focus
    data = room_focus._read(home, for_write=True)
    if data is None:
        return               # an unreadable file is not rewritten (it holds other pointers)
    changed = False
    for entry in data.values():
        if isinstance(entry, dict) and entry.get("surface") == surface and str(entry.get("chat_id")) == old:
            entry["chat_id"] = new
            changed = True
    if changed:
        room_focus._write(home, data)


def migrate_room(container: Any, surface: str, old: Any, new: Any) -> List[str]:
    """Move one room. Returns what moved, empty when nothing did."""
    from core.surfaces.group_allowlist import GroupAllowlist
    old, new = str(old), str(new)
    home = container_data_home(container)
    allow_db = store_db_path("group_allowlist.db", home)
    if old == new or not os.path.isfile(allow_db):
        return []
    rows = GroupAllowlist(allow_db).list_all()
    if not any(r["surface"] == surface and str(r["chat_id"]) == old for r in rows):
        return []              # a stranger's chat, or a replay of a finished move
    moved = ["allowlist"] if _move_tables(allow_db, surface, old, new) else []
    db = surfaces_db_path(home)
    if os.path.isfile(db):
        moved.extend(_move_tables(db, surface, old, new))
    if _move_policy(home, surface, old, new):
        moved.append("policy")
    _move_focus(home, surface, old, new)
    logger.warning("room %s:%s migrated to %s: %s", surface, old, new, ", ".join(moved))
    return moved


__all__ = ["migrate_room"]
