"""0008: the post ledger — every delivered proactive send / room reply, with the
surface's OWN message ids, so the agent can name what it posted and delete it.

One table in the bus's ``surfaces.db`` (beside ``group_ledger``), written ONLY by
``MessageRouter`` — the one place that knows a send actually landed — and read by
the `message` action (``action="posts"`` / ``action="delete"``).

⚠️ The ledger IS the ownership proof. A message id that is not on a row here for
that chat is not "my post", and the delete verb refuses it — a bot that is a
group admin could otherwise delete anybody's message.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import List, Optional

from core.secret_scrub import scrub_secret_shapes
from core.sqlite_util import execute_retry

logger = logging.getLogger(__name__)

#: Telegram refuses deleteMessage on a message older than 48 h; a row is kept a
#: while longer so the agent can still NAME what it posted (and say why it can't
#: be removed), then pruned on the next write.
RETENTION_SEC = 14 * 86400
_PREVIEW_CHARS = 200

_DDL = [
    """CREATE TABLE IF NOT EXISTS sent_posts (
        row INTEGER PRIMARY KEY AUTOINCREMENT,
        surface TEXT NOT NULL, chat_id TEXT NOT NULL,
        message_ids TEXT NOT NULL, ts REAL NOT NULL,
        preview TEXT NOT NULL DEFAULT '', deleted_ts REAL)""",
    "CREATE INDEX IF NOT EXISTS sent_posts_chat ON sent_posts(surface, chat_id, ts)",
]


@dataclass
class SentPost:
    row: int
    surface: str
    chat_id: str
    message_ids: List[str]
    ts: float
    preview: str
    deleted_ts: Optional[float]


def _post(r) -> SentPost:
    try:
        ids = [str(i) for i in json.loads(r["message_ids"] or "[]")]
    except (TypeError, ValueError):
        ids = []
    return SentPost(row=int(r["row"]), surface=r["surface"], chat_id=r["chat_id"],
                    message_ids=ids, ts=float(r["ts"]), preview=r["preview"] or "",
                    deleted_ts=r["deleted_ts"])


class SentPosts:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        for stmt in _DDL:
            execute_retry(db_path, stmt)

    def record(self, surface: str, chat_id, message_ids, text: str, *,
               now: Optional[float] = None) -> Optional[int]:
        """Write one delivered send; returns its row, or None when the surface
        reported no id (nothing to address later, so nothing is written)."""
        ids = [str(i) for i in (message_ids or []) if i is not None and str(i) != ""]
        if not ids:
            return None
        ts = float(now if now is not None else time.time())
        preview = scrub_secret_shapes(text or "").strip()
        if len(preview) > _PREVIEW_CHARS:
            preview = preview[:_PREVIEW_CHARS] + "…"
        row = execute_retry(
            self.db_path,
            "INSERT INTO sent_posts(surface,chat_id,message_ids,ts,preview) VALUES(?,?,?,?,?)",
            (str(surface), str(chat_id), json.dumps(ids), ts, preview), fetch="lastrowid")
        try:
            execute_retry(self.db_path, "DELETE FROM sent_posts WHERE ts < ?",
                          (ts - RETENTION_SEC,))
        except Exception:  # pragma: no cover - a prune fault must not lose the write
            logger.debug("sent_posts prune skipped", exc_info=True)
        return int(row) if row is not None else None

    def get(self, row: int) -> Optional[SentPost]:
        r = execute_retry(self.db_path, "SELECT * FROM sent_posts WHERE row=?",
                          (int(row),), fetch="one")
        return _post(r) if r is not None else None

    def recent(self, surface: str, chat_id=None, limit: int = 10, *,
               include_deleted: bool = False) -> List[SentPost]:
        sql = "SELECT * FROM sent_posts WHERE surface=?"
        params: list = [str(surface)]
        if chat_id is not None:
            sql += " AND chat_id=?"
            params.append(str(chat_id))
        if not include_deleted:
            sql += " AND deleted_ts IS NULL"
        sql += " ORDER BY ts DESC, row DESC LIMIT ?"
        params.append(max(1, int(limit)))
        return [_post(r) for r in (execute_retry(self.db_path, sql, tuple(params),
                                                 fetch="all") or [])]

    def find(self, surface: str, chat_id, message_id) -> Optional[SentPost]:
        """The row in THIS chat that carries ``message_id``, or None."""
        mid = str(message_id)
        rows = execute_retry(
            self.db_path,
            "SELECT * FROM sent_posts WHERE surface=? AND chat_id=? ORDER BY ts DESC",
            (str(surface), str(chat_id)), fetch="all") or []
        for r in rows:
            p = _post(r)
            if mid in p.message_ids:
                return p
        return None

    def mark_deleted(self, row: int, *, now: Optional[float] = None) -> None:
        execute_retry(self.db_path, "UPDATE sent_posts SET deleted_ts=? WHERE row=?",
                      (float(now if now is not None else time.time()), int(row)))


__all__ = ["RETENTION_SEC", "SentPost", "SentPosts"]
