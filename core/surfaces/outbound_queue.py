"""Durable outbound delivery queue (SQLite WAL). Converts MessageRouter's fire-and-forget
send into at-least-once-with-dedup: publish() enqueues; a dispatcher worker drains with
backoff + token-bucket, dead-lettering after N attempts. idempotency_key (hash of
session_key+turn_id+chunk_idx) dedups a redelivery after a worker crash."""
import logging
import time as _time
from typing import List, Optional

from core.sqlite_util import wal_connect, execute_retry

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbound_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT UNIQUE,
    session_key     TEXT NOT NULL,
    surface_id      TEXT NOT NULL,
    dest            TEXT,
    payload         TEXT NOT NULL,
    media           TEXT,                      -- JSON list of media entries (030 L4)
    kind            TEXT DEFAULT 'agent_text',
    state           TEXT DEFAULT 'pending',   -- pending|inflight|delivered|dead
    attempts        INTEGER DEFAULT 0,
    next_attempt_at REAL DEFAULT 0,
    last_error      TEXT,
    created_at      REAL DEFAULT (strftime('%s','now')),
    updated_at      REAL DEFAULT (strftime('%s','now'))
)
"""


class OutboundDeliveryQueue:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        conn = wal_connect(db_path)
        try:
            conn.execute(_SCHEMA)
            # 030 L4 additive migration: a pre-media queue DB gains the column on
            # open. CREATE IF NOT EXISTS never alters an existing table.
            cols = {r[1] for r in conn.execute("PRAGMA table_info(outbound_queue)")}
            if "media" not in cols:
                conn.execute("ALTER TABLE outbound_queue ADD COLUMN media TEXT")
            conn.commit()
        finally:
            conn.close()

    def enqueue(self, *, idempotency_key: str, session_key: str, surface_id: str,
                dest: Optional[str], payload: str, kind: str = "agent_text",
                media: Optional[list] = None) -> bool:
        media_json: Optional[str] = None
        if media:
            try:
                import json
                media_json = json.dumps(media)
            except (TypeError, ValueError):
                logger.warning("outbound enqueue: media not JSON-serializable, dropped")
        inserted = execute_retry(
            self.db_path,
            """INSERT OR IGNORE INTO outbound_queue
                 (idempotency_key, session_key, surface_id, dest, payload, media, kind, next_attempt_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0)""",
            (idempotency_key, session_key, surface_id, dest, payload, media_json, kind),
        )
        return inserted == 1

    def claim_due(self, now: float, limit: int = 20) -> List[dict]:
        # Two-step claim under WAL: select due ids, then CAS each to 'inflight'.
        rows = execute_retry(
            self.db_path,
            """SELECT * FROM outbound_queue
               WHERE state='pending' AND next_attempt_at <= ?
               ORDER BY id ASC LIMIT ?""",
            (now, limit), fetch="all",
        ) or []
        claimed = []
        for r in rows:
            n = execute_retry(
                self.db_path,
                "UPDATE outbound_queue SET state='inflight', updated_at=? WHERE id=? AND state='pending'",
                (now, r["id"]),
            )
            if n == 1:
                d = dict(r); d["state"] = "inflight"; claimed.append(d)
        return claimed

    def mark_delivered(self, row_id: int) -> None:
        execute_retry(self.db_path,
                      "UPDATE outbound_queue SET state='delivered', updated_at=? WHERE id=?",
                      (_time.time(), row_id))

    def reschedule(self, row_id: int, *, next_attempt_at: float, attempts: int,
                   error: Optional[str] = None) -> None:
        execute_retry(
            self.db_path,
            """UPDATE outbound_queue SET state='pending', attempts=?, next_attempt_at=?,
                 last_error=?, updated_at=? WHERE id=?""",
            (attempts, next_attempt_at, error, _time.time(), row_id),
        )

    def dead_letter(self, row_id: int, error: str) -> None:
        execute_retry(self.db_path,
                      "UPDATE outbound_queue SET state='dead', last_error=?, updated_at=? WHERE id=?",
                      (error, _time.time(), row_id))

    def counts(self) -> dict:
        rows = execute_retry(self.db_path,
                             "SELECT state, COUNT(*) c FROM outbound_queue GROUP BY state",
                             fetch="all") or []
        out = {"pending": 0, "inflight": 0, "delivered": 0, "dead": 0}
        for r in rows:
            out[r["state"]] = r["c"]
        return out

    def reclaim_inflight(self, older_than: float) -> int:
        """Restart-recovery: return long-inflight rows to 'pending' (a worker died mid-send)."""
        return execute_retry(
            self.db_path,
            "UPDATE outbound_queue SET state='pending' WHERE state='inflight' AND updated_at < ?",
            (older_than,),
        ) or 0
