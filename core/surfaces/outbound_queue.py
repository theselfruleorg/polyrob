"""Durable outbound delivery queue (SQLite WAL). Converts MessageRouter's fire-and-forget
send into at-least-once-with-dedup: publish() enqueues; a dispatcher worker drains with
backoff + token-bucket, dead-lettering after N attempts.

``idempotency_key`` (OB2, 2026-10-03 audit): ONE key per message — :func:`message_key`
mints it from a fresh per-message id plus a sha256 of the body. The old key was
``session#turn#hash(text)`` with ``turn`` = the constant session key and Python's
per-process salted ``hash()``, so a second identical line ("Done.") in a session was
reported queued and never sent. Only a ``pending``/``inflight`` row is live; delivered
and dead rows age out through :meth:`OutboundDeliveryQueue.prune` (OB15).

A claimed row carries a lease (``claim_token``, OB6): the claimer renews it before
each send, and ``reclaim_inflight`` returns only rows whose lease is older than
``INFLIGHT_LEASE_SEC`` — and voids the old token, so the late claimer skips."""
import hashlib
import logging
import time as _time
import uuid
from typing import Iterable, List, Optional

from core.sqlite_util import wal_connect, execute_retry

logger = logging.getLogger(__name__)

#: How long a claimed row may stay ``inflight`` without a renewed lease before
#: another process may return it to ``pending`` (OB6). The claimer renews before
#: every send, so only a single send longer than this can be sent twice.
INFLIGHT_LEASE_SEC = 300.0

#: Retention of terminal rows (OB15). Delivered rows are kept a week for
#: forensics; dead letters two weeks, because the status snapshot names them.
DELIVERED_RETENTION_SEC = 7 * 86400
DEAD_RETENTION_SEC = 14 * 86400


