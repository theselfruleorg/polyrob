"""Surface-agnostic inbound idempotency (generalizes surfaces/telegram/dedup.py).
Keyed by an arbitrary string id (Telegram update_id, WhatsApp message.id, email
Message-ID). Atomic INSERT OR IGNORE + windowed stale-prune; fail-open to 'new' so a
dedup fault never drops a real message."""
import logging
import time as _time
from typing import Optional, Union

from core.sqlite_util import wal_connect, execute_retry

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS {table} (
    {key_col} TEXT PRIMARY KEY,
    ts        REAL NOT NULL
)
"""


class IdempotencyStore:
    """Atomic INSERT OR IGNORE + windowed prune over one ``(id, ts)`` table.

    ``table``/``key_col`` exist so the Telegram ``UpdateDedup`` (which
    predates this store and whose prod ``tg_dedup.db`` already holds
    ``seen_updates(update_id, ts)``) can be the SAME class over its existing
    table rather than a byte-for-byte twin.
    """
    table = "seen_messages"
    key_col = "msg_id"

    def __init__(self, db_path: str, window_seconds: float = 300.0) -> None:
        self.db_path = db_path
        self.window_seconds = window_seconds
        conn = wal_connect(db_path)
        try:
            conn.execute(_SCHEMA.format(table=self.table, key_col=self.key_col))
            conn.commit()
        finally:
            conn.close()

    def seen(self, key: Union[str, int], *, now: Optional[float] = None) -> bool:
        """True if *key* was already seen (drop it), False if new (process).
        Atomic: a concurrent redelivery resolves to exactly one False."""
        ts = now if now is not None else _time.time()
        k = str(key)
        try:
            # Prune stale rows first so a post-window redelivery is reprocessable.
            execute_retry(self.db_path, f"DELETE FROM {self.table} WHERE ts < ?",
                          (ts - self.window_seconds,))
            inserted = execute_retry(
                self.db_path,
                f"INSERT OR IGNORE INTO {self.table} ({self.key_col}, ts) VALUES (?, ?)",
                (k, ts),
            )
            return inserted == 0
        except Exception as e:  # fail-open: a dedup error must not drop a real message
            logger.error("%s.seen failed for %s: %s", type(self).__name__, k, e,
                         exc_info=True)
            return False

    def peek(self, key: Union[str, int]) -> bool:
        """Non-mutating: True if *key* is already recorded, WITHOUT claiming it.
        Never call this in place of seen() for routing. Fail-open to False."""
        try:
            row = execute_retry(
                self.db_path,
                f"SELECT 1 FROM {self.table} WHERE {self.key_col} = ? LIMIT 1",
                (str(key),), fetch="one",
            )
            return row is not None
        except Exception as e:
            logger.debug("IdempotencyStore.peek failed for %s: %s", key, e)
            return False

    def claim_permanent(self, keys) -> bool:
        """Atomically consume every key once, or none if any was consumed.

        For signed authorizations: never expire claims and propagate store errors.
        Use a separate store from windowed message deduplication.
        """
        keys = tuple(dict.fromkeys(str(key) for key in keys))
        if not keys:
            raise ValueError("at least one replay key is required")
        conn = wal_connect(self.db_path)
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                for key in keys:
                    if conn.execute(f"SELECT 1 FROM {self.table} WHERE {self.key_col}=?",
                                    (key,)).fetchone():
                        return False
                conn.executemany(f"INSERT INTO {self.table} ({self.key_col}, ts) VALUES (?, ?)",
                                 [(key, _time.time()) for key in keys])
            return True
        finally:
            conn.close()


_ACCEPTED_SCHEMA = """
CREATE TABLE IF NOT EXISTS accepted_events (
    event_key  TEXT PRIMARY KEY,
    surface_id TEXT NOT NULL,
    body       BLOB NOT NULL,
    state      TEXT NOT NULL DEFAULT 'accepted',
    attempts   INTEGER NOT NULL DEFAULT 0,
    ts         REAL NOT NULL
)
"""

from core.state.machine import StateMachine  # noqa: E402

#: 098 CO-18: ``accepted → processing → (row deleted) | accepted | failed``.
ACCEPTED_EVENT = StateMachine(
    "surfaces.accepted_event",
    states={"accepted", "processing", "failed"},
    transitions={"accepted": {"processing"},
                 "processing": {"accepted", "failed"}},
    terminal={"failed"},
    initial={"accepted"},
)


class AcceptedEventJournal:
    """Persist-before-ack for a webhook surface (064 F4).

    A platform with a short ack budget (WeCom, Kakao, WeChat OA, DingTalk:
    ~5 s) must get its 200 before the agent turn runs. The VERIFIED raw event
    is written here as ``accepted`` first; the ack goes out; a background drain
    processes it and deletes the row. A crash between the ack and the turn
    leaves the row, and :meth:`recover` hands it back once on restart. The
    per-message :class:`IdempotencyStore` records completed handlers, so retries
    skip completed messages in a batch. An interrupted handler is at least once:
    effects before a crash need their own tool-level idempotency protection.

    ``state``: ``accepted`` → ``processing`` → (row deleted) | ``failed``
    after ``max_attempts``. A ``failed`` row stays for an operator to read.

    098 CO-18: a ``processing`` row names its claimant (``claimed_by`` =
    :func:`core.state.proc.process_identity`). ``recover`` hands back only rows
    whose claimant is no longer alive, so a second process that hosts the same
    webhook surface never re-runs an event the first is still processing; and
    ``complete``/``release`` act only on the caller's own claim.

    Unlike :class:`IdempotencyStore` this is NOT fail-open on write: if the
    event cannot be persisted, :meth:`accept` raises and the caller processes
    inline instead of acking an event it could lose.
    """

    def __init__(self, db_path: str, *, max_attempts: int = 3) -> None:
        self.db_path = db_path
        self.max_attempts = max_attempts
        conn = wal_connect(db_path)
        try:
            conn.execute(_ACCEPTED_SCHEMA)
            from core.sqlite_util import ensure_columns
            ensure_columns(conn, "accepted_events", (("claimed_by", "TEXT"),))
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _me() -> str:
        from core.state.proc import process_identity
        return process_identity().encode()

    def accept(self, key: str, surface_id: str, body: bytes, *,
               now: Optional[float] = None) -> bool:
        """Record the event. True = new (drain it); False = already journaled."""
        ts = now if now is not None else _time.time()
        inserted = execute_retry(
            self.db_path,
            "INSERT OR IGNORE INTO accepted_events (event_key, surface_id, body, ts) "
            "VALUES (?, ?, ?, ?)",
            (str(key), surface_id, bytes(body), ts),
        )
        return inserted == 1

    def claim(self, key: str) -> Optional[bytes]:
        """Atomically move one ``accepted`` row to ``processing`` under this
        process's identity; its body, or None when another drain holds it."""
        from core.state.sql import cas_transition
        if not cas_transition(self.db_path, "accepted_events", key="event_key=?",
                              key_params=(str(key),), machine=ACCEPTED_EVENT,
                              to="processing", from_states=("accepted",),
                              set_cols={"claimed_by": self._me()},
                              status_col="state"):
            return None
        execute_retry(self.db_path,
                      "UPDATE accepted_events SET attempts=attempts+1 WHERE event_key=?",
                      (str(key),))
        row = execute_retry(self.db_path,
                            "SELECT body FROM accepted_events WHERE event_key=?",
                            (str(key),), fetch="one")
        return bytes(row["body"]) if row else None

    def complete(self, key: str) -> None:
        """Delete the row this process claimed (a legacy row without a claimant
        too). A row another live process re-claimed is left alone."""
        execute_retry(self.db_path,
                      "DELETE FROM accepted_events WHERE event_key=? "
                      "AND (claimed_by IS NULL OR claimed_by=?)",
                      (str(key), self._me()))

    def release(self, key: str) -> None:
        """A drain failed: back to ``accepted``, or ``failed`` past the cap.
        Only the claimant's own ``processing`` row moves."""
        from core.state.sql import cas_transition
        row = execute_retry(self.db_path,
                            "SELECT attempts FROM accepted_events WHERE event_key=?",
                            (str(key),), fetch="one")
        if row is None:
            return
        to = "failed" if int(row["attempts"] or 0) >= self.max_attempts else "accepted"
        cas_transition(self.db_path, "accepted_events", key="event_key=?",
                       key_params=(str(key),), machine=ACCEPTED_EVENT, to=to,
                       from_states=("processing",),
                       set_cols={"claimed_by": None},
                       where_extra="claimed_by IS NULL OR claimed_by=?",
                       where_params=(self._me(),), status_col="state")

    def pending(self, surface_id: str) -> list:
        rows = execute_retry(
            self.db_path,
            "SELECT event_key FROM accepted_events WHERE surface_id=? AND state='accepted' "
            "ORDER BY ts", (surface_id,), fetch="all")
        return [r["event_key"] for r in rows or []]

    def recover(self, surface_id: str) -> list:
        """On start, BEFORE the surface takes a request: a ``processing`` row
        whose claimant is no longer alive is handed back (CO-18: a live
        claimant keeps its row). Returns the keys to drain."""
        from core.state.proc import ProcIdentity, is_alive
        from core.state.sql import cas_transition
        rows = execute_retry(
            self.db_path,
            "SELECT event_key, claimed_by FROM accepted_events "
            "WHERE surface_id=? AND state='processing'", (surface_id,), fetch="all") or []
        me = self._me()
        for r in rows:
            owner = r["claimed_by"]
            if owner and owner != me and is_alive(ProcIdentity.decode(owner)):
                continue
            extra, params = (("claimed_by=?", (owner,)) if owner
                             else ("claimed_by IS NULL", ()))
            cas_transition(self.db_path, "accepted_events", key="event_key=?",
                           key_params=(r["event_key"],), machine=ACCEPTED_EVENT,
                           to="accepted", from_states=("processing",),
                           set_cols={"claimed_by": None}, where_extra=extra,
                           where_params=params, status_col="state")
        return self.pending(surface_id)
