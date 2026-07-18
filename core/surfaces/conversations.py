"""E1/E2/E3/E6 (2026-07-13 correspondent review): the durable per-correspondent
conversation container.

Sessions are ephemeral (idle-reset, eviction, compaction); the relationship with an
external party is not. This store is the address-keyed home the architecture lacked:
one row per (tenant, surface, address) with a bounded message log, so that
- a reply weeks later can be answered with real context even after the original
  session's history is compacted or gone ("what did we already say to this person");
- the delivery rail can prepend a compact transcript to every injected reply;
- a dead originating session can be REPLACED (re-pointed) instead of silently
  dropping the correspondent's message;
- email threading has a durable place for the last inbound Message-ID (so our
  replies set In-Reply-To and land in the correspondent's thread).

Pure storage — no surface/transport imports. WAL + jittered retry via
``core/sqlite_util`` (same pattern as the correspondent registry). Message bodies
are capped and each conversation is pruned to the newest ``_PRUNE_KEEP`` rows, so
the store stays a compact digest substrate, not a full mail archive.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import List, Optional

from core.sqlite_util import execute_retry, wal_connect

logger = logging.getLogger(__name__)

_BODY_CAP = 2000        # chars kept per message body
_PRUNE_KEEP = 200       # newest messages kept per conversation


def _norm_addr(address: str) -> str:
    return (address or "").strip().lower()


def _iso(ts: float) -> str:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%MZ")
    except Exception:
        return "?"


class ConversationStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        conn = wal_connect(db_path)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id         TEXT NOT NULL,
                    surface         TEXT NOT NULL,
                    address         TEXT NOT NULL,
                    display_name    TEXT NOT NULL DEFAULT '',
                    state           TEXT NOT NULL DEFAULT 'open',
                    session_id      TEXT NOT NULL DEFAULT '',
                    last_inbound_ts  REAL,
                    last_outbound_ts REAL,
                    last_inbound_mid TEXT NOT NULL DEFAULT '',
                    created_at      REAL NOT NULL,
                    updated_at      REAL NOT NULL,
                    UNIQUE (user_id, surface, address)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_messages (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id INTEGER NOT NULL,
                    direction       TEXT NOT NULL,
                    ts              REAL NOT NULL,
                    mid             TEXT NOT NULL DEFAULT '',
                    subject         TEXT NOT NULL DEFAULT '',
                    body            TEXT NOT NULL,
                    session_id      TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_convmsg_conv "
                "ON conversation_messages(conversation_id, ts)"
            )
            conn.commit()
        finally:
            conn.close()

    # --- write ---------------------------------------------------------------
    def _get_or_create(self, user_id: str, surface: str, address: str,
                       *, now: Optional[float] = None) -> int:
        ts = time.time() if now is None else now
        addr = _norm_addr(address)
        row = execute_retry(
            self.db_path,
            "SELECT id FROM conversations WHERE user_id=? AND surface=? AND address=?",
            (user_id, surface, addr), fetch="one")
        if row is not None:
            return int(row["id"])
        execute_retry(
            self.db_path,
            "INSERT OR IGNORE INTO conversations "
            "(user_id, surface, address, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, surface, addr, ts, ts))
        row = execute_retry(
            self.db_path,
            "SELECT id FROM conversations WHERE user_id=? AND surface=? AND address=?",
            (user_id, surface, addr), fetch="one")
        return int(row["id"])

    def _record(self, direction: str, user_id: str, surface: str, address: str,
                body: str, *, mid: Optional[str] = None, subject: Optional[str] = None,
                session_id: Optional[str] = None, now: Optional[float] = None) -> None:
        ts = time.time() if now is None else now
        conv_id = self._get_or_create(user_id, surface, address, now=ts)
        execute_retry(
            self.db_path,
            "INSERT INTO conversation_messages "
            "(conversation_id, direction, ts, mid, subject, body, session_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (conv_id, direction, ts, mid or "", (subject or "")[:300],
             (body or "")[:_BODY_CAP], session_id or ""))
        sets = ["updated_at=?"]
        params: list = [ts]
        if direction == "in":
            sets.append("last_inbound_ts=?")
            params.append(ts)
            if mid:
                sets.append("last_inbound_mid=?")
                params.append(mid)
        else:
            sets.append("last_outbound_ts=?")
            params.append(ts)
        if session_id:
            sets.append("session_id=?")
            params.append(session_id)
        params.append(conv_id)
        execute_retry(
            self.db_path,
            f"UPDATE conversations SET {', '.join(sets)} WHERE id=?",
            tuple(params))
        # bounded log: keep only the newest _PRUNE_KEEP rows per conversation
        execute_retry(
            self.db_path,
            "DELETE FROM conversation_messages WHERE conversation_id=? AND id NOT IN "
            "(SELECT id FROM conversation_messages WHERE conversation_id=? "
            " ORDER BY ts DESC, id DESC LIMIT ?)",
            (conv_id, conv_id, _PRUNE_KEEP))

    def record_outbound(self, user_id: str, surface: str, address: str, body: str, *,
                        mid: Optional[str] = None, subject: Optional[str] = None,
                        session_id: Optional[str] = None,
                        now: Optional[float] = None) -> None:
        self._record("out", user_id, surface, address, body, mid=mid,
                     subject=subject, session_id=session_id, now=now)

    def record_inbound(self, user_id: str, surface: str, address: str, body: str, *,
                       mid: Optional[str] = None, subject: Optional[str] = None,
                       session_id: Optional[str] = None,
                       now: Optional[float] = None) -> None:
        self._record("in", user_id, surface, address, body, mid=mid,
                     subject=subject, session_id=session_id, now=now)

    def rebind_session(self, user_id: str, surface: str, address: str,
                       session_id: str, *, now: Optional[float] = None) -> None:
        """E6: point the conversation at a replacement session (the original died)."""
        ts = time.time() if now is None else now
        execute_retry(
            self.db_path,
            "UPDATE conversations SET session_id=?, updated_at=? "
            "WHERE user_id=? AND surface=? AND address=?",
            (session_id, ts, user_id, surface, _norm_addr(address)))

    # --- read ----------------------------------------------------------------
    def get(self, user_id: str, surface: str, address: str) -> Optional[dict]:
        row = execute_retry(
            self.db_path,
            "SELECT * FROM conversations WHERE user_id=? AND surface=? AND address=?",
            (user_id, surface, _norm_addr(address)), fetch="one")
        return dict(row) if row is not None else None

    def history(self, user_id: str, surface: str, address: str,
                limit: int = 20) -> List[dict]:
        """Newest-last (chronological) tail of the conversation."""
        conv = self.get(user_id, surface, address)
        if conv is None:
            return []
        rows = execute_retry(
            self.db_path,
            "SELECT * FROM conversation_messages WHERE conversation_id=? "
            "ORDER BY ts DESC, id DESC LIMIT ?",
            (conv["id"], max(1, int(limit))), fetch="all") or []
        return [dict(r) for r in reversed(rows)]

    def outbound_count_since(self, user_id: str, surface: str, address: str,
                             since_secs: float, *, now: Optional[float] = None) -> int:
        """Outbound messages to this address within the window (D1 rounds budget)."""
        ts = time.time() if now is None else now
        conv = self.get(user_id, surface, address)
        if conv is None:
            return 0
        row = execute_retry(
            self.db_path,
            "SELECT COUNT(*) AS n FROM conversation_messages "
            "WHERE conversation_id=? AND direction='out' AND ts >= ?",
            (conv["id"], ts - since_secs), fetch="one")
        return int(row["n"]) if row is not None else 0

    def outbound_count_surface_since(self, user_id: str, surface: str,
                                     since_secs: float, *,
                                     now: Optional[float] = None) -> int:
        """Tenant+surface-wide outbound count within the window (013 T6 open-tier
        daily-send-cap rail). Unlike :meth:`outbound_count_since` (one address),
        this spans every correspondent contacted on the surface — the signal the
        cap check needs (an open policy can reach many new addresses; the cap
        bounds the SURFACE's total outbound volume, not one relationship)."""
        ts = time.time() if now is None else now
        row = execute_retry(
            self.db_path,
            "SELECT COUNT(*) AS n FROM conversation_messages m "
            "JOIN conversations c ON m.conversation_id = c.id "
            "WHERE c.user_id=? AND c.surface=? AND m.direction='out' AND m.ts >= ?",
            (user_id, surface, ts - since_secs), fetch="one")
        return int(row["n"]) if row is not None else 0

    def list(self, user_id: str, limit: int = 100) -> List[dict]:
        rows = execute_retry(
            self.db_path,
            "SELECT * FROM conversations WHERE user_id=? ORDER BY updated_at DESC LIMIT ?",
            (user_id, max(1, int(limit))), fetch="all") or []
        return [dict(r) for r in rows]

    def format_list(self, user_id: str, limit: int = 30) -> str:
        """One line per conversation — 'who did we contact, who replied' at a
        glance (E4 slimmed: the store already tracks this; no campaign subsystem).
        Empty string when the tenant has no conversations."""
        convs = self.list(user_id, limit=limit)
        if not convs:
            return ""
        lines = []
        for c in convs:
            replied = (f"last reply {_iso(c['last_inbound_ts'])}"
                       if c.get("last_inbound_ts") else "no reply yet")
            sent = (f"last sent {_iso(c['last_outbound_ts'])}"
                    if c.get("last_outbound_ts") else "nothing sent")
            lines.append(f"{c['surface']}:{c['address']} — {sent}; {replied}; "
                         f"session {c['session_id'] or '?'}")
        return "\n".join(lines)

    def format_context(self, user_id: str, surface: str, address: str,
                       limit: int = 10, max_chars: int = 4000) -> str:
        """Compact transcript block for prompt injection / owner review.

        Empty string when no conversation exists. The content derives from
        correspondent text, so callers must keep it inside untrusted framing."""
        conv = self.get(user_id, surface, address)
        if conv is None:
            return ""
        msgs = self.history(user_id, surface, address, limit=limit)
        if not msgs:
            return ""
        header = (f"[conversation with {surface}:{_norm_addr(address)} — "
                  f"session {conv['session_id'] or '?'}; "
                  f"last inbound {_iso(conv['last_inbound_ts']) if conv['last_inbound_ts'] else 'never'}; "
                  f"last outbound {_iso(conv['last_outbound_ts']) if conv['last_outbound_ts'] else 'never'}]")
        lines = [header]
        for m in msgs:
            who = "we sent" if m["direction"] == "out" else "they wrote"
            subj = f" (subject: {m['subject']})" if m.get("subject") else ""
            lines.append(f"[{_iso(m['ts'])} {who}{subj}] {m['body']}")
        out = "\n".join(lines)
        if len(out) > max_chars:
            out = out[-max_chars:]
        return out
