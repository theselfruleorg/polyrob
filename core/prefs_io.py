"""The ``preferences.toml`` file itself: unreadable is not empty (ST-1).

A hand edit that breaks the TOML used to read as "no preferences": the next write
replaced the whole file with ONE key (the owner's caps and loop switches gone), and
every read in between fell back to the operator's looser values. Here:

- a read that fails keeps the LAST GOOD values in force (the process cache, else the
  copy the last successful write left beside the file) and is reported, never empty;
- a write reads strictly and refuses on an unreadable file, under a cross-process
  lock, through a unique temp file — the broken file is never overwritten.

Imports nothing from :mod:`core.prefs` (it calls in, not out).
"""
from __future__ import annotations

import logging
import os
import tempfile
import tomllib
from pathlib import Path
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

#: path -> the read error, while that file is unreadable.
_UNREADABLE: Dict[str, str] = {}
#: path -> (mtime_ns, error) last warned about: one WARNING per broken file state.
_WARNED: Dict[str, tuple] = {}


class PrefsUnreadable(Exception):
    """``preferences.toml`` exists but cannot be read or parsed."""

    def __init__(self, path, error: BaseException):
        super().__init__(f"preferences.toml is unreadable ({type(error).__name__}: {error}). "
                         f"Nothing was written: fix or remove {path} first")
        self.path, self.error = str(path), error


def backup_path(path: Path) -> Path:
    """The last good copy a successful write leaves beside the file."""
    return path.with_name("." + path.name + ".good")


def mark_readable(key: str) -> None:
    _UNREADABLE.pop(key, None)
    _WARNED.pop(key, None)


def remember_good(key: str, path: Path, text: str) -> None:
    """The file parsed: mark it readable and refresh the last good copy when it
    differs. A valid HAND edit counts too — else a later typo restored an older,
    looser copy that only a ``/config`` write had refreshed."""
    mark_readable(key)
    good = backup_path(path)
    try:
        if good.is_file() and good.read_text(encoding="utf-8") == text:
            return
        _atomic_text(good, text)
    except OSError as e:
        logger.warning("preferences last-good copy not refreshed (%s): %s", path, e)


def unreadable(path: Optional[Path]) -> Optional[str]:
    """The read error of an unreadable ``preferences.toml``, else None."""
    return _UNREADABLE.get(str(path)) if path is not None else None


def last_good_raw(key: str, path: Path, error: BaseException) -> Optional[dict]:
    """Record the read failure; the parsed last good copy on disk, or None."""
    _UNREADABLE[key] = f"{type(error).__name__}: {error}"[:300]
    try:
        state = (path.stat().st_mtime_ns, _UNREADABLE[key])
    except OSError:
        state = (None, _UNREADABLE[key])
    if _WARNED.get(key) != state:  # once per broken file state, not per read
        _WARNED[key] = state
        logger.warning("preferences.toml unreadable (%s) — the last good values stay in "
                       "force: %s", path, error)
    try:
        return tomllib.loads(backup_path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def _atomic_text(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def locked_write(path: Path, load_strict: Callable[[], dict], key: str, value: object,
                 render: Callable[[dict], str], cache: dict) -> tuple[bool, str]:
    """Read strictly, set *key*, write — exclusive across processes."""
    from core.state.files import file_lock
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(str(path)):
            flat = load_strict()
            flat[key] = value
            text = render(flat)
            _atomic_text(path, text)
            cache.pop(str(path), None)
            try:
                _atomic_text(backup_path(path), text)
            except OSError as e:
                logger.warning("preferences last-good copy not written (%s): %s", path, e)
        return True, ""
    except PrefsUnreadable as e:
        logger.error("preferences write refused: %s", e)
        return False, str(e)
    except Exception as e:
        logger.error("preferences write failed (%s): %s", path, e, exc_info=True)
        return False, f"write failed: {e}"
