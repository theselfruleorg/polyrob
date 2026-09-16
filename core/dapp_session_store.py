"""Durable dapp-session envelope store (043 A37).

``dapp_connect`` (``tools/dapp_browser/``) arms a wallet inside a browser page
and holds the authorization ENVELOPE — the chain, the per-transaction ceiling,
the whole-session budget, what has been spent, every transaction it signed and
every request it refused — in a process-local ``_bridges`` dict. That dict does
not survive a restart, so after ``polyrob.service`` bounces ``dapp_status`` could
only ever answer "no dapp wallet is connected", even though the owner had armed
one minutes earlier and money had moved. The record of what the wallet DID was
gone with the process.

This is the honest durable shape, mirroring ``core/wake_queue.py``: a small
WAL-backed sqlite table that the tool writes on connect, on every spend, on every
refusal and on disconnect, and reads back when the in-memory bridge is absent.
Registered in ``core/db_manifest.py`` so backup/rollback snapshot it.

⚠️ **This store never resumes a wallet — it only REPORTS one.** The row is read
by ``dapp_status`` for display; nothing rebuilds a spending ``WalletBridge`` from
it. The browser binding a spend needs is gone after a restart, so a persisted
envelope is physically incapable of spending — which is the whole point. And the
write is a REPLACE, not a merge: a reconnect on the same session id overwrites
the row wholesale, exactly as it replaces the in-memory bridge, so a stale
envelope can never keep spending under a new one's budget.

Layering: core-tier, imports only stdlib + ``core.sqlite_util`` /
``core.runtime_paths``. The envelope arrives and leaves as a plain JSON-able
dict — ``core`` may not import ``tools`` (``tests/test_layering_ratchet.py``), so
the tool owns the Envelope↔dict serialization and this store owns only the bytes.

Failure-mode: init/writes fail-open + LOUD (a broken store must never break an
owner's ``dapp_connect`` or a page's spend — the in-memory bridge is authoritative
either way; a lost row only costs the after-restart report). Reads fail-open to
None.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from core.runtime_paths import sidecar_db_path
from core.sqlite_util import execute_retry, wal_connect

logger = logging.getLogger("core.dapp_session_store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dapp_sessions (
    session_id  TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL DEFAULT '',
    envelope    TEXT NOT NULL,
    revoked     INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dapp_user ON dapp_sessions(user_id);
"""


@dataclass
class DappSessionRow:
    session_id: str
    user_id: str
    envelope: Dict[str, Any]
    revoked: bool
    created_at: float
    updated_at: float


