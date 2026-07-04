"""Surface-agnostic inbound idempotency (generalizes surfaces/telegram/dedup.py).
Keyed by an arbitrary string id (Telegram update_id, WhatsApp message.id, email
Message-ID). Atomic INSERT OR IGNORE + windowed stale-prune; fail-open to 'new' so a
dedup fault never drops a real message."""
import logging
import time as _time
from typing import Optional, Union

from core.sqlite_util import wal_connect, execute_retry

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_messages (
    msg_id TEXT PRIMARY KEY,
    ts     REAL NOT NULL
)
"""


class IdempotencyStore:
    def __init__(self, db_path: str, window_seconds: float = 300.0) -> None:
        self.db_path = db_path
        self.window_seconds = window_seconds
        conn = wal_connect(db_path)
        try:
            conn.execute(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def seen(self, key: Union[str, int], *, now: Optional[float] = None) -> bool:
        ts = now if now is not None else _time.time()
        k = str(key)
        try:
            execute_retry(self.db_path, "DELETE FROM seen_messages WHERE ts < ?",
                          (ts - self.window_seconds,))
            inserted = execute_retry(
                self.db_path,
                "INSERT OR IGNORE INTO seen_messages (msg_id, ts) VALUES (?, ?)",
                (k, ts),
            )
            return inserted == 0
        except Exception as e:  # fail-open: a dedup error must not drop a real message
            logger.error("IdempotencyStore.seen failed for %s: %s", k, e, exc_info=True)
            return False

    def peek(self, key: Union[str, int]) -> bool:
        try:
            row = execute_retry(
                self.db_path, "SELECT 1 FROM seen_messages WHERE msg_id = ? LIMIT 1",
                (str(key),), fetch="one",
            )
            return row is not None
        except Exception as e:
            logger.debug("IdempotencyStore.peek failed for %s: %s", key, e)
            return False
