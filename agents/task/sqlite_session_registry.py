"""SQLite-backed session registry (roadmap P6 / §33 — the scale unlock).

The in-process :class:`SessionRegistry` forces ``UVICORN_WORKERS=1``: an
orchestrator created in one worker is invisible to another, so a request that
lands on the wrong worker 404s. This variant keeps the **local object dict**
(the live orchestrator can't cross processes) but ALSO mirrors session metadata to
a shared SQLite ``active_sessions`` table (WAL + jittered retry) so every worker
can see which sessions exist and who owns them — the routing foundation for
``workers>1``.

Drop-in compatible with ``SessionRegistry`` (register/get/remove/contains/count/…)
plus cross-process methods: ``exists``, ``owner_pid``, ``global_session_ids``,
``global_count``, ``heartbeat``, ``reap_stale``.

Scope note: this gives cross-worker *visibility*, not transparent cross-worker
*method calls* on a remote orchestrator (that needs sticky routing / IPC — a
follow-up). With it, a worker can return a meaningful "owned by pid N / elsewhere"
result instead of a false 404.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, Iterator, List, Optional, Tuple

from core.sqlite_util import execute_retry


class SqliteSessionRegistry:
    def __init__(self, db_path: str, *, worker_pid: Optional[int] = None) -> None:
        self.db_path = db_path
        self._pid = worker_pid or os.getpid()
        # Per-process boot id: ownership is keyed on this, NOT the (reusable) pid.
        # Two processes that happen to share a pid (PID reuse after a worker dies)
        # still get distinct boot ids, so routing never mistakes one for the other.
        self._boot_id = uuid.uuid4().hex
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._orchestrators: Dict[str, Any] = {}
        self._init_schema()

    def _init_schema(self) -> None:
        # owner_boot_id is in the CREATE so a FRESH DB never needs the ALTER — this
        # avoids the concurrent-boot race where two workers both PRAGMA-see the column
        # missing and both ALTER (the second crashing with "duplicate column name").
        execute_retry(
            self.db_path,
            """
            CREATE TABLE IF NOT EXISTS active_sessions (
                session_id TEXT PRIMARY KEY,
                worker_pid INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                params TEXT NOT NULL DEFAULT '{}',
                last_seen_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                owner_boot_id TEXT
            )
            """,
        )
        # Migration for pre-existing DBs created before owner_boot_id. (No
        # ALTER ... IF NOT EXISTS in SQLite.) Tolerate the "duplicate column name"
        # error so a concurrent boot that adds it first can't crash this worker.
        cols = execute_retry(
            self.db_path, "PRAGMA table_info(active_sessions)", fetch="all",
        ) or []
        names = {row["name"] for row in cols}
        if "owner_boot_id" not in names:
            try:
                execute_retry(
                    self.db_path,
                    "ALTER TABLE active_sessions ADD COLUMN owner_boot_id TEXT",
                )
            except Exception as e:  # concurrent add won the race — that's fine
                if "duplicate column" not in str(e).lower():
                    raise

    # --- local object dict (compatible with SessionRegistry) -----------------

    def register(self, session_id: str, orchestrator: Any, *, params: Optional[Dict[str, Any]] = None) -> None:
        # Write the DB row BEFORE the local dict. route() reads the DB; if the DB
        # write raised after we'd already stored the object locally, route() would
        # return MISSING for a session this worker actually holds — a false 404, the
        # exact failure this class exists to prevent.
        now = datetime.now().isoformat()
        execute_retry(
            self.db_path,
            """
            INSERT INTO active_sessions
                (session_id, worker_pid, owner_boot_id, status, params, last_seen_at, created_at)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(session_id) DO UPDATE SET
                worker_pid=excluded.worker_pid, owner_boot_id=excluded.owner_boot_id,
                status='active', params=excluded.params, last_seen_at=excluded.last_seen_at
            """,
            (session_id, self._pid, self._boot_id, "active", json.dumps(params or {}), now, now),
        )
        self._orchestrators[session_id] = orchestrator

    def get(self, session_id: str) -> Optional[Any]:
        """Return the LOCAL orchestrator object (None if owned by another worker)."""
        return self._orchestrators.get(session_id)

    def remove(self, session_id: str) -> Optional[Any]:
        execute_retry(self.db_path, "DELETE FROM active_sessions WHERE session_id=?", (session_id,))
        return self._orchestrators.pop(session_id, None)

    def contains(self, session_id: str) -> bool:
        return session_id in self._orchestrators

    def count(self) -> int:
        return len(self._orchestrators)

    def items(self) -> List[Tuple[str, Any]]:
        return list(self._orchestrators.items())

    def values(self) -> List[Any]:
        return list(self._orchestrators.values())

    def session_ids(self) -> List[str]:
        return list(self._orchestrators.keys())

    def clear(self) -> None:
        for sid in list(self._orchestrators):
            execute_retry(self.db_path, "DELETE FROM active_sessions WHERE session_id=?", (sid,))
        self._orchestrators.clear()

    # --- cross-process metadata (SQLite) -------------------------------------

    def exists(self, session_id: str) -> bool:
        row = execute_retry(
            self.db_path, "SELECT 1 FROM active_sessions WHERE session_id=?", (session_id,), fetch="one",
        )
        return row is not None

    def route(self, session_id: str):
        """Routing decision (P6): LOCAL (object is in this worker), REMOTE (exists but
        owned by another worker — caller can forward / 409 with owner_pid), or MISSING."""
        from agents.task.session_route import SessionRoute, LOCAL, REMOTE, MISSING
        row = execute_retry(
            self.db_path,
            "SELECT worker_pid, owner_boot_id FROM active_sessions WHERE session_id=?",
            (session_id,), fetch="one",
        )
        if row is None:
            return SessionRoute(status=MISSING)
        owner = int(row["worker_pid"])
        owner_boot = row["owner_boot_id"]
        # LOCAL only when the OWNING boot id is ours (pid match alone is not enough:
        # a reused pid in a fresh process must not be treated as the same owner).
        local = self._orchestrators.get(session_id)
        if owner_boot == self._boot_id and local is not None:
            return SessionRoute(status=LOCAL, orchestrator=local, owner_pid=owner)
        return SessionRoute(status=REMOTE, owner_pid=owner)

    def owner_pid(self, session_id: str) -> Optional[int]:
        row = execute_retry(
            self.db_path, "SELECT worker_pid FROM active_sessions WHERE session_id=?", (session_id,), fetch="one",
        )
        return int(row["worker_pid"]) if row else None

    def owner_boot_id(self, session_id: str) -> Optional[str]:
        row = execute_retry(
            self.db_path, "SELECT owner_boot_id FROM active_sessions WHERE session_id=?", (session_id,), fetch="one",
        )
        return row["owner_boot_id"] if row else None

    def global_session_ids(self) -> List[str]:
        rows = execute_retry(self.db_path, "SELECT session_id FROM active_sessions", fetch="all")
        return [r["session_id"] for r in rows]

    def global_count(self) -> int:
        row = execute_retry(self.db_path, "SELECT COUNT(*) AS c FROM active_sessions", fetch="one")
        return int(row["c"]) if row else 0

    def heartbeat(self, session_id: str) -> None:
        execute_retry(
            self.db_path, "UPDATE active_sessions SET last_seen_at=? WHERE session_id=?",
            (datetime.now().isoformat(), session_id),
        )

    @staticmethod
    def _pid_alive(pid: Optional[int]) -> bool:
        """True if ``pid`` is a live process on THIS host.

        Same-host multi-worker (uvicorn workers) is the supported ``workers>1``
        deployment, so a signal-0 liveness probe is meaningful. Be conservative:
        an unknown/None pid or any unexpected error is treated as ALIVE so we never
        reap a possibly-live owner. (A cross-host DB would need a heartbeat-based
        liveness signal; that deployment is out of scope for the live orchestrator.)
        """
        if not pid or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists but owned by another user
        except Exception:
            return True

    def reap_stale(self, ttl_seconds: int = 300) -> List[str]:
        """Remove sessions whose last_seen_at is older than ttl. Returns reaped ids.

        A row is reaped ONLY if it is truly abandoned: stale AND not held locally AND
        its owning worker process is DEAD. A session sitting IDLE between user turns
        does not step, so its heartbeat freezes — but it is still alive in its owning
        worker and must never be reaped (deleting the row would force a false-404 and
        leak that worker's orchestrator). Rows we hold locally, and rows whose
        ``worker_pid`` is still alive on this host, are skipped; only genuinely
        dead-worker rows are cleaned up. Complements the per-step heartbeat.
        """
        cutoff = (datetime.now() - timedelta(seconds=ttl_seconds)).isoformat()
        # Sessions alive in THIS process, regardless of heartbeat staleness.
        local_sids = set(self._orchestrators.keys())
        rows = execute_retry(
            self.db_path,
            "SELECT session_id, worker_pid FROM active_sessions WHERE last_seen_at < ?",
            (cutoff,), fetch="all",
        )
        # Reap only rows that are (a) not ours and (b) owned by a DEAD process. A
        # stale row whose owner worker is still alive is an idle session on another
        # worker — deleting it would false-404 and orphan that worker's orchestrator.
        reaped = [
            r["session_id"] for r in rows
            if r["session_id"] not in local_sids and not self._pid_alive(r["worker_pid"])
        ]
        for sid in reaped:
            # Re-check under the cutoff: don't delete a row that heartbeated between
            # the SELECT and the DELETE (and re-skip if it somehow became local).
            if sid in self._orchestrators:
                continue
            execute_retry(
                self.db_path,
                "DELETE FROM active_sessions WHERE session_id=? AND last_seen_at < ?",
                (sid, cutoff),
            )
            self._orchestrators.pop(sid, None)
        return reaped

    # test helper: force a row's last_seen_at (e.g. into the past) for reap tests.
    def _set_last_seen(self, session_id: str, iso: str) -> None:
        execute_retry(self.db_path, "UPDATE active_sessions SET last_seen_at=? WHERE session_id=?", (iso, session_id))

    # backwards-compatible alias
    _set_last_seen_for_test = _set_last_seen

    # --- dict-flavoured conveniences -----------------------------------------

    def __contains__(self, session_id: str) -> bool:
        return session_id in self._orchestrators

    def __len__(self) -> int:
        return len(self._orchestrators)

    def __iter__(self) -> Iterator[str]:
        return iter(list(self._orchestrators.keys()))
