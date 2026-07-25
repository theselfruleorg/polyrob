"""Dead-target registry: store + liveness classifier (T1.5, Task 1 — pure/unwired).

Every surface's send path collapses exception TYPES to a plain string
(``SendResult(success=False, error=str(e))`` — telegram ``surface.py:112-114`` +
six siblings), so a send to a definitively-dead target (bot blocked, chat
deleted, user deactivated) is retried forever alongside genuinely transient
failures. This module ships the two pure pieces the later wiring tasks build
on:

- ``DeadTargetStore`` — a tiny SQLite-backed registry keyed ``(surface,
  address)``, shaped after ``CircuitStore`` (``core/surfaces/circuit.py``):
  same ``wal_connect``/``execute_retry`` construction, same fail-open contract
  on read.
- ``classify_dead_error`` — a pure string-pattern classifier over the
  collapsed error text. Unknown/transient text (timeouts, rate limits,
  network resets) always classifies as ``None`` — mark only on a hard,
  known-definitive liveness signal, never on a guess.

Not wired into any send path yet (see the plan's Task 2/3). No `from __future__
import annotations` — mirrors circuit.py's convention for modules whose callers
may introspect param annotations.
"""
import logging
import time
from typing import List, Optional

from core.config_policy import dead_target_registry_enabled  # noqa: F401 (convenience re-export)

logger = logging.getLogger(__name__)


def _norm_addr(address: str) -> str:
    """Normalize an external address for keying (mirrors
    ``core/surfaces/correspondents.py::_norm_addr`` — case/space-insensitive;
    lowercasing is correct for email and harmless for phone-number/chat ids).
    """
    return (address or "").strip().lower()


class DeadTargetStore:
    """SQLite-backed dead-target registry.

    One table: dead_targets(surface, address) PK, reason, marked_at, fail_count.
    Reads/writes go through core.sqlite_util for WAL + jittered retry.
    """

    _CREATE = (
        "CREATE TABLE IF NOT EXISTS dead_targets ("
        "surface TEXT NOT NULL, "
        "address TEXT NOT NULL, "
        "reason TEXT NOT NULL, "
        "marked_at REAL NOT NULL, "
        "fail_count INTEGER NOT NULL DEFAULT 1, "
        "PRIMARY KEY (surface, address))"
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

    def mark(self, surface: str, address: str, reason: str) -> None:
        """Upsert a dead-target row: stamps ``marked_at`` to now and bumps
        ``fail_count`` (starts at 1, increments on every re-mark)."""
        from core.sqlite_util import execute_retry
        execute_retry(
            self._db,
            "INSERT INTO dead_targets(surface, address, reason, marked_at, fail_count) "
            "VALUES(?,?,?,?,1) "
            "ON CONFLICT(surface, address) DO UPDATE SET "
            "reason=excluded.reason, marked_at=excluded.marked_at, "
            "fail_count=fail_count + 1",
            (surface, _norm_addr(address), reason, time.time()),
        )

    def is_dead(self, surface: str, address: str) -> bool:
        """Return True iff ``(surface, address)`` is marked dead.

        Fail-open: ANY store error (corrupt db, missing file, locked) degrades
        to False — a liveness check must never be able to block a send.
        """
        try:
            from core.sqlite_util import execute_retry
            row = execute_retry(
                self._db,
                "SELECT 1 FROM dead_targets WHERE surface=? AND address=?",
                (surface, _norm_addr(address)),
                fetch="one",
            )
            return bool(row)
        except Exception as e:
            logger.warning(
                "dead_targets: read failed for %s/%s (treating as alive): %s",
                surface, address, e,
            )
            return False

    def clear(self, surface: str, address: str) -> None:
        """Idempotent delete (revive-on-inbound). A no-op if never marked."""
        from core.sqlite_util import execute_retry
        execute_retry(
            self._db,
            "DELETE FROM dead_targets WHERE surface=? AND address=?",
            (surface, _norm_addr(address)),
        )

    def list_all(self) -> List[dict]:
        from core.sqlite_util import execute_retry
        rows = execute_retry(
            self._db,
            "SELECT surface, address, reason, marked_at, fail_count FROM dead_targets "
            "ORDER BY surface, address",
            fetch="all",
        )
        return [
            {
                "surface": r["surface"],
                "address": r["address"],
                "reason": r["reason"],
                "marked_at": r["marked_at"],
                "fail_count": r["fail_count"],
            }
            for r in (rows or [])
        ]


# ---------------------------------------------------------------------------
# Liveness classifier
# ---------------------------------------------------------------------------
#
# Telegram's API error messages are stable and are the only hard-liveness
# signal shipped in v1 (email/SMTP bounce classification is explicitly out of
# scope — SMTP failure shapes vary too widely; see the plan's Open questions).
# Lowercase substring match, case-insensitive on the input.
_DEAD_PATTERNS = (
    ("bot was blocked", "blocked"),
    ("user is deactivated", "deactivated"),
    ("chat not found", "chat_not_found"),
    ("peer_id_invalid", "chat_not_found"),
    ("bot was kicked", "kicked"),
)


def classify_dead_error(surface_id: str, error_text: Optional[str]) -> Optional[str]:
    """Classify a collapsed send-failure string as a definitive liveness signal.

    Returns the matched reason slug (e.g. ``"blocked"``), or ``None`` for
    empty/``None`` input and for any unmatched text — including known-transient
    failures (timeouts, rate limits, connection resets) and anything else not
    in the pattern table. Fail-open by construction: only a recognized,
    definitive pattern ever returns non-``None``.

    ``surface_id`` is accepted for a future per-surface pattern table; v1 uses
    one shared table regardless of surface, so it is currently unused.
    """
    if not error_text:
        return None
    lowered = error_text.lower()
    for pattern, reason in _DEAD_PATTERNS:
        if pattern in lowered:
            return reason
    return None
