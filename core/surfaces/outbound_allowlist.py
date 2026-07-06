"""Owner-scoped outbound send-authorization. Default-DENY: a (user_id, surface,
target) must have an ACTIVE row to be sendable. Owner targets bypass this (resolved
upstream in outbound_target.resolve_target_tier). Tenant-scoped, WAL+jitter via
``core/sqlite_util`` (no hand-rolled retry loop) — mirrors ``agents/task/goals/board.py``.
"""
from __future__ import annotations

import os
import time
from typing import Dict, List

from core.sqlite_util import execute_retry

_DDL = """
CREATE TABLE IF NOT EXISTS outbound_allowlist (
    user_id TEXT NOT NULL,
    surface TEXT NOT NULL,
    target  TEXT NOT NULL,
    note    TEXT NOT NULL DEFAULT '',
    status  TEXT NOT NULL DEFAULT 'active',
    created_at REAL NOT NULL,
    PRIMARY KEY (user_id, surface, target)
);
"""


class OutboundAllowlist:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        execute_retry(self.db_path, _DDL)

    def allow(self, user_id: str, surface: str, target: str, note: str = "") -> None:
        execute_retry(
            self.db_path,
            "INSERT INTO outbound_allowlist(user_id,surface,target,note,status,created_at)"
            " VALUES(?,?,?,?, 'active', ?)"
            " ON CONFLICT(user_id,surface,target) DO UPDATE SET status='active', note=excluded.note",
            (user_id, surface, target, note, time.time()),
        )

    def revoke(self, user_id: str, surface: str, target: str) -> bool:
        rc = execute_retry(
            self.db_path,
            "UPDATE outbound_allowlist SET status='revoked'"
            " WHERE user_id=? AND surface=? AND target=? AND status='active'",
            (user_id, surface, target),
        )
        return bool(rc)

    def is_allowed(self, user_id: str, surface: str, target: str) -> bool:
        row = execute_retry(
            self.db_path,
            "SELECT 1 FROM outbound_allowlist"
            " WHERE user_id=? AND surface=? AND target=? AND status='active'",
            (user_id, surface, target),
            fetch="one",
        )
        return row is not None

    def list(self, user_id: str) -> List[Dict]:
        rows = execute_retry(
            self.db_path,
            "SELECT surface,target,note,status,created_at FROM outbound_allowlist"
            " WHERE user_id=? ORDER BY created_at DESC",
            (user_id,),
            fetch="all",
        ) or []
        return [
            {
                "surface": r["surface"],
                "target": r["target"],
                "note": r["note"],
                "status": r["status"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]
