"""Tracks the WhatsApp 24h customer-service window per recipient (last inbound time)."""
from typing import Optional
from core.sqlite_util import wal_connect, execute_retry

_SCHEMA = """CREATE TABLE IF NOT EXISTS wa_window (
    wa_phone TEXT PRIMARY KEY, last_inbound REAL NOT NULL)"""


class WindowTracker:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        conn = wal_connect(db_path)
        try:
            conn.execute(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def touch(self, wa_phone: str, *, now: float) -> None:
        execute_retry(
            self.db_path,
            """INSERT INTO wa_window (wa_phone, last_inbound) VALUES (?, ?)
               ON CONFLICT(wa_phone) DO UPDATE SET last_inbound=excluded.last_inbound""",
            (wa_phone, now),
        )

    def last_inbound(self, wa_phone: str) -> Optional[float]:
        row = execute_retry(
            self.db_path,
            "SELECT last_inbound FROM wa_window WHERE wa_phone=?",
            (wa_phone,),
            fetch="one",
        )
        return float(row["last_inbound"]) if row else None
