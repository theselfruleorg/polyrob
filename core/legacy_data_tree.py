"""Converge a 1.4.0 server data home onto the one-data-dir layout.

Up to 1.4.0 a server (``POLYROB_DATA_DIR`` set, ``DATA_DIR``/``DB_PATH`` unset)
kept ``config.data_dir`` at ``<home>/data``: bot.db at
``<home>/data/database/bot.db`` plus every store that hung under
``config.data_dir`` (characters/, logs/, cache/, knowledge/, simulations/,
temp/, artifacts.db, deployed_apps.db, autonomy_state.db, the surface bus ...).
Since the one-data-dir change ``config.data_dir`` IS ``<home>``, so an upgraded
install would open an EMPTY ``<home>/database/bot.db``.

:func:`converge_legacy_data_tree` runs once at config construction, before any
directory is created. It moves each entry of ``<home>/data`` up into
``<home>`` with ``os.rename`` (same filesystem, atomic per entry; a sqlite file
moves with its ``-wal``/``-shm``/``-journal`` siblings), under an exclusive
``fcntl`` lock so concurrently starting units do not race — the second caller
finds the work done.

Rules — nothing is ever merged, overwritten or deleted:

- An entry whose name already exists in ``<home>`` stays where it is (an empty
  directory at the destination does not count — it holds nothing). Readers
  already resolved those names at ``<home>`` before the change, so the copy
  left behind is the shadow; ``polyrob doctor`` names it.
- bot.db is the exception: if ``<home>/database`` holds a file of the bot.db
  group, the move refuses with a RuntimeError that names both paths and the
  remedy (the same rule as ``resolve_bot_db_path``).
- The ``database`` entry moves LAST, so a failure part-way leaves the trigger
  (``<home>/data/database/bot.db`` present, ``<home>/database/bot.db`` absent)
  in place and the next start finishes the job.
"""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

logger = logging.getLogger("core.legacy_data_tree")

LEGACY_SUBDIR = "data"
_DB_DIR = "database"
_BOT_DB = "bot.db"
_SQLITE_SUFFIXES = ("-wal", "-shm", "-journal")
_LOCK_NAME = ".legacy_data_tree.lock"


def _group(name: str) -> Tuple[str, ...]:
    """A sqlite file and its siblings move (or stay) as one unit."""
    return (name,) + tuple(name + s for s in _SQLITE_SUFFIXES)


def _base_name(name: str) -> str:
    for s in _SQLITE_SUFFIXES:
        if name.endswith(s):
            return name[: -len(s)]
    return name


def _occupied(path: Path) -> bool:
    """True when *path* holds something (an empty directory holds nothing)."""
    if path.is_symlink() or path.is_file():
        return True
    if path.is_dir():
        try:
            return any(path.iterdir())
        except OSError:
            return True
    return path.exists()


def _units(src_dir: Path) -> List[Tuple[str, ...]]:
    """The entries of *src_dir* grouped into move units (sqlite siblings together)."""
    groups: dict = {}
    for name in sorted(p.name for p in src_dir.iterdir()):
        groups.setdefault(_base_name(name), []).append(name)
    return [tuple(v) for _, v in sorted(groups.items())]


def applies(home: Optional[Path], data_dir: str = "", db_path: str = "") -> bool:
    """True when the 1.4.0 legacy tree must converge into *home*."""
    if home is None or data_dir or db_path:
        return False
    legacy_db = home / LEGACY_SUBDIR / _DB_DIR / _BOT_DB
    return legacy_db.is_file() and not (home / _DB_DIR / _BOT_DB).exists()


