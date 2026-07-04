"""P4: Telegram update_id dedup over WAL SQLite (atomic CAS).

Telegram redelivers the same update on a webhook-ack timeout, so the inbound handler
must drop a repeat BEFORE the side-effecting steps (identify -> get_or_create_by_tg_id
writes; route_inbound -> create_session). A SELECT-then-INSERT check races when two
deliveries of the same update arrive concurrently; `INSERT OR IGNORE` is the atomic
compare-and-set — exactly one caller gets rowcount==1 (process), the rest get 0 (drop).

5-minute window (Telegram's retry horizon is minutes); stale rows are pruned before
each check so a genuine redelivery after the window is processed again.
"""
import logging
import time as _time
from typing import Optional, Union

from core.sqlite_util import wal_connect, execute_retry

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_updates (
    update_id TEXT PRIMARY KEY,
    ts        REAL NOT NULL
)
"""


class UpdateDedup:
    def __init__(self, db_path: str, window_seconds: float = 300.0) -> None:
        self.db_path = db_path
        self.window_seconds = window_seconds
        conn = wal_connect(db_path)
        try:
            conn.execute(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def seen(self, update_id: Union[str, int], *, now: Optional[float] = None) -> bool:
        """Return True if this update was already seen (drop it), False if new (process).

        Atomic via INSERT OR IGNORE: a concurrent redelivery of the same update_id
        resolves to exactly one False (the winner) and the rest True.
        """
        ts = now if now is not None else _time.time()
        key = str(update_id)
        try:
            # Prune stale rows first so a post-window redelivery is reprocessable.
            execute_retry(
                self.db_path,
                "DELETE FROM seen_updates WHERE ts < ?",
                (ts - self.window_seconds,),
            )
            inserted = execute_retry(
                self.db_path,
                "INSERT OR IGNORE INTO seen_updates (update_id, ts) VALUES (?, ?)",
                (key, ts),
            )
            return inserted == 0  # 0 == row already existed (within window) -> duplicate
        except Exception as e:  # fail-open: a dedup error must not drop a real update
            logger.error("UpdateDedup.seen failed for %s: %s", key, e, exc_info=True)
            return False

    def peek(self, update_id: Union[str, int]) -> bool:
        """Non-mutating: True if this update_id is already recorded, WITHOUT claiming it.

        Used to suppress a cosmetic pre-send (e.g. a 'Transcribing…' status bubble) on a
        redelivery, while the AUTHORITATIVE claim still happens later in seen() inside
        process_update. Never call this in place of seen() for routing — it does not
        record. Fail-open to False (treat as new) so a peek error never suppresses a
        legitimate status."""
        key = str(update_id)
        try:
            row = execute_retry(
                self.db_path,
                "SELECT 1 FROM seen_updates WHERE update_id = ? LIMIT 1",
                (key,),
                fetch="one",
            )
            return row is not None
        except Exception as e:  # fail-open
            logger.debug("UpdateDedup.peek failed for %s: %s", key, e)
            return False
