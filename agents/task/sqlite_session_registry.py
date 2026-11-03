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

098 AG-16 — row ownership is a small machine: ``absent -> owned(boot) -> absent``
and ``owned(dead owner) -> owned(new boot)`` (take-over). Only the owning boot
(or a taker of a DEAD owner's row) writes a row; ``remove`` deletes only our own
row. "Owner alive" is :func:`core.state.proc.is_alive` on the stored
``owner_ident`` (pid + start time + boot id), so a reused pid (a container PID 1
after a restart) reads dead. The legacy ``status`` column is no longer read or
written (S10); an old DB keeps it with its default.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, Iterator, List, Optional, Tuple

from core.sqlite_util import execute_retry
from core.state.proc import ProcIdentity, boot_id, is_alive, process_identity, start_time

logger = logging.getLogger(__name__)


class SqliteSessionRegistry:
    def __init__(self, db_path: str, *, worker_pid: Optional[int] = None) -> None:
        self.db_path = db_path
        self._pid = worker_pid or os.getpid()
        # Per-process boot id: ownership is keyed on this, NOT the (reusable) pid.
        # Two processes that happen to share a pid (PID reuse after a worker dies)
        # still get distinct boot ids, so routing never mistakes one for the other.
        self._boot_id = uuid.uuid4().hex
        # 098 AG-16: the liveness identity stored with each owned row.
        if self._pid == os.getpid():
            self._ident = process_identity()
        else:  # tests / an explicit pid: pid-only liveness on this boot
            self._ident = ProcIdentity(self._pid, start_time(self._pid) or "unknown", boot_id())
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
                params TEXT NOT NULL DEFAULT '{}',
                last_seen_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                owner_boot_id TEXT,
                owner_ident TEXT
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
        for col in ("owner_boot_id", "owner_ident"):
            if col in names:
                continue
            try:
                execute_retry(
                    self.db_path,
                    f"ALTER TABLE active_sessions ADD COLUMN {col} TEXT",
                )
            except Exception as e:  # concurrent add won the race — that's fine
                if "duplicate column" not in str(e).lower():
                    raise

    # --- local object dict (compatible with SessionRegistry) -----------------

    # --- ownership (098 AG-16) -------------------------------------------------

    def _owner_alive(self, row: Any) -> bool:
        """The process that owns ``row`` still runs. Our own boot: alive. A row
        with an identity: :func:`is_alive` (pid + start time + boot id). A legacy
        row without one: pid liveness, and our own pid under another boot id is
        a previous life of this pid (dead)."""
        if row["owner_boot_id"] == self._boot_id:
            return True
        ident = ProcIdentity.decode(row["owner_ident"]) if "owner_ident" in row.keys() else None
        if ident is not None:
            return is_alive(ident)
        pid = row["worker_pid"]
        if pid == self._pid:
            return False
        return self._pid_alive(pid)

    def _row(self, session_id: str) -> Any:
        return execute_retry(
            self.db_path,
            "SELECT worker_pid, owner_boot_id, owner_ident FROM active_sessions WHERE session_id=?",
            (session_id,), fetch="one",
        )

    def _drop_dead_row(self, session_id: str, observed_boot: Optional[str]) -> None:
        """Delete a dead owner's row — a CAS on the owner we observed, so a row a
        live worker took over in between is never deleted."""
        execute_retry(
            self.db_path,
            "DELETE FROM active_sessions WHERE session_id=? AND owner_boot_id IS ?",
            (session_id, observed_boot),
        )

    def register(self, session_id: str, orchestrator: Any, *, params: Optional[Dict[str, Any]] = None) -> bool:
        """Own ``session_id``: insert the row, keep our own row, or take over the
        row of a DEAD owner. A row of a LIVE other owner is never stolen (098
        AG-16): the object is still held locally, the row stays theirs, and the
        call returns False (``route`` keeps answering REMOTE)."""
        # Write the DB row BEFORE the local dict. route() reads the DB; if the DB
        # write raised after we'd already stored the object locally, route() would
        # return MISSING for a session this worker actually holds — a false 404, the
        # exact failure this class exists to prevent.
        now = datetime.now().isoformat()
        payload = json.dumps(params or {})
        ident = self._ident.encode()
        owned = True
        for _ in range(3):
            row = self._row(session_id)
            if row is None:
                execute_retry(
                    self.db_path,
                    """
                    INSERT INTO active_sessions
                        (session_id, worker_pid, owner_boot_id, owner_ident, params,
                         last_seen_at, created_at)
                    VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(session_id) DO NOTHING
                    """,
                    (session_id, self._pid, self._boot_id, ident, payload, now, now),
                )
            elif row["owner_boot_id"] == self._boot_id or not self._owner_alive(row):
                execute_retry(
                    self.db_path,
                    """
                    UPDATE active_sessions SET worker_pid=?, owner_boot_id=?, owner_ident=?,
                        params=?, last_seen_at=?
                    WHERE session_id=? AND owner_boot_id IS ?
                    """,
                    (self._pid, self._boot_id, ident, payload, now,
                     session_id, row["owner_boot_id"]),
                )
            else:
                owned = False
                logger.warning(
                    "session %s is owned by a live worker (pid %s); not taking the row",
                    session_id, row["worker_pid"])
                break
            if self.owner_boot_id(session_id) == self._boot_id:
                break
        else:
            owned = self.owner_boot_id(session_id) == self._boot_id
        self._orchestrators[session_id] = orchestrator
        return owned

    def get(self, session_id: str) -> Optional[Any]:
        """Return the LOCAL orchestrator object (None if owned by another worker)."""
        return self._orchestrators.get(session_id)

    def remove(self, session_id: str) -> Optional[Any]:
        """Drop the local object; delete the row only when this boot owns it
        (098 AG-16 — never another worker's row)."""
        execute_retry(
            self.db_path,
            "DELETE FROM active_sessions WHERE session_id=? AND owner_boot_id=?",
            (session_id, self._boot_id),
        )
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
            execute_retry(
                self.db_path,
                "DELETE FROM active_sessions WHERE session_id=? AND owner_boot_id=?",
                (sid, self._boot_id),
            )
        self._orchestrators.clear()

    # --- cross-process metadata (SQLite) -------------------------------------

    def exists(self, session_id: str) -> bool:
        row = execute_retry(
            self.db_path, "SELECT 1 FROM active_sessions WHERE session_id=?", (session_id,), fetch="one",
        )
        return row is not None

    def route(self, session_id: str):
        """Routing decision (P6): LOCAL (object is in this worker), REMOTE (exists and
        owned by another LIVE worker — caller can forward / 409 with owner_pid), or
        MISSING. 098 AG-16: a row whose owner is dead (a reused pid included) is
        deleted with an owner CAS and reads MISSING, so the caller recreates the
        session locally instead of answering 409 for ever."""
        from agents.task.session_route import SessionRoute, LOCAL, REMOTE, MISSING
        row = self._row(session_id)
        if row is None:
            return SessionRoute(status=MISSING)
        owner = int(row["worker_pid"])
        owner_boot = row["owner_boot_id"]
        # LOCAL only when the OWNING boot id is ours (pid match alone is not enough:
        # a reused pid in a fresh process must not be treated as the same owner).
        local = self._orchestrators.get(session_id)
        if owner_boot == self._boot_id and local is not None:
            return SessionRoute(status=LOCAL, orchestrator=local, owner_pid=owner)
        if owner_boot != self._boot_id and not self._owner_alive(row):
            self._drop_dead_row(session_id, owner_boot)
            return SessionRoute(status=MISSING)
        if owner_boot == self._boot_id:
            # Our row but no local object (a lost local entry): nobody else can
            # serve it — recreate here.
            return SessionRoute(status=MISSING)
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
            "SELECT session_id, worker_pid, owner_boot_id, owner_ident FROM active_sessions "
            "WHERE last_seen_at < ?",
            (cutoff,), fetch="all",
        )
        # Reap only rows that are (a) not ours and (b) owned by a DEAD process. A
        # stale row whose owner worker is still alive is an idle session on another
        # worker — deleting it would false-404 and orphan that worker's orchestrator.
        reaped = [
            r["session_id"] for r in rows
            if r["session_id"] not in local_sids
            # our own row with no local object is an orphan; another owner's row
            # goes only when that owner is dead (process_identity, 098 AG-16)
            and (r["owner_boot_id"] == self._boot_id or not self._owner_alive(r))
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