@contextmanager
def _exclusive(home: Path) -> Iterator[None]:
    try:
        import fcntl
    except ImportError:  # pragma: no cover - Windows: no concurrent units there
        yield
        return
    fd = os.open(str(home / _LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o660)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _move(src: Path, dst: Path) -> None:
    if dst.is_dir() and not dst.is_symlink() and not _occupied(dst):
        dst.rmdir()  # empty: holds nothing, so nothing is lost
    os.rename(src, dst)


def _bot_db_refusal(legacy_db_dir: Path, new_db_dir: Path) -> RuntimeError:
    return RuntimeError(
        f"Upgrade cannot move the database: {legacy_db_dir / _BOT_DB} holds the "
        f"existing bot.db, but {new_db_dir} already holds a bot.db file. "
        "Refusing to guess: stop the services, move the bot.db files out of "
        f"{new_db_dir} (keep them), move {legacy_db_dir}/bot.db* there, then restart.")


def _move_database(legacy: Path, home: Path, moved: List[str]) -> None:
    src, dst = legacy / _DB_DIR, home / _DB_DIR
    if not _occupied(dst):
        _move(src, dst)
        moved.append(_DB_DIR + "/")
        return
    # <home>/database exists with other files: move the bot.db group into it.
    if any((dst / n).exists() for n in _group(_BOT_DB)):
        raise _bot_db_refusal(src, dst)
    for unit in _units(src):
        if any(_occupied(dst / n) for n in unit):
            continue
        for n in unit:
            _move(src / n, dst / n)
            moved.append(f"{_DB_DIR}/{n}")


def converge_legacy_data_tree(home: Optional[Path], *, data_dir: str = "",
                              db_path: str = "") -> List[str]:
    """Move the 1.4.0 ``<home>/data`` tree up into *home*. Returns the moved
    entry names (empty when nothing applied). Raises RuntimeError only for a
    bot.db collision; any other OSError is logged and the start continues on
    whatever has moved (the next start retries)."""
    if not applies(home, data_dir, db_path):
        return []
    assert home is not None
    legacy = home / LEGACY_SUBDIR
    if not (os.access(home, os.W_OK) and os.access(legacy, os.W_OK)):
        logger.warning("legacy data tree %s needs moving into %s, but this process "
                       "cannot write there; the agent service will move it", legacy, home)
        return []
    moved: List[str] = []
    left: List[str] = []
    try:
        with _exclusive(home):
            if not applies(home, data_dir, db_path):
                return []  # another unit did it while we waited
            for unit in _units(legacy):
                if unit[0] == _DB_DIR:
                    continue
                if any(_occupied(home / n) for n in unit):
                    left.extend(unit)
                    continue
                for n in unit:
                    _move(legacy / n, home / n)
                    moved.append(n)
            _move_database(legacy, home, moved)
    except RuntimeError:
        raise
    except OSError as exc:
        logger.error("legacy data tree move stopped at %s (moved so far: %s); "
                     "the next start retries", exc, ", ".join(moved) or "nothing")
        return moved
    logger.warning("upgrade: moved the 1.4.0 data tree %s up into %s: %s%s",
                   legacy, home, ", ".join(moved) or "nothing",
                   f"; left in place (name already in {home}): {', '.join(left)}"
                   if left else "")
    return moved


def legacy_tree_doctor_line(home: Optional[Path], *, data_dir: str = "",
                            db_path: str = "") -> Optional[str]:
    """The ``polyrob doctor`` line for the 1.4.0 legacy tree, or None when there
    is none. *data_dir*/*db_path* are the ``DATA_DIR``/``DB_PATH`` env values.
    Read-only; never raises."""
    try:
        if home is None:
            return None
        legacy = home / LEGACY_SUBDIR
        if not legacy.is_dir():
            return None
        entries = sorted(p.name for p in legacy.iterdir())
        if not entries:
            return None
        shown = ", ".join(entries[:8]) + (" ..." if len(entries) > 8 else "")
        if data_dir or db_path:
            return (f"! legacy data tree: {legacy} holds {len(entries)} entries and "
                    "DATA_DIR/DB_PATH is set, so nothing moves automatically; "
                    f"stores outside DB_PATH are read from {home} now: {shown}")
        legacy_db = legacy / _DB_DIR / _BOT_DB
        new_db = home / _DB_DIR / _BOT_DB
        if legacy_db.is_file() and new_db.exists():
            return (f"! legacy data tree: {legacy_db} AND {new_db} both exist — the "
                    f"agent reads {new_db}; compare them and keep one by hand")
        if legacy_db.is_file():
            return (f"! legacy data tree: {legacy_db} not moved yet — the next "
                    "service start moves it up")
        return (f"legacy data tree: moved; left in {legacy} (name already in "
                f"{home}, compare by hand): {shown}")
    except Exception:
        return None
