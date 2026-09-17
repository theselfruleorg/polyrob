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
CREATE TABLE IF NOT EXISTS {table} (
    {key_col} TEXT PRIMARY KEY,
    ts        REAL NOT NULL
)
"""


class IdempotencyStore:
    """Atomic INSERT OR IGNORE + windowed prune over one ``(id, ts)`` table.

    ``table``/``key_col`` exist so the Telegram ``UpdateDedup`` (which
    predates this store and whose prod ``tg_dedup.db`` already holds
    ``seen_updates(update_id, ts)``) can be the SAME class over its existing
    table rather than a byte-for-byte twin.
    """
    table = "seen_messages"
    key_col = "msg_id"

    def __init__(self, db_path: str, window_seconds: float = 300.0) -> None:
        self.db_path = db_path
        self.window_seconds = window_seconds
        conn = wal_connect(db_path)
        try:
            conn.execute(_SCHEMA.format(table=self.table, key_col=self.key_col))
            conn.commit()
        finally:
            conn.close()

    def seen(self, key: Union[str, int], *, now: Optional[float] = None) -> bool:
        """True if *key* was already seen (drop it), False if new (process).
        Atomic: a concurrent redelivery resolves to exactly one False."""
        ts = now if now is not None else _time.time()
        k = str(key)
        try:
            # Prune stale rows first so a post-window redelivery is reprocessable.
            execute_retry(self.db_path, f"DELETE FROM {self.table} WHERE ts < ?",
                          (ts - self.window_seconds,))
            inserted = execute_retry(
                self.db_path,
                f"INSERT OR IGNORE INTO {self.table} ({self.key_col}, ts) VALUES (?, ?)",
                (k, ts),
            )
            return inserted == 0
        except Exception as e:  # fail-open: a dedup error must not drop a real message
            logger.error("%s.seen failed for %s: %s", type(self).__name__, k, e,
                         exc_info=True)
            return False

    def peek(self, key: Union[str, int]) -> bool:
        """Non-mutating: True if *key* is already recorded, WITHOUT claiming it.
        Never call this in place of seen() for routing. Fail-open to False."""
        try:
            row = execute_retry(
                self.db_path,
                f"SELECT 1 FROM {self.table} WHERE {self.key_col} = ? LIMIT 1",
                (str(key),), fetch="one",
            )
            return row is not None
        except Exception as e:
            logger.debug("IdempotencyStore.peek failed for %s: %s", key, e)
            return False
