"""Group-chat ingress allowlist. Default-DENY: a (surface, chat_id) must have an
ACTIVE row for the agent to accept messages from that group/channel at all
(design 2026-07-11 §B1). Instance-level (one bot presence per chat, not
per-tenant) — owner-managed via ``polyrob owner``. WAL+jitter via
``core/sqlite_util`` — mirrors ``core/surfaces/outbound_allowlist.py``.
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


class GroupAllowlist:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        execute_retry(self.db_path, _DDL)

    def allow(self, surface: str, chat_id: str, note: str = "") -> None:
        execute_retry(
            self.db_path,
            "INSERT INTO group_allowlist(surface,chat_id,note,status,created_at)"
            " VALUES(?,?,?, 'active', ?)"
            " ON CONFLICT(surface,chat_id) DO UPDATE SET status='active',"
            " note=excluded.note",
            (surface, str(chat_id), note, time.time()),
        )

    def revoke(self, surface: str, chat_id: str) -> bool:
        rc = execute_retry(
            self.db_path,
            "UPDATE group_allowlist SET status='revoked'"
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
            "SELECT surface, chat_id, note, status, created_at"
            " FROM group_allowlist ORDER BY created_at DESC",
            fetch="all",
        ) or []
        return [
            {"surface": r[0], "chat_id": r[1], "note": r[2], "status": r[3],
             "created_at": r[4]}
            for r in rows
        ]


__all__ = ["GroupAllowlist"]
