"""SqliteMessageStore — durable, queryable per-session message history.

Drop-in alternative to the JSON-blob persistence (PersistenceMixin.save_to_disk),
opt-in via MESSAGE_STORE_BACKEND=sqlite. WAL + jittered retry via core/sqlite_util,
matching the SESSION_REGISTRY_BACKEND=sqlite precedent. Stores each message as a JSON
row keyed by (session_id, seq) so ordering and per-session isolation are preserved.
"""
import json
import logging
import os
from typing import Any, Dict, List

from core.sqlite_util import execute_retry, wal_connect

logger = logging.getLogger(__name__)


class SqliteMessageStore:
    def __init__(self, db_path: str):
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.db_path = db_path
        self._init_schema()

    def _init_schema(self) -> None:
        conn = wal_connect(self.db_path)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS messages ("
                "  session_id TEXT NOT NULL,"
                "  seq INTEGER NOT NULL,"
                "  payload TEXT NOT NULL,"
                "  PRIMARY KEY (session_id, seq)"
                ")"
            )
            conn.commit()
        finally:
            conn.close()

    def append(self, session_id: str, message: Dict[str, Any]) -> None:
        row = execute_retry(
            self.db_path,
            "SELECT COALESCE(MAX(seq), -1) AS m FROM messages WHERE session_id = ?",
            (session_id,),
            fetch="one",
        )
        next_seq = (row["m"] if row else -1) + 1
        execute_retry(
            self.db_path,
            "INSERT INTO messages (session_id, seq, payload) VALUES (?, ?, ?)",
            (session_id, next_seq, json.dumps(message)),
        )

    def replace_all(self, session_id: str, messages: List[Dict[str, Any]]) -> None:
        """Atomically replace every row for a session in ONE connection/transaction.

        Preferred over clear()+append()-loop: avoids the SELECT-MAX+INSERT PK race
        (seq is assigned by enumerate inside the txn) and collapses ~2n+1 connection
        open/close cycles per save down to 1 (MED-4).
        """
        conn = wal_connect(self.db_path)
        try:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.executemany(
                "INSERT INTO messages (session_id, seq, payload) VALUES (?, ?, ?)",
                [(session_id, i, json.dumps(m)) for i, m in enumerate(messages)],
            )
            conn.commit()
        finally:
            conn.close()

    def load(self, session_id: str) -> List[Dict[str, Any]]:
        rows = execute_retry(
            self.db_path,
            "SELECT payload FROM messages WHERE session_id = ? ORDER BY seq ASC",
            (session_id,),
            fetch="all",
        )
        return [json.loads(r["payload"]) for r in (rows or [])]

    def clear(self, session_id: str) -> None:
        execute_retry(
            self.db_path, "DELETE FROM messages WHERE session_id = ?", (session_id,)
        )
