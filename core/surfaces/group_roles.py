"""044 T16: per-chat roles. Owner comes from the principal, never a row.

Four roles, one per ``(surface, chat_id, user_id)``:

- ``owner``   — the bound owner principal. Resolved BEFORE any row is read, so a
                row can never demote him out of his own agent.
- ``admin``   — a member the owner promoted in THIS room. His line is a steer.
- ``member``  — the default. No row means member: the room regime is that anyone
                may talk, and his line is DATA.
- ``blocked`` — the ONLY per-member deny. The tier resolver turns it into DENIED.

Keyed on the RAW platform id (``identity.raw_user_id``), because that is the id
an owner can actually name from a seat (``/groups role here 9911``) and the one
every surface carries. Telegram's own admin list is a suggestion the owner
confirms (Task 19), never an authority this store reads.
"""
from __future__ import annotations

import time
from typing import Dict, List

from core.sqlite_util import execute_retry

#: The grantable vocabulary. ``owner`` is deliberately absent — it is a
#: PRINCIPAL, not a grant, so it can never be handed out by a room admin.
ROLES = ("admin", "member", "blocked")

_DDL = """CREATE TABLE IF NOT EXISTS group_roles (
    surface TEXT NOT NULL, chat_id TEXT NOT NULL, user_id TEXT NOT NULL,
    role TEXT NOT NULL, granted_by TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL, PRIMARY KEY (surface, chat_id, user_id))"""


class GroupRoles:
    """Per-chat role rows in ``surfaces.db`` (one more table, no new file)."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        execute_retry(db_path, _DDL)

    def grant(self, surface: str, chat_id: str, user_id: str, role: str, *,
              granted_by: str, note: str = "") -> None:
        """Upsert one role row. An unknown role RAISES — a seat that mistypes a
        role must hear the vocabulary back, never write a row nothing reads."""
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}; one of {ROLES}")
        execute_retry(
            self.db_path,
            """INSERT INTO group_roles(surface,chat_id,user_id,role,granted_by,note,created_at)
               VALUES(?,?,?,?,?,?,?) ON CONFLICT(surface,chat_id,user_id) DO UPDATE SET
               role=excluded.role, granted_by=excluded.granted_by, note=excluded.note""",
            (str(surface), str(chat_id), str(user_id), role, str(granted_by), str(note),
             time.time()))

    def revoke(self, surface: str, chat_id: str, user_id: str) -> bool:
        """Drop the row (back to ``member``). False when there was none."""
        rc = execute_retry(
            self.db_path,
            "DELETE FROM group_roles WHERE surface=? AND chat_id=? AND user_id=?",
            (str(surface), str(chat_id), str(user_id)))
        return bool(rc)

    def role(self, surface: str, chat_id: str, user_id: str, *, is_owner: bool) -> str:
        """The speaker's role in this room.

        Fail-open toward the LEAST privilege: an unreadable store answers
        ``member`` (whose line is data), never ``admin`` (whose line is a steer).
        """
        if is_owner:
            return "owner"
        row = execute_retry(
            self.db_path,
            "SELECT role FROM group_roles WHERE surface=? AND chat_id=? AND user_id=?",
            (str(surface), str(chat_id), str(user_id)), fetch="one")
        value = str(row["role"]) if row else "member"
        return value if value in ROLES else "member"

    def list(self, surface: str, chat_id: str) -> List[Dict]:
        """Every granted row in one room, oldest first."""
        rows = execute_retry(
            self.db_path,
            "SELECT user_id, role, granted_by, note, created_at FROM group_roles "
            "WHERE surface=? AND chat_id=? ORDER BY created_at",
            (str(surface), str(chat_id)), fetch="all") or []
        return [dict(r) for r in rows]


__all__ = ["GroupRoles", "ROLES"]
