"""SessionChatRegistry: pure chat-scoped session-key + durable chat<->session map.

build_session_key is PURE and CHAT-scoped. The SQLite map is what
lets an inbound update resolve to its existing session cross-worker without an
in-process handle.
"""
from typing import Optional

from core.surfaces.envelopes import SessionSource
from core.sqlite_util import wal_connect, execute_retry

_PREFIX = "agent:main"


def build_session_key(source: SessionSource, user_id: Optional[str] = None) -> str:
    base = f"{_PREFIX}:{source.surface_id}:{source.chat_type}:{source.chat_id}"
    if source.chat_type == "dm" and user_id:
        base = f"{base}:{user_id}"
    if source.thread_id:
        base = f"{base}:thread:{source.thread_id}"
    return base


def chat_id_from_session_key(session_key: str) -> str:
    """The inverse of :func:`build_session_key`'s chat segment (030 WS-B3/E1).

    Chat-scoped keys ``agent:main:{surface}:{type}:{chat}[...]`` → index 4; the
    cron back-compat shim ``direct:{surface}:{chat}`` → last segment; any other
    shape falls back to the last segment. This 6-line parse used to be copied
    verbatim into every surface module — ONE inverse next to the builder now.
    """
    parts = session_key.split(":")
    if parts and parts[0] == "direct":
        return parts[-1]
    if len(parts) >= 5:
        return parts[4]
    return parts[-1] if parts else session_key


def row_from_session_key(session_key: str) -> Optional[dict]:
    """The binding a chat-scoped key IMPLIES — for when no durable row exists.

    044 T20 fix round 2 (N1): ``bind_chat_surface`` is the ONLY writer of
    ``session_chat_map``, so a room the agent has been allowlisted into but that
    has never triggered a LIVE turn has no row at all. A service run in that room
    found work, paid for the model call, and then had every reply dropped at
    DEBUG by ``MessageRouter.publish`` because ``resolve`` returned None — while
    the checkpoint advanced, so the lines were gone.

    The key is not a lookup token, it is an ADDRESS:
    ``agent:main:{surface}:{chat_type}:{chat_id}[…]`` (see
    :func:`build_session_key`). Reading it back is exact and needs no write.
    Returns None for anything that is not a chat-scoped key of that shape —
    never a guess.
    """
    parts = str(session_key or "").split(":")
    if len(parts) < 5 or ":".join(parts[:2]) != _PREFIX:
        return None
    surface_id, chat_type = parts[2].strip(), parts[3].strip()
    chat_id = chat_id_from_session_key(session_key)
    if not surface_id or not chat_type or not str(chat_id or "").strip():
        return None
    return {"surface_id": surface_id, "chat_id": str(chat_id),
            "chat_type": chat_type, "session_id": None, "user_id": None}


_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_chat_map (
    session_key TEXT PRIMARY KEY,
    session_id  TEXT NOT NULL,
    user_id     TEXT,
    surface_id  TEXT,
    chat_id     TEXT,
    owner_pid   INTEGER,
    updated_at  REAL DEFAULT (strftime('%s','now'))
)
"""


class SessionChatRegistry:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        conn = wal_connect(db_path)
        try:
            conn.execute(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def bind(self, session_key: str, session_id: str, user_id: str,
             surface_id: str, chat_id: str) -> None:
        execute_retry(
            self.db_path,
            """INSERT INTO session_chat_map
                 (session_key, session_id, user_id, surface_id, chat_id)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(session_key) DO UPDATE SET
                 session_id=excluded.session_id, user_id=excluded.user_id,
                 surface_id=excluded.surface_id, chat_id=excluded.chat_id,
                 updated_at=strftime('%s','now')""",
            (session_key, session_id, user_id, surface_id, chat_id),
        )

    def resolve(self, session_key: str) -> Optional[dict]:
        row = execute_retry(
            self.db_path,
            "SELECT * FROM session_chat_map WHERE session_key = ?",
            (session_key,), fetch="one",
        )
        return dict(row) if row is not None else None

    def set_owner(self, session_key: str, owner_pid: int) -> None:
        execute_retry(
            self.db_path,
            "UPDATE session_chat_map SET owner_pid = ? WHERE session_key = ?",
            (owner_pid, session_key),
        )

    def resolve_by_session_id(self, session_id: str) -> Optional[dict]:
        """Reverse lookup: the chat binding row for a session_id (newest if several).
        Used to re-attach the outbound surface to a recreated orchestrator (#0)."""
        row = execute_retry(
            self.db_path,
            "SELECT * FROM session_chat_map WHERE session_id = ? ORDER BY updated_at DESC LIMIT 1",
            (session_id,), fetch="one",
        )
        return dict(row) if row is not None else None

    def touch(self, session_key: str) -> None:
        """Bump updated_at to now (last-activity clock for the idle boundary, P0.1)."""
        execute_retry(
            self.db_path,
            "UPDATE session_chat_map SET updated_at = strftime('%s','now') WHERE session_key = ?",
            (session_key,),
        )

    def delete(self, session_key: str) -> None:
        """Remove a chat<->session binding (explicit /new, or stale-row GC, P0.3)."""
        execute_retry(
            self.db_path,
            "DELETE FROM session_chat_map WHERE session_key = ?",
            (session_key,),
        )

    def purge_stale(self, older_than_secs: float) -> int:
        """Delete bindings whose last activity is older than ``older_than_secs`` and
        return the number removed (a5 surface GC). A chat<->session row is just a
        routing pointer; the on-disk session is GC'd separately, so dropping a long-idle
        pointer only means the next message from that chat starts a fresh thread."""
        cutoff = "strftime('%s','now') - ?"
        return execute_retry(
            self.db_path,
            f"DELETE FROM session_chat_map WHERE updated_at < {cutoff}",
            (older_than_secs,),
        ) or 0
