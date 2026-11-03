"""Per-message inbound state for the email surface (098 TS-20).

Before this, the poison counter lived in process memory (a restart reset it)
and a message whose fetch raised was skipped with a warning every poll, for
ever, without a trace the owner could see. Each inbound message now has ONE
durable row in the surface's dedup DB, and every move is a guarded write:

    fetched ──▶ routed ──▶ handled
       │  └──────────────▶ handled      (denied / duplicate: nothing to route)
       ▼
    failed(n) ──▶ fetched              (the next poll tries again)
       └──────▶ quarantined            (n reached the cap: \\Seen + ONE owner notice)

``routed`` means the turn was dispatched. A crash after ``routed`` and before
the ``\\Seen`` mark re-fetches the message on the next poll; the row says it was
already dispatched, so the poll only marks it — it never runs the turn twice.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional

from core.sqlite_util import execute_retry, wal_connect
from core.state.machine import StateMachine
from core.state.sql import cas_transition

INBOUND = StateMachine(
    "email.inbound",
    states={"fetched", "routed", "handled", "failed", "quarantined"},
    transitions={
        "fetched": {"routed", "handled", "failed"},
        "routed": {"handled"},
        "failed": {"fetched", "quarantined"},
    },
    terminal={"handled", "quarantined"},
    initial={"fetched"},
)

_TABLE = "inbound_state"


class InboundState:
    """The ``inbound_state`` table in ``email_dedup.db``."""

    def __init__(self, db_path: str, *, window_seconds: float = 30 * 86400) -> None:
        self.db_path = db_path
        self.window_seconds = window_seconds
        conn = wal_connect(db_path)
        try:
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {_TABLE} ("
                "  key        TEXT PRIMARY KEY,"
                "  state      TEXT NOT NULL,"
                "  attempts   INTEGER NOT NULL DEFAULT 0,"
                "  last_error TEXT,"
                "  sender     TEXT,"
                "  subject    TEXT,"
                "  updated_at REAL NOT NULL"
                ")")
            conn.commit()
        finally:
            conn.close()

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        row = execute_retry(self.db_path, f"SELECT key, state, attempts, last_error, sender, "
                            f"subject, updated_at FROM {_TABLE} WHERE key=?", (key,),
                            fetch="one")
        return dict(row) if row is not None else None

    def begin(self, key: str, *, sender: str = "", subject: str = "") -> Dict[str, Any]:
        """The row for a message this poll fetched: a new row starts ``fetched``;
        a ``failed`` row moves back to ``fetched`` for its next attempt; any
        other row is returned as it is (the caller acts on its state)."""
        now = time.time()
        self._prune(now)
        execute_retry(self.db_path,
                      f"INSERT OR IGNORE INTO {_TABLE} (key, state, attempts, sender, subject, "
                      f"updated_at) VALUES (?, 'fetched', 0, ?, ?, ?)",
                      (key, (sender or "")[:300], (subject or "")[:300], now))
        row = self.get(key) or {"key": key, "state": "fetched", "attempts": 0}
        if row["state"] == "failed":
            self.move(key, "failed", "fetched")
            row["state"] = "fetched"
        return row

    def move(self, key: str, frm: str, to: str, **cols: Any) -> bool:
        cols["updated_at"] = time.time()
        return cas_transition(self.db_path, _TABLE, key="key=?", key_params=(key,),
                              machine=INBOUND, to=to, from_states=(frm,), set_cols=cols,
                              status_col="state")

    def fail(self, key: str, error: str, *, max_attempts: int) -> Optional[str]:
        """``fetched -> failed`` with the attempt counted; then
        ``failed -> quarantined`` when the count reached ``max_attempts``.
        Returns the new state (``None`` when another poller moved the row)."""
        row = self.get(key)
        if not row or row["state"] != "fetched":
            return None
        n = int(row["attempts"] or 0) + 1
        if not cas_transition(self.db_path, _TABLE, key="key=?", key_params=(key,),
                              machine=INBOUND, to="failed", from_states=("fetched",),
                              set_cols={"attempts": n, "last_error": str(error)[:500],
                                        "updated_at": time.time()},
                              where_extra="attempts=?", where_params=(row["attempts"],),
                              status_col="state"):
            return None
        if n >= max_attempts and self.move(key, "failed", "quarantined"):
            return "quarantined"
        return "failed"

    def _prune(self, now: float) -> None:
        execute_retry(self.db_path,
                      f"DELETE FROM {_TABLE} WHERE state IN ('handled','quarantined') "
                      f"AND updated_at < ?", (now - self.window_seconds,))
