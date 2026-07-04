"""WS-B email dedup — atomic, windowed dedup keyed by RFC Message-ID.

Mirrors ``surfaces/telegram/dedup.py`` (IMAP redelivers on reconnect / overlapping
polls, and Meta-style retries don't apply, but a poll loop can re-see a UID). Atomic
``INSERT OR IGNORE`` so exactly one of two concurrent pollers wins the insert.
"""
from __future__ import annotations

import time
from typing import Optional

from core.sqlite_util import execute_retry, wal_connect


class MessageDedup:
    def __init__(self, db_path: str, window_seconds: float = 7 * 86400) -> None:
        self.db_path = db_path
        self.window_seconds = window_seconds
        conn = wal_connect(db_path)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS seen_messages ("
                "  message_id TEXT PRIMARY KEY,"
                "  ts         REAL NOT NULL"
                ")"
            )
            conn.commit()
        finally:
            conn.close()

    def seen(self, message_id: str, *, now: Optional[float] = None) -> bool:
        """True if ``message_id`` was already processed; False (and records it) if new."""
        if not message_id:
            return False
        ts = time.time() if now is None else now
        # prune stale rows outside the window first
        execute_retry(self.db_path, "DELETE FROM seen_messages WHERE ts < ?",
                      (ts - self.window_seconds,))
        n = execute_retry(
            self.db_path,
            "INSERT OR IGNORE INTO seen_messages (message_id, ts) VALUES (?, ?)",
            (str(message_id), ts),
        )
        return n == 0  # 0 rows inserted -> it was already present
