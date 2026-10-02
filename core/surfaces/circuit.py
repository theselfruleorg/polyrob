"""Per-surface circuit breaker for the outbound dispatcher.

Opens automatically after K consecutive failures (stops hammering a dead platform);
also supports manual pause/resume so an operator can hold a surface while it's
in maintenance. An optional CircuitStore persists the paused flag across processes
so the CLI's `polyrob surface pause` is visible to the worker.

OB1 (2026-10-03 audit): an auto-open is HALF-OPEN after ``cooldown`` seconds —
``is_open`` lets the next send through as a probe; a failed probe re-opens for
another cooldown, a successful one closes. Before this only a successful send
closed the breaker and an open breaker never sent, so five failures in a
one-minute outage stalled that surface's queue until the process restarted.

OB12: the auto-open moment is persisted too (``auto_open_at``), so a reader in
another process (the CLI / a status seat) sees the breaker the worker holds,
and ``surface resume`` (which clears it) also closes a worker's auto-open.

No `from __future__ import annotations` — callers may introspect param annotations.
"""
import logging
import time
from typing import Callable, Optional, Tuple

logger = logging.getLogger(__name__)


class CircuitStore:
    """Tiny SQLite-backed paused-flag store.

    One table: surface_state(surface_id TEXT PK, paused INTEGER, auto_open_at REAL).
    Reads/writes go through core.sqlite_util for WAL + jittered retry.
    """

    _CREATE = (
        "CREATE TABLE IF NOT EXISTS surface_state "
        "(surface_id TEXT PRIMARY KEY, paused INTEGER NOT NULL DEFAULT 0, "
        "auto_open_at REAL)"
    )

    def __init__(self, db_path: str) -> None:
        self._db = db_path
        from core.sqlite_util import wal_connect
        conn = wal_connect(db_path)
        try:
            conn.execute(self._CREATE)
            # OB12 additive migration: a pre-existing table gains the column.
            cols = {r[1] for r in conn.execute("PRAGMA table_info(surface_state)")}
            if "auto_open_at" not in cols:
                conn.execute("ALTER TABLE surface_state ADD COLUMN auto_open_at REAL")
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
            "INSERT INTO surface_state(surface_id, paused, auto_open_at) VALUES(?,0,NULL) "
            "ON CONFLICT(surface_id) DO UPDATE SET paused=0, auto_open_at=NULL",
            (surface_id,),
        )

    def set_auto_open(self, surface_id: str, ts: float) -> None:
        """Record that the breaker auto-opened at *ts* (OB12)."""
        from core.sqlite_util import execute_retry
        execute_retry(
            self._db,
            "INSERT INTO surface_state(surface_id, paused, auto_open_at) VALUES(?,0,?) "
            "ON CONFLICT(surface_id) DO UPDATE SET auto_open_at=excluded.auto_open_at",
            (surface_id, float(ts)),
        )

    def clear_auto_open(self, surface_id: str) -> None:
        from core.sqlite_util import execute_retry
        execute_retry(
            self._db,
            "UPDATE surface_state SET auto_open_at=NULL WHERE surface_id=?",
            (surface_id,),
        )

    def auto_open_at(self, surface_id: str) -> Optional[float]:
        from core.sqlite_util import execute_retry
        row = execute_retry(
            self._db,
            "SELECT auto_open_at FROM surface_state WHERE surface_id=?",
            (surface_id,),
            fetch="one",
        )
        if not row or row["auto_open_at"] is None:
            return None
        return float(row["auto_open_at"])

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
    - ``_opened_at[surface_id]``: when THIS process auto-opened the breaker.
    - ``_paused``: set of manually paused surface IDs.

    Optional ``store`` (a ``CircuitStore``): pause/resume also writes the flag to
    SQLite, and ``is_open`` reads it so a worker process sees CLI changes. The
    auto-open moment is persisted as well (OB12).

    An auto-open lasts ``cooldown`` seconds; after that the breaker is
    half-open and lets a probe through (OB1).
    """

    def __init__(self, threshold: int = 5, store: Optional[CircuitStore] = None,
                 *, cooldown: float = 60.0,
                 clock: Optional[Callable[[], float]] = None) -> None:
        self._threshold = threshold
        self._counts: dict = {}   # surface_id -> consecutive fail count
        self._opened_at: dict = {}   # surface_id -> ts this process auto-opened
        self._persisted: set = set()   # surface ids whose auto-open reached the store
        self._paused: set = set()
        self._store = store
        self._cooldown = float(cooldown)
        self._clock = clock or time.time

    # ------------------------------------------------------------------
    # Auto-tripping path
    # ------------------------------------------------------------------

    def record_ok(self, surface_id: str) -> None:
        """Reset the consecutive-fail counter; closes the auto-open state."""
        self._counts[surface_id] = 0
        was_open = self._opened_at.pop(surface_id, None) is not None
        self._persisted.discard(surface_id)
        if self._store is None:
            return
        # Clear the persisted marker when this process opened it, or when
        # another process did: a delivered send proves the surface is up.
        if not was_open:
            _, stored = self._store_auto_open_at(surface_id)
            was_open = stored is not None
        if was_open:
            try:
                self._store.clear_auto_open(surface_id)
            except Exception as e:
                logger.warning("circuit_store: clear failed for %s: %s", surface_id, e)

    def record_fail(self, surface_id: str) -> None:
        """Increment the consecutive-fail counter; opens when >= threshold.

        A failure while open or half-open (the probe) re-opens for another
        cooldown, measured from now."""
        count = self._counts.get(surface_id, 0) + 1
        self._counts[surface_id] = count
        if count >= self._threshold:
            now = self._clock()
            self._opened_at[surface_id] = now
            logger.warning(
                "circuit_breaker: surface=%s opened after %d consecutive failures "
                "(half-open probe in %.0fs)", surface_id, count, self._cooldown,
            )
            if self._store is not None:
                try:
                    self._store.set_auto_open(surface_id, now)
                    self._persisted.add(surface_id)
                except Exception as e:
                    self._persisted.discard(surface_id)
                    logger.warning("circuit_store: auto-open write failed for %s: %s",
                                   surface_id, e)

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
        self._opened_at.pop(surface_id, None)
        self._persisted.discard(surface_id)
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

    def _store_auto_open_at(self, surface_id: str) -> Tuple[bool, Optional[float]]:
        """``(readable, ts)`` of the persisted auto-open. Fail-open: an unreadable
        store is ``(False, None)`` so the caller can tell it from a cleared row."""
        if self._store is None:
            return False, None
        reader = getattr(self._store, "auto_open_at", None)
        if not callable(reader):
            return False, None
        try:
            return True, reader(surface_id)
        except Exception as e:
            logger.warning("circuit_store: auto-open read failed for %s: %s", surface_id, e)
            return False, None

    def _auto_open(self, surface_id: str) -> bool:
        """The auto-open half of :meth:`is_open` (OB1 + OB12)."""
        readable, stored = self._store_auto_open_at(surface_id)
        local = self._opened_at.get(surface_id)
        if (local is not None and surface_id in self._persisted
                and readable and stored is None):
            # Another process (`polyrob surface resume`) cleared the persisted
            # auto-open: the operator closed it, so this process closes too.
            self._counts[surface_id] = 0
            self._opened_at.pop(surface_id, None)
            self._persisted.discard(surface_id)
            return False
        opened = max([t for t in (local, stored) if t is not None], default=None)
        if opened is None:
            return False
        # Past the cooldown the breaker is half-open: let the next send probe.
        return (self._clock() - opened) < self._cooldown

    def is_open(self, surface_id: str) -> bool:
        """Return True if the surface should be skipped (open = bad = skip)."""
        if surface_id in self._paused:
            return True
        if self._store_paused(surface_id):
            return True
        return self._auto_open(surface_id)

    def state(self, surface_id: str) -> dict:
        """Return a snapshot of the surface's circuit state."""
        manually_paused = surface_id in self._paused
        store_paused = self._store_paused(surface_id)
        auto_open = self._auto_open(surface_id)
        return {
            "surface_id": surface_id,
            "consecutive_failures": self._counts.get(surface_id, 0),
            "threshold": self._threshold,
            "auto_open": auto_open,
            "manually_paused": manually_paused,
            "store_paused": store_paused,
            "is_open": manually_paused or auto_open or store_paused,
        }