def message_key(scope: str, text: str) -> str:
    """The idempotency key for ONE outbound message (OB2).

    A fresh id per message, so two identical bodies are two messages; the
    sha256 of the body (stable across processes, unlike ``hash()``) keeps a key
    readable as "this body"."""
    digest = hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()[:16]
    return f"{scope}#{uuid.uuid4().hex}#{digest}"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbound_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT UNIQUE,
    session_key     TEXT NOT NULL,
    surface_id      TEXT NOT NULL,
    dest            TEXT,
    payload         TEXT NOT NULL,
    media           TEXT,                      -- JSON list of media entries (030 L4)
    kind            TEXT DEFAULT 'agent_text',
    state           TEXT DEFAULT 'pending',   -- pending|inflight|delivered|dead
    attempts        INTEGER DEFAULT 0,
    next_attempt_at REAL DEFAULT 0,
    last_error      TEXT,
    created_at      REAL DEFAULT (strftime('%s','now')),
    updated_at      REAL DEFAULT (strftime('%s','now'))
)
"""


class OutboundDeliveryQueue:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        conn = wal_connect(db_path)
        try:
            conn.execute(_SCHEMA)
            # 030 L4 additive migration: a pre-media queue DB gains the column on
            # open. CREATE IF NOT EXISTS never alters an existing table.
            cols = {r[1] for r in conn.execute("PRAGMA table_info(outbound_queue)")}
            if "media" not in cols:
                conn.execute("ALTER TABLE outbound_queue ADD COLUMN media TEXT")
            if "claim_token" not in cols:   # OB6 lease
                conn.execute("ALTER TABLE outbound_queue ADD COLUMN claim_token TEXT")
            conn.commit()
        finally:
            conn.close()

    def enqueue(self, *, idempotency_key: str, session_key: str, surface_id: str,
                dest: Optional[str], payload: str, kind: str = "agent_text",
                media: Optional[list] = None) -> bool:
        media_json: Optional[str] = None
        if media:
            import json
            # OB17: stamp each file's identity now; the drain refuses a file
            # that was swapped (e.g. for a symlink) while the row waited.
            from core.surfaces.attachments import stamp_identity
            media = stamp_identity(media)
            try:
                media_json = json.dumps(media)
            except (TypeError, ValueError) as exc:
                # OB22: dropping the media and accepting the row delivered a
                # message without its attachment while reporting success. Refuse
                # the row; the router falls back to a direct (in-process) send.
                raise ValueError(f"outbound enqueue: media not JSON-serializable: {exc}")
        inserted = execute_retry(
            self.db_path,
            """INSERT OR IGNORE INTO outbound_queue
                 (idempotency_key, session_key, surface_id, dest, payload, media, kind, next_attempt_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0)""",
            (idempotency_key, session_key, surface_id, dest, payload, media_json, kind),
        )
        return inserted == 1

    #: Row states in which an already-present row means the message is still on
    #: its way. A ``dead`` row means the opposite: the queue gave up on it, so a
    #: caller must NOT read the ``INSERT OR IGNORE`` no-op as acceptance (D5,
    #: 2026-09-21 interface audit). OB2: ``delivered`` is not live either — a
    #: key collision with a delivered row is not "this message is queued".
    LIVE_STATES = ("pending", "inflight")

    def row_state(self, idempotency_key: str) -> Optional[str]:
        """The state of the row under *idempotency_key*, or None if there is none.

        Exists so ``enqueue`` returning False can be interpreted honestly: the
        key already existed (still queued, or already delivered) versus the row
        was dead-lettered and nothing will retry it.
        """
        row = execute_retry(
            self.db_path,
            "SELECT state FROM outbound_queue WHERE idempotency_key = ?",
            (idempotency_key,), fetch="one")
        return None if row is None else str(row["state"])

    def accepted(self, idempotency_key: str) -> bool:
        """True when a row under this key exists AND is not dead-lettered."""
        return self.row_state(idempotency_key) in self.LIVE_STATES

    def claim_due(self, now: float, limit: int = 20, *,
                  surfaces: Optional[Iterable[str]] = None,
                  token: Optional[str] = None) -> List[dict]:
        """Claim due rows. ``surfaces`` (OB5) restricts the claim to the surfaces
        THIS process hosts — every process drains the shared ``outbox.db``, and a
        process without the surface used to burn the row's attempts on "no
        surface". ``None`` = every surface (legacy); an empty set claims nothing.
        ``token`` is the claimer's lease id (OB6)."""
        # Two-step claim under WAL: select due ids, then CAS each to 'inflight'.
        sql = ("SELECT * FROM outbound_queue WHERE state='pending' AND next_attempt_at <= ?")
        params: list = [now]
        if surfaces is not None:
            sids = sorted({str(x) for x in surfaces})
            if not sids:
                return []
            sql += f" AND surface_id IN ({','.join('?' for _ in sids)})"
            params.extend(sids)
        sql += " ORDER BY id ASC LIMIT ?"
        params.append(limit)
        rows = execute_retry(self.db_path, sql, tuple(params), fetch="all") or []
        claimed = []
        for r in rows:
            n = execute_retry(
                self.db_path,
                "UPDATE outbound_queue SET state='inflight', updated_at=?, claim_token=? "
                "WHERE id=? AND state='pending'",
                (now, token, r["id"]),
            )
            if n == 1:
                d = dict(r); d["state"] = "inflight"; d["claim_token"] = token
                claimed.append(d)
        return claimed

    def renew(self, row_id: int, token: Optional[str]) -> bool:
        """Renew this claimer's lease on an inflight row (OB6). False = the row
        was reclaimed (or finished) by another process: do NOT send it."""
        if token is None:
            return True
        n = execute_retry(
            self.db_path,
            "UPDATE outbound_queue SET updated_at=? "
            "WHERE id=? AND state='inflight' AND claim_token=?",
            (_time.time(), row_id, token),
        )
        return n == 1

    def mark_delivered(self, row_id: int) -> None:
        execute_retry(self.db_path,
                      "UPDATE outbound_queue SET state='delivered', updated_at=? WHERE id=?",
                      (_time.time(), row_id))

    def reschedule(self, row_id: int, *, next_attempt_at: float, attempts: int,
                   error: Optional[str] = None) -> None:
        execute_retry(
            self.db_path,
            """UPDATE outbound_queue SET state='pending', attempts=?, next_attempt_at=?,
                 last_error=?, updated_at=? WHERE id=?""",
            (attempts, next_attempt_at, error, _time.time(), row_id),
        )

    def set_payload(self, row_id: int, payload: str) -> None:
        """Replace a row's text with what is still undelivered (OB7: a partial
        send resumes from the next chunk instead of re-sending the first)."""
        execute_retry(self.db_path,
                      "UPDATE outbound_queue SET payload=?, updated_at=? WHERE id=?",
                      (payload, _time.time(), row_id))

    def dead_letter(self, row_id: int, error: str) -> None:
        execute_retry(self.db_path,
                      "UPDATE outbound_queue SET state='dead', last_error=?, updated_at=? WHERE id=?",
                      (error, _time.time(), row_id))

    def counts(self) -> dict:
        rows = execute_retry(self.db_path,
                             "SELECT state, COUNT(*) c FROM outbound_queue GROUP BY state",
                             fetch="all") or []
        out = {"pending": 0, "inflight": 0, "delivered": 0, "dead": 0}
        for r in rows:
            out[r["state"]] = r["c"]
        return out

    def dead_letters(self, limit: int = 5) -> List[dict]:
        """The most recent dead-lettered rows — a message the queue GAVE UP on.

        Read-only. Surfaced by the status snapshot's ``delivery`` section (D22):
        until 2026-09-21 ``counts()`` had no caller at all, so a dead letter was
        a message the owner never received and no seat could name.
        """
        rows = execute_retry(
            self.db_path,
            "SELECT id, surface_id, dest, kind, attempts, last_error, updated_at "
            "FROM outbound_queue WHERE state='dead' ORDER BY updated_at DESC LIMIT ?",
            (int(limit),), fetch="all") or []
        return [dict(r) for r in rows]

    def reclaim_inflight(self, older_than: Optional[float] = None) -> int:
        """Restart-recovery: return rows whose lease expired to 'pending' (a worker
        died mid-send). Voids the old ``claim_token``, so a slow claimer that
        comes back fails :meth:`renew` and does not send a second copy (OB6).
        Default cutoff: ``now - INFLIGHT_LEASE_SEC``."""
        if older_than is None:
            older_than = _time.time() - INFLIGHT_LEASE_SEC
        return execute_retry(
            self.db_path,
            "UPDATE outbound_queue SET state='pending', claim_token=NULL "
            "WHERE state='inflight' AND updated_at < ?",
            (older_than,),
        ) or 0

    def prune(self, now: Optional[float] = None, *,
              delivered_retention: float = DELIVERED_RETENTION_SEC,
              dead_retention: float = DEAD_RETENTION_SEC) -> int:
        """Delete terminal rows past their retention (OB15). Never touches a
        ``pending``/``inflight`` row. Returns the number of rows removed."""
        ts = _time.time() if now is None else float(now)
        return execute_retry(
            self.db_path,
            "DELETE FROM outbound_queue WHERE "
            "(state='delivered' AND updated_at < ?) OR (state='dead' AND updated_at < ?)",
            (ts - delivered_retention, ts - dead_retention),
        ) or 0
