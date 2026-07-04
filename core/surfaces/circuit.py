"""Per-surface circuit breaker for the outbound dispatcher.

Opens automatically after K consecutive failures (stops hammering a dead platform);
also supports manual pause/resume so an operator can hold a surface while it's
in maintenance. An optional CircuitStore persists the paused flag across processes
so the CLI's `polyrob surface pause` is visible to the worker.

No `from __future__ import annotations` — callers may introspect param annotations.
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class CircuitStore:
    """Tiny SQLite-backed paused-flag store.

    One table: surface_state(surface_id TEXT PK, paused INTEGER).
    Reads/writes go through core.sqlite_util for WAL + jittered retry.
    """

    _CREATE = (
        "CREATE TABLE IF NOT EXISTS surface_state "
        "(surface_id TEXT PRIMARY KEY, paused INTEGER NOT NULL DEFAULT 0)"
    )

    def __init__(self, db_path: str) -> None:
        self._db = db_path
        from core.sqlite_util import wal_connect
        conn = wal_connect(db_path)
        try:
            conn.execute(self._CREATE)
            conn.commit()
        finally:
            conn.close()

    def pause(self, surface_id: str) -> None:
        from core.sqlite_util import execute_retry
        execute_retry(
            self._db,
            "INSERT INTO surface_state(surface_id, paused) VALUES(?,1) "
            "ON CONFLICT(surface_id) DO UPDATE SET paused=1",
            (surface_id,),
        )

    def resume(self, surface_id: str) -> None:
        from core.sqlite_util import execute_retry
        execute_retry(
            self._db,
            "INSERT INTO surface_state(surface_id, paused) VALUES(?,0) "
            "ON CONFLICT(surface_id) DO UPDATE SET paused=0",
            (surface_id,),
        )

    def is_paused(self, surface_id: str) -> bool:
        from core.sqlite_util import execute_retry
        row = execute_retry(
            self._db,
            "SELECT paused FROM surface_state WHERE surface_id=?",
            (surface_id,),
            fetch="one",
        )
        return bool(row and row["paused"])

    def list_all(self) -> list:
        from core.sqlite_util import execute_retry
        rows = execute_retry(
            self._db,
            "SELECT surface_id, paused FROM surface_state ORDER BY surface_id",
            fetch="all",
        )
        return [{"surface_id": r["surface_id"], "paused": bool(r["paused"])} for r in (rows or [])]


class SurfaceCircuitBreaker:
    """Auto-pauses a surface after `threshold` consecutive failures.

    In-memory state:
    - ``_counts[surface_id]``: consecutive failure counter (resets on ok).
    - ``_paused``: set of manually paused surface IDs.

    Optional ``store`` (a ``CircuitStore``): pause/resume also writes the flag to
    SQLite, and ``is_open`` reads it so a worker process sees CLI changes.
    """

    def __init__(self, threshold: int = 5, store: Optional[CircuitStore] = None) -> None:
        self._threshold = threshold
        self._counts: dict = {}   # surface_id -> consecutive fail count
        self._paused: set = set()
        self._store = store

    # ------------------------------------------------------------------
    # Auto-tripping path
    # ------------------------------------------------------------------

    def record_ok(self, surface_id: str) -> None:
        """Reset the consecutive-fail counter; closes the auto-open state."""
        self._counts[surface_id] = 0

    def record_fail(self, surface_id: str) -> None:
        """Increment the consecutive-fail counter; opens when >= threshold."""
        count = self._counts.get(surface_id, 0) + 1
        self._counts[surface_id] = count
        if count >= self._threshold:
            logger.warning(
                "circuit_breaker: surface=%s opened after %d consecutive failures",
                surface_id, count,
            )

    # ------------------------------------------------------------------
    # Manual pause/resume path
    # ------------------------------------------------------------------

    def pause(self, surface_id: str) -> None:
        """Manually pause a surface (operator-driven; persisted if store attached)."""
        self._paused.add(surface_id)
        if self._store is not None:
            self._store.pause(surface_id)

    def resume(self, surface_id: str) -> None:
        """Resume a paused surface and reset its fail counter."""
        self._paused.discard(surface_id)
        self._counts[surface_id] = 0
        if self._store is not None:
            self._store.resume(surface_id)

    # ------------------------------------------------------------------
    # State query
    # ------------------------------------------------------------------

    def _store_paused(self, surface_id: str) -> bool:
        """Read the persisted pause flag, fail-open. A store/DB fault (missing or corrupt
        surface_state.db) must NEVER crash a send — treat it as not-paused."""
        if self._store is None:
            return False
        try:
            return bool(self._store.is_paused(surface_id))
        except Exception as e:
            logger.warning("circuit_store: read failed for %s (treating as not paused): %s",
                           surface_id, e)
            return False

    def is_open(self, surface_id: str) -> bool:
        """Return True if the surface should be skipped (open = bad = skip)."""
        if surface_id in self._paused:
            return True
        if (self._counts.get(surface_id, 0)) >= self._threshold:
            return True
        if self._store_paused(surface_id):
            return True
        return False

    def state(self, surface_id: str) -> dict:
        """Return a snapshot of the surface's circuit state."""
        count = self._counts.get(surface_id, 0)
        manually_paused = surface_id in self._paused
        store_paused = self._store_paused(surface_id)
        auto_open = count >= self._threshold
        return {
            "surface_id": surface_id,
            "consecutive_failures": count,
            "threshold": self._threshold,
            "auto_open": auto_open,
            "manually_paused": manually_paused,
            "store_paused": store_paused,
            "is_open": manually_paused or auto_open or store_paused,
        }