class DappSessionStore:
    """Durable dapp-session store. Init/writes fail-open + LOUD; reads fail-open."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._ready = False
        try:
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
            conn = wal_connect(db_path)
            try:
                conn.executescript(_SCHEMA)
                conn.commit()
            finally:
                conn.close()
            self._ready = True
        except Exception as e:
            logger.error(f"dapp_session_store init failed ({db_path}): {e}")

    # -- writer ---------------------------------------------------------------

    def save(self, session_id: str, user_id: str, envelope: Dict[str, Any], *,
             revoked: bool = False) -> None:
        """Write (REPLACE, never merge) the envelope snapshot for ``session_id``.

        ``created_at`` is preserved on an update so the age of the arming is
        honest; the envelope and ``revoked`` are overwritten wholesale, which is
        what makes a reconnect on the same session drop the previous budget's
        spend history instead of carrying it forward. Fail-open + LOUD."""
        if not self._ready or not session_id:
            return
        try:
            blob = json.dumps(envelope or {}, default=str)
        except Exception:
            blob = "{}"
        now = time.time()
        try:
            self._exec(
                "INSERT INTO dapp_sessions "
                "(session_id, user_id, envelope, revoked, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "user_id=excluded.user_id, envelope=excluded.envelope, "
                "revoked=excluded.revoked, updated_at=excluded.updated_at",
                (str(session_id), str(user_id or ""), blob,
                 1 if revoked else 0, now, now))
        except Exception as e:
            logger.error(f"dapp_session_store save failed (session={session_id}): {e}")

    def mark_revoked(self, session_id: str) -> None:
        """Flip a row to revoked without a full snapshot. Fail-open + LOUD."""
        if not self._ready or not session_id:
            return
        self._exec(
            "UPDATE dapp_sessions SET revoked=1, updated_at=? WHERE session_id=?",
            (time.time(), str(session_id)))

    # -- reader ---------------------------------------------------------------

    def get(self, session_id: str, user_id: Optional[str] = None
            ) -> Optional[DappSessionRow]:
        """The persisted row for ``session_id``, or None. When ``user_id`` is
        given and non-empty the read is tenant-scoped (defense in depth — a
        session belongs to one tenant by construction). Fail-open to None."""
        if not self._ready or not session_id:
            return None
        sql = ("SELECT session_id, user_id, envelope, revoked, created_at, "
               "updated_at FROM dapp_sessions WHERE session_id=?")
        params: tuple = (str(session_id),)
        if user_id:
            sql += " AND user_id=?"
            params = (str(session_id), str(user_id))
        try:
            row = execute_retry(self.db_path, sql, params, fetch="one")
        except Exception as e:
            logger.error(f"dapp_session_store get failed (session={session_id}): {e}")
            return None
        return self._to_row(row)

    def list_for_tenant(self, user_id: str) -> List[DappSessionRow]:
        """Every persisted session for ``user_id``, newest first. Fail-open to []."""
        if not self._ready:
            return []
        try:
            rows = execute_retry(
                self.db_path,
                "SELECT session_id, user_id, envelope, revoked, created_at, "
                "updated_at FROM dapp_sessions WHERE user_id=? "
                "ORDER BY updated_at DESC",
                (str(user_id or ""),), fetch="all") or []
        except Exception as e:
            logger.error(f"dapp_session_store list failed: {e}")
            return []
        out = []
        for r in rows:
            parsed = self._to_row(r)
            if parsed is not None:
                out.append(parsed)
        return out

    # -- internals ------------------------------------------------------------

    def _exec(self, sql: str, params: tuple) -> None:
        try:
            execute_retry(self.db_path, sql, params)
        except Exception as e:
            logger.error(f"dapp_session_store write failed: {e}")

    def _to_row(self, row) -> Optional[DappSessionRow]:
        if not row:
            return None
        env_raw = row[2] if not isinstance(row, dict) else row["envelope"]
        try:
            env = json.loads(env_raw) if env_raw else {}
            if not isinstance(env, dict):
                env = {}
        except Exception:
            env = {}
        if isinstance(row, dict):
            return DappSessionRow(
                session_id=row["session_id"], user_id=row["user_id"],
                envelope=env, revoked=bool(row["revoked"]),
                created_at=float(row["created_at"]),
                updated_at=float(row["updated_at"]))
        return DappSessionRow(
            session_id=row[0], user_id=row[1], envelope=env,
            revoked=bool(row[3]), created_at=float(row[4]),
            updated_at=float(row[5]))


# --- process-wide singleton keyed by db path ------------------------------------
_INSTANCES: Dict[str, DappSessionStore] = {}


def default_dapp_session_store_path() -> str:
    """The db_manifest axis (``<data_home>/dapp_sessions.db``)."""
    return str(sidecar_db_path("dapp_sessions.db"))


def get_dapp_session_store(db_path: Optional[str] = None) -> DappSessionStore:
    """Get/create the shared dapp-session store for ``db_path`` (default: the
    data-home axis)."""
    if db_path is None:
        db_path = default_dapp_session_store_path()
    inst = _INSTANCES.get(db_path)
    if inst is None:
        inst = DappSessionStore(db_path)
        _INSTANCES[db_path] = inst
    return inst


__all__ = [
    "DappSessionRow",
    "DappSessionStore",
    "default_dapp_session_store_path",
    "get_dapp_session_store",
]
