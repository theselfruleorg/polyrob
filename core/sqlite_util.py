"""Small shared SQLite helper: WAL connection + jittered write-retry (roadmap P6).

Centralizes the WAL-with-DELETE-fallback + 'database is locked' retry pattern so
the cross-process session registry (and, later, the cron store) don't each
re-implement it. Mirrors Reference ``SessionDB``: WAL for concurrent readers + one
writer, DELETE fallback on NFS/SMB/FUSE, and 20–150ms jitter to avoid SQLite's
deterministic backoff convoy under contention.
"""
from __future__ import annotations

import random
import sqlite3
import time
from typing import Optional

_MAX_RETRIES = 15
_BUSY_TIMEOUT_S = 1.0


def wal_connect(db_path: str, timeout: float = _BUSY_TIMEOUT_S) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=timeout)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        conn.execute("PRAGMA journal_mode=DELETE")
    return conn


def execute_retry(db_path: str, sql: str, params: tuple = (), *, fetch: Optional[str] = None):
    """Execute one statement with jittered retry on write contention.

    fetch: None -> rowcount; 'one' -> a Row or None; 'all' -> list[Row].
    """
    last_err: Optional[Exception] = None
    for _ in range(_MAX_RETRIES):
        try:
            conn = wal_connect(db_path)
            try:
                cur = conn.execute(sql, params)
                if fetch == "one":
                    row = cur.fetchone()
                    conn.commit()
                    return row
                if fetch == "all":
                    rows = cur.fetchall()
                    conn.commit()
                    return rows
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() or "busy" in str(e).lower():
                last_err = e
                time.sleep(random.uniform(0.02, 0.15))
                continue
            raise
    raise last_err  # type: ignore[misc]
