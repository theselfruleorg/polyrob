"""Group-chat ingress allowlist. Default-DENY: a (surface, chat_id) must have an
ACTIVE row for the agent to accept messages from that group/channel at all
Instance-level (one bot presence
per chat, not per-tenant) — owner-managed via ``polyrob owner groups`` (see
``docs/guide/groups.md``). WAL+jitter via ``core/sqlite_util`` — mirrors
``core/surfaces/outbound_allowlist.py``.
"""
from __future__ import annotations

import os
import time
from typing import Dict, List

from core.sqlite_util import execute_retry

_DDL = """
CREATE TABLE IF NOT EXISTS group_allowlist (
    surface TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    note    TEXT NOT NULL DEFAULT '',
    status  TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL,
    PRIMARY KEY (surface, chat_id)
);
"""

#: 057 WS-D. ``allow()`` on an existing row REWRITES the note, so dating that
#: note by ``created_at`` would over-age it by however long the room has been
#: allowed. Added by an idempotent ALTER (a duplicate-column error is the
#: already-migrated case) rather than a numbered migration — this store is
#: created on first use by every surface that touches it.
_ALTER_UPDATED_AT = "ALTER TABLE group_allowlist ADD COLUMN updated_at REAL"


class GroupAllowlist:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        execute_retry(self.db_path, _DDL)
        try:
            execute_retry(self.db_path, _ALTER_UPDATED_AT)
        except Exception:
            pass  # already present (or an older sqlite) — never block the store

    def allow(self, surface: str, chat_id: str, note: str = "") -> None:
        now = time.time()
        execute_retry(
            self.db_path,
            "INSERT INTO group_allowlist(surface,chat_id,note,status,created_at,updated_at)"
            " VALUES(?,?,?, 'active', ?, ?)"
            " ON CONFLICT(surface,chat_id) DO UPDATE SET status='active',"
            " note=excluded.note, updated_at=excluded.updated_at",
            (surface, str(chat_id), note, now, now),
        )

    def revoke(self, surface: str, chat_id: str) -> bool:
        rc = execute_retry(
            self.db_path,
            "UPDATE group_allowlist SET status='revoked'"
            " WHERE surface=? AND chat_id=? AND status='active'",
            (surface, str(chat_id)),
        )
        return bool(rc)

    def mark_left(self, surface: str, chat_id: str) -> bool:
        """044 T21: the bot is no longer IN this room (kicked, or the chat is
        gone). Distinct from ``revoke``: the owner did not withdraw permission,
        the room withdrew the bot — and ``/groups list`` should say which.

        ``left`` is not ``active``, so ``is_allowed`` goes False and both ingress
        and outbound stop. Re-``allow`` restores the room after a rejoin.
        Returns True iff an ACTIVE row was moved (idempotent; a revoked or
        unknown room is left alone — never resurrected as ``left``).
        """
        rc = execute_retry(
            self.db_path,
            "UPDATE group_allowlist SET status='left'"
            " WHERE surface=? AND chat_id=? AND status='active'",
            (surface, str(chat_id)),
        )
        return bool(rc)

    def is_allowed(self, surface: str, chat_id: str) -> bool:
        """Never raises — any fault reads as NOT allowed (fail-closed)."""
        try:
            row = execute_retry(
                self.db_path,
                "SELECT 1 FROM group_allowlist"
                " WHERE surface=? AND chat_id=? AND status='active'",
                (surface, str(chat_id)),
                fetch="one",
            )
            return row is not None
        except Exception:
            return False

    def list_all(self) -> List[Dict]:
        rows = execute_retry(
            self.db_path,
            "SELECT surface, chat_id, note, status, created_at, updated_at"
            " FROM group_allowlist ORDER BY created_at DESC",
            fetch="all",
        ) or []
        return [
            {"surface": r[0], "chat_id": r[1], "note": r[2], "status": r[3],
             "created_at": r[4], "updated_at": r[5]}
            for r in rows
        ]


__all__ = ["GroupAllowlist"]
