"""One-shot relocation of legacy session-tree sidecar DBs to the data home (R-2 T3).

``telemetry_events.db`` and the ``messages.db`` mirror historically lived under
``pm().data_root`` (the SESSION artifact tree); ``core.runtime_paths.sidecar_db_path``
moved their canonical home to ``<data_home>/<name>`` with a read-both fallback. This
sweep retires the fallback for a live install by physically moving each legacy file
the first time a process resolves the event log (the trigger sits in
``agents/task/telemetry/event_log.py::get_event_log``'s DEFAULT-resolution branch,
BEFORE the singleton binds — so the mover process starts on the new path and never
forks history).

Safety properties (all fail-open — a relocation error must never block boot):
- **No clobber:** the move is ``os.link(legacy, new)`` + ``os.unlink(legacy)`` —
  ``os.link`` raises ``FileExistsError`` atomically if the new path appeared in the
  meantime (a racing process), unlike ``shutil.move``'s silent POSIX rename-over.
- **No artifact moves:** a zero-byte legacy file (another process's
  ``sqlite3.connect`` artifact recreated mid-race) is never moved.
- **WAL hygiene:** the legacy DB is checkpointed (``wal_checkpoint(TRUNCATE)``)
  before the move so ``-wal``/``-shm`` sidecars are folded in; leftovers are
  unlinked defensively.
- Cross-filesystem installs (session tree and data home on different mounts) make
  ``os.link`` raise ``OSError`` — the sweep skips (fallback keeps working) rather
  than attempting a non-atomic copy.

A brief write-window caveat is accepted by design: another ALREADY-RUNNING process
whose event log bound to the legacy path before this sweep ran will recreate a
(zero-byte-then-small) legacy file on its next per-write connect; those few rows are
orphaned until its restart. Telemetry-grade data, disclosed in the R-2 plan.
"""
import logging
import os
import sqlite3
from pathlib import Path
from typing import List

logger = logging.getLogger(__name__)

SIDECARS_TO_RELOCATE = ("telemetry_events.db", "messages.db")

_DONE = False


def _checkpoint(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


def relocate_legacy_sidecars() -> List[str]:
    """Move each legacy session-tree sidecar DB to the data home. Returns the
    names actually moved. Idempotent per process; every failure is per-file and
    fail-open."""
    global _DONE
    if _DONE:
        return []
    _DONE = True

    from core.runtime_paths import resolve_data_home, resolve_session_data_root

    moved: List[str] = []
    try:
        home = resolve_data_home()
        session_root = Path(resolve_session_data_root())
    except Exception:
        return moved
    if home.resolve() == session_root.resolve():
        return moved  # nothing to do on a layout where the axes coincide

    for name in SIDECARS_TO_RELOCATE:
        legacy = session_root / name
        new = home / name
        try:
            if new.exists() or not legacy.is_file():
                continue
            if legacy.stat().st_size == 0:
                continue  # racing connect artifact, not a real DB
            _checkpoint(legacy)
            new.parent.mkdir(parents=True, exist_ok=True)
            os.link(legacy, new)  # atomic no-clobber (FileExistsError if raced)
            os.unlink(legacy)
            for suffix in ("-wal", "-shm"):
                try:
                    os.unlink(str(legacy) + suffix)
                except OSError:
                    pass
            moved.append(name)
            logger.info("Relocated sidecar DB %s -> %s (R-2 T3 one-shot move)", legacy, new)
        except FileExistsError:
            logger.info("Sidecar DB %s appeared at %s mid-move — keeping both, new wins", name, new)
        except Exception as e:
            logger.warning("Could not relocate sidecar DB %s: %s (fallback keeps working)", name, e)
    return moved
