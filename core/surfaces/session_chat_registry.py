"""SessionChatRegistry: pure chat-scoped session-key + durable chat<->session map.

build_session_key is PURE and CHAT-scoped. The SQLite map is what
lets an inbound update resolve to its existing session cross-worker without an
in-process handle.
"""
from typing import Optional

from core.surfaces.envelopes import SessionSource
import re

from core.sqlite_util import wal_connect, execute_retry

_PREFIX = "agent:main"


def build_session_key(source: SessionSource, user_id: Optional[str] = None) -> str:
    base = f"{_PREFIX}:{source.surface_id}:{source.chat_type}:{source.chat_id}"
    if source.chat_type == "dm" and user_id:
        base = f"{base}:{user_id}"
    if source.thread_id:
        base = f"{base}:thread:{source.thread_id}"
    return base


def admin_session_key(room_key: str, raw_uid: object) -> str:
    """The room+admin session (101). ⚠️ ``:admin:`` goes BEFORE ``:thread:``:
    ``message_router`` splits on ``:thread:`` and takes the tail as the thread id."""
    base, sep, thread = str(room_key).partition(":thread:")
    return f"{base}:admin:{raw_uid}{sep}{thread}"


#: The slug of a seat's default chat. Its key is the seat's base key, unchanged — so
#: every chat that existed before 113 is its seat's main chat with no re-bind.
MAIN_SLUG = "main"
_CHAT_SEP = ":chat:"
_SLUG_RE = re.compile(r"c[1-9][0-9]{0,5}")


def chat_session_key(base_key: str, slug: str) -> str:
    """The key of chat ``slug`` on the seat whose base key is ``base_key`` (113).
    ``main`` is the base key itself. ⚠️ ``:chat:`` goes BEFORE ``:thread:``, for the
    same reason as :func:`admin_session_key`: readers split on ``:thread:`` and take
    the tail as the thread id."""
    slug = str(slug or MAIN_SLUG).strip().lower()
    if slug == MAIN_SLUG:
        return str(base_key)
    base, sep, thread = str(base_key).partition(":thread:")
    return f"{base}{_CHAT_SEP}{slug}{sep}{thread}"


def split_chat_key(session_key: str) -> tuple:
    """``(base_key, slug)`` — the inverse of :func:`chat_session_key`."""
    head, sep, thread = str(session_key or "").partition(":thread:")
    base, chat_sep, slug = head.rpartition(_CHAT_SEP)
    if not chat_sep or not _SLUG_RE.fullmatch(slug):   # a chat id that reads "chat" is not a slug
        return (str(session_key or ""), MAIN_SLUG)
    return (f"{base}{sep}{thread}", slug)


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


#: 113: a chat's own facts live on its row (one store). ``base_key`` is the seat's key
#: without the chat part; ``slug`` is ``main`` or ``c<n>``; ``focused`` is 1 on the one
#: open chat of a ``base_key`` that the seat writes into; a chat that has no session yet
#: (opened, nothing said) holds ``session_id=''``.
_CHAT_COLUMNS = (("base_key", "TEXT"), ("slug", "TEXT"), ("label", "TEXT"),
                 ("created_at", "REAL"), ("closed_at", "REAL"), ("focused", "INTEGER"))


