"""044 T12: the ONE room log. Every allowed-room message, attributed, bounded,
secret-scrubbed. Readers: the room turn (tail), the service goal (checkpoint),
the owner seats (/groups tail), the status snapshot (count). Never a user turn."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import List, Optional

from core.env import int_env
from core.secret_scrub import scrub_secret_shapes
from core.sqlite_util import execute_retry

logger = logging.getLogger(__name__)

_DDL = [
    """CREATE TABLE IF NOT EXISTS group_ledger (
        surface TEXT NOT NULL, chat_id TEXT NOT NULL, thread_id TEXT,
        message_id TEXT NOT NULL, ts REAL NOT NULL,
        sender_id TEXT NOT NULL DEFAULT '', sender_name TEXT NOT NULL DEFAULT '',
        sender_is_bot INTEGER NOT NULL DEFAULT 0, role_at_write TEXT NOT NULL DEFAULT 'member',
        kind TEXT NOT NULL DEFAULT 'text', text TEXT NOT NULL DEFAULT '',
        reply_to_message_id TEXT, mentions_bot INTEGER NOT NULL DEFAULT 0,
        answered_by TEXT, media_path TEXT,
        PRIMARY KEY (surface, chat_id, message_id))""",
    "CREATE INDEX IF NOT EXISTS group_ledger_ts ON group_ledger(surface, chat_id, ts)",
    """CREATE TABLE IF NOT EXISTS group_ledger_checkpoints (
        surface TEXT NOT NULL, chat_id TEXT NOT NULL, reader TEXT NOT NULL,
        ts REAL NOT NULL DEFAULT 0, PRIMARY KEY (surface, chat_id, reader))""",
]


@dataclass
class LedgerRow:
    surface: str
    chat_id: str
    thread_id: Optional[str]
    message_id: str
    ts: float
    sender_id: str
    sender_name: str
    sender_is_bot: bool
    role_at_write: str
    kind: str
    text: str
    reply_to_message_id: Optional[str]
    mentions_bot: bool
    answered_by: Optional[str] = None
    #: 044 §4.1: a media row stores the caption (in ``text``) and the workspace
    #: path the inbound rail already wrote — never the bytes.
    media_path: Optional[str] = None


class GroupLedger:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        for stmt in _DDL:
            execute_retry(db_path, stmt)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns a ledger created by an EARLIER build is missing.

        ``CREATE TABLE IF NOT EXISTS`` is a no-op on an existing table, so a new
        column in ``_DDL`` never reaches a db that already exists — the T12 rows
        on a live box would keep the old shape and every read of the new column
        would raise. Idempotent, guarded by ``PRAGMA table_info``."""
        try:
            cols = {r[1] for r in (execute_retry(
                self.db_path, "PRAGMA table_info(group_ledger)", fetch="all") or [])}
            if "media_path" not in cols:
                execute_retry(self.db_path,
                              "ALTER TABLE group_ledger ADD COLUMN media_path TEXT")
        except Exception:  # pragma: no cover - a migration fault must not kill the surface
            logger.warning("group_ledger migration skipped", exc_info=True)

    def append(self, row: LedgerRow) -> None:
        text = scrub_secret_shapes(row.text or "")
        execute_retry(
            self.db_path,
            """INSERT INTO group_ledger(surface,chat_id,thread_id,message_id,ts,sender_id,sender_name,
                 sender_is_bot,role_at_write,kind,text,reply_to_message_id,mentions_bot,media_path)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(surface,chat_id,message_id) DO UPDATE SET
                 text=CASE WHEN excluded.kind='edit' THEN excluded.text ELSE group_ledger.text END,
                 kind=CASE WHEN excluded.kind='edit' THEN 'edit' ELSE group_ledger.kind END""",
            (row.surface, str(row.chat_id), row.thread_id, str(row.message_id), float(row.ts),
             str(row.sender_id or ""), str(row.sender_name or ""), int(bool(row.sender_is_bot)),
             row.role_at_write or "member", row.kind or "text", text,
             row.reply_to_message_id, int(bool(row.mentions_bot)), row.media_path),
        )
        self.prune(row.surface, row.chat_id)

    def tail(self, surface: str, chat_id: str, *, thread_id: Optional[str] = None,
             since_ts: Optional[float] = None, limit: int = 200,
             unanswered_only: bool = False) -> List[LedgerRow]:
        # Prune-on-read (round-1 review): a quiet room only re-evaluates its
        # wall-clock retention cutoff on append, so without this a reader
        # (the room turn, the service goal, /groups tail) could still observe
        # expired rows between the last append and the next one. Cheap and
        # idempotent — mirrors prune-on-append.
        self.prune(surface, chat_id)
        sql = "SELECT * FROM group_ledger WHERE surface=? AND chat_id=?"
        params: list = [surface, str(chat_id)]
        if thread_id is not None:
            sql += " AND thread_id=?"; params.append(str(thread_id))
        if since_ts is not None:
            sql += " AND ts>?"; params.append(float(since_ts))
        if unanswered_only:
            # ⚠️ 2026-09-16: the agent's OWN lines are exempt from the burn.
            # `unanswered_only` means "a line nobody has answered yet", which is
            # a question only a HUMAN line can be asked. A bot row marked
            # answered (it is shown as context, so `mark_room_lines_answered`
            # stamps it like any other) would vanish from the next tail — and
            # since the agent's replies live nowhere else durable, the room's
            # whole log would again show one side of the conversation. Keeping
            # them costs a little context budget, which `select_context_rows`
            # already bounds newest-first.
            sql += " AND (answered_by IS NULL OR sender_is_bot = 1)"
        sql += " ORDER BY ts DESC LIMIT ?"; params.append(int(limit))
        rows = execute_retry(self.db_path, sql, tuple(params), fetch="all") or []
        out = [LedgerRow(surface=r["surface"], chat_id=r["chat_id"], thread_id=r["thread_id"],
                         message_id=r["message_id"], ts=r["ts"], sender_id=r["sender_id"],
                         sender_name=r["sender_name"], sender_is_bot=bool(r["sender_is_bot"]),
                         role_at_write=r["role_at_write"], kind=r["kind"], text=r["text"],
                         reply_to_message_id=r["reply_to_message_id"],
                         mentions_bot=bool(r["mentions_bot"]), answered_by=r["answered_by"],
                         media_path=(r["media_path"] if "media_path" in r.keys() else None))
               for r in rows]
        out.reverse()
        return out

    def mark_answered(self, surface: str, chat_id: str, message_ids: List[str], session_id: str) -> None:
        for mid in message_ids:
            execute_retry(self.db_path,
                          "UPDATE group_ledger SET answered_by=? WHERE surface=? AND chat_id=? AND message_id=?",
                          (session_id, surface, str(chat_id), str(mid)))

    def checkpoint(self, surface: str, chat_id: str, reader: str) -> float:
        row = execute_retry(self.db_path,
                            "SELECT ts FROM group_ledger_checkpoints WHERE surface=? AND chat_id=? AND reader=?",
                            (surface, str(chat_id), reader), fetch="one")
        return float(row["ts"]) if row else 0.0

    def advance(self, surface: str, chat_id: str, reader: str, ts: float) -> None:
        execute_retry(self.db_path,
                      """INSERT INTO group_ledger_checkpoints(surface,chat_id,reader,ts) VALUES(?,?,?,?)
                         ON CONFLICT(surface,chat_id,reader) DO UPDATE SET ts=MAX(ts, excluded.ts)""",
                      (surface, str(chat_id), reader, float(ts)))

    def count(self, surface: str, chat_id: str) -> int:
        row = execute_retry(self.db_path, "SELECT COUNT(*) FROM group_ledger WHERE surface=? AND chat_id=?",
                            (surface, str(chat_id)), fetch="one")
        return int(row[0]) if row else 0

    def prune(self, surface: str, chat_id: str) -> None:
        max_rows = int_env("GROUP_LEDGER_MAX_ROWS_PER_CHAT", 2000)
        days = int_env("GROUP_LEDGER_RETENTION_DAYS", 14)
        # Wall-clock, not chat-relative: a chat's age window must expire even
        # when the chat goes quiet and receives no further messages to
        # re-evaluate the cutoff against (round-1 review finding — anchoring
        # to MAX(ts) for the chat meant a quiet room's rows NEVER aged out,
        # which violates the T7 privacy retention bound).
        execute_retry(self.db_path, "DELETE FROM group_ledger WHERE surface=? AND chat_id=? AND ts<?",
                      (surface, str(chat_id), time.time() - days * 86400))
        execute_retry(self.db_path,
                      """DELETE FROM group_ledger WHERE surface=? AND chat_id=? AND message_id IN (
                           SELECT message_id FROM group_ledger WHERE surface=? AND chat_id=?
                           ORDER BY ts DESC LIMIT -1 OFFSET ?)""",
                      (surface, str(chat_id), surface, str(chat_id), max_rows))

    def purge_chat(self, surface: str, chat_id: str) -> None:
        execute_retry(self.db_path, "DELETE FROM group_ledger WHERE surface=? AND chat_id=?", (surface, str(chat_id)))
        execute_retry(self.db_path, "DELETE FROM group_ledger_checkpoints WHERE surface=? AND chat_id=?", (surface, str(chat_id)))