class SessionChatRegistry:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        conn = wal_connect(db_path)
        try:
            conn.execute(_SCHEMA)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(session_chat_map)")}
            for col, typ in _CHAT_COLUMNS:
                if col not in cols:
                    try:
                        conn.execute(f"ALTER TABLE session_chat_map ADD COLUMN {col} {typ}")
                    except Exception as exc:  # another process added it first
                        if "duplicate column" not in str(exc).lower():
                            raise
            # Every row written before 113 is its seat's main chat, in focus.
            conn.execute(
                "UPDATE session_chat_map SET base_key = session_key, slug = ?, focused = 1, "
                "created_at = updated_at WHERE base_key IS NULL", (MAIN_SLUG,))
            conn.execute("CREATE INDEX IF NOT EXISTS idx_session_chat_base "
                         "ON session_chat_map(base_key)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_session_chat_user "
                         "ON session_chat_map(user_id)")
            conn.commit()
        finally:
            conn.close()

    def bind(self, session_key: str, session_id: str, user_id: str,
             surface_id: str, chat_id: str) -> None:
        """Point the chat ``session_key`` at ``session_id``. A new row is its seat's
        focused chat only when that seat has no open focused chat; an existing row
        keeps its name, its focus and its open/closed state."""
        base_key, slug = split_chat_key(session_key)
        execute_retry(
            self.db_path,
            """INSERT INTO session_chat_map
                 (session_key, session_id, user_id, surface_id, chat_id,
                  base_key, slug, created_at, focused)
               VALUES (?, ?, ?, ?, ?, ?, ?, strftime('%s','now'),
                       NOT EXISTS (SELECT 1 FROM session_chat_map
                                   WHERE base_key = ? AND focused = 1 AND closed_at IS NULL))
               ON CONFLICT(session_key) DO UPDATE SET
                 session_id=excluded.session_id, user_id=excluded.user_id,
                 surface_id=excluded.surface_id, chat_id=excluded.chat_id,
                 updated_at=strftime('%s','now')""",
            (session_key, session_id, user_id, surface_id, chat_id, base_key, slug, base_key),
        )

    def resolve(self, session_key: str) -> Optional[dict]:
        """The chat<->session BINDING, or None. A chat that was opened and has no
        session yet (``session_id=''``) is not a binding: its first line starts one."""
        row = execute_retry(
            self.db_path,
            "SELECT * FROM session_chat_map WHERE session_key = ?",
            (session_key,), fetch="one",
        )
        return dict(row) if row is not None and row["session_id"] else None

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
        # 113: a chat the person NAMED or opened beside main is theirs; only an unnamed
        # main pointer (the pre-113 row) ages out. ⚠️ A CLOSED chat stays too: its slug
        # is its key, and a freed slug would hand the next `/new` the dead chat's key
        # (its episodes, its model pin). `/chat <name>` also opens it again.
        return execute_retry(
            self.db_path,
            f"DELETE FROM session_chat_map WHERE updated_at < {cutoff} "
            "AND COALESCE(slug, 'main') = 'main' AND COALESCE(label, '') = ''",
            (older_than_secs,),
        ) or 0

    # --- 113: the chat's own facts (the logic is core/surfaces/chats.py) ------------

    def chat_row(self, session_key: str) -> Optional[dict]:
        """The raw chat row, bound or not (``resolve`` hides a chat with no session)."""
        row = execute_retry(self.db_path,
                            "SELECT * FROM session_chat_map WHERE session_key = ?",
                            (session_key,), fetch="one")
        return dict(row) if row is not None else None

    def chat_rows(self, *, user_id: str, base_key: Optional[str] = None) -> list:
        """Every chat row of ONE tenant (optionally one seat). Raises on a store fault:
        the caller reports "could not read", never an empty list."""
        sql, params = "SELECT * FROM session_chat_map WHERE user_id = ?", [str(user_id)]
        if base_key is not None:
            sql += " AND base_key = ?"
            params.append(str(base_key))
        rows = execute_retry(self.db_path, sql, tuple(params), fetch="all") or []
        return [dict(r) for r in rows]

    def chat_rows_for_base(self, base_key: str) -> list:
        """Every chat row of one seat key, whatever the tenant (the caller filters)."""
        rows = execute_retry(self.db_path,
                             "SELECT * FROM session_chat_map WHERE base_key = ?",
                             (str(base_key),), fetch="all") or []
        return [dict(r) for r in rows]

    def focused_key(self, base_key: str, user_id: Optional[str] = None) -> str:
        """The key a new line on this seat goes to: its focused open chat, else
        ``base_key`` (main). With ``user_id`` a row of another tenant is never the
        answer. Raises on a store fault (``chats.focused_key`` is the fail-open form)."""
        sql = ("SELECT session_key FROM session_chat_map WHERE base_key = ? "
               "AND focused = 1 AND closed_at IS NULL")
        params = [str(base_key)]
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(str(user_id))
        row = execute_retry(self.db_path, sql + " LIMIT 1", tuple(params), fetch="one")
        return str(row["session_key"]) if row is not None else str(base_key)

    def insert_chat(self, session_key: str, *, user_id: str, surface_id: str,
                    chat_id: str, label: str = "") -> bool:
        """Create a chat that has no session yet. False when the key exists."""
        base_key, slug = split_chat_key(session_key)
        return bool(execute_retry(
            self.db_path,
            """INSERT OR IGNORE INTO session_chat_map
                 (session_key, session_id, user_id, surface_id, chat_id,
                  base_key, slug, label, created_at, focused)
               VALUES (?, '', ?, ?, ?, ?, ?, ?, strftime('%s','now'), 0)""",
            (session_key, str(user_id), surface_id, str(chat_id), base_key, slug, label or "")))

    def set_focus(self, base_key: str, session_key: str) -> None:
        """One statement, so a seat never has two focused chats."""
        execute_retry(self.db_path,
                      "UPDATE session_chat_map SET focused = (session_key = ?) WHERE base_key = ?",
                      (session_key, base_key))

    def set_label(self, session_key: str, label: str) -> None:
        execute_retry(self.db_path,
                      "UPDATE session_chat_map SET label = ? WHERE session_key = ?",
                      (label or "", session_key))

    def set_closed(self, session_key: str, closed: bool = True) -> None:
        execute_retry(
            self.db_path,
            "UPDATE session_chat_map SET closed_at = CASE WHEN ? THEN strftime('%s','now') "
            "ELSE NULL END, focused = CASE WHEN ? THEN 0 ELSE focused END WHERE session_key = ?",
            (1 if closed else 0, 1 if closed else 0, session_key))
