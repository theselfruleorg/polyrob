"""The ``preferences.toml`` file itself: unreadable is not empty (ST-1).

A hand edit that breaks the TOML used to read as "no preferences": the next write
replaced the whole file with ONE key (the owner's caps and loop switches gone), and
every read in between fell back to the operator's looser values. Here:

- a read that fails keeps the LAST GOOD values in force (the process cache, else the
  copy the last successful write left beside the file) and is reported, never empty;
- a write reads strictly and refuses on an unreadable file, under a cross-process
  lock, through a unique temp file — the broken file is never overwritten;
- a write merges into the RAW file: a key this version cannot validate is kept.

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

#: The value :func:`locked_write` reads as "remove the key" (a preference unset).
REMOVE = object()


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


def flatten(nested: dict, prefix: str = "") -> dict[str, object]:
    """``{"a": {"b": 1}}`` -> ``{"a.b": 1}``; a list stays one value."""
    flat: dict[str, object] = {}
    for k, v in nested.items():
        dotted = f"{prefix}{k}"
        if isinstance(v, dict):
            flat.update(flatten(v, f"{dotted}."))
        else:
            flat[dotted] = v
    return flat


def _to_toml_value(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_to_toml_value(i) for i in v) + "]"
    # Escape for TOML basic string: backslash first (already done), then quotes,
    # then control chars per TOML spec (newline, tab, CR, and other < U+0020).
    s = str(v)
    s = s.replace("\\", "\\\\")  # backslash first
    s = s.replace('"', '\\"')    # double-quote
    s = s.replace("\n", "\\n")   # newline
    s = s.replace("\r", "\\r")   # carriage return
    s = s.replace("\t", "\\t")   # tab
    # Escape any other control character (< U+0020 or U+007F DEL) as \uXXXX
    result = []
    for c in s:
        if (ord(c) < 0x20 and c not in "\n\r\t") or ord(c) == 0x7F:
            result.append(f"\\u{ord(c):04x}")
        else:
            result.append(c)
    return '"' + "".join(result) + '"'


def render_toml(flat: dict[str, object]) -> str:
    """A flat dict as ``preferences.toml`` text (one table per first name segment)."""
    groups: dict[str, dict[str, object]] = {}
    for dotted, v in sorted(flat.items()):
        group, _, leaf = dotted.partition(".")
        groups.setdefault(group, {})[leaf] = v
    out: list[str] = ["# POLYROB per-user preferences — managed via /config, "
                      "`polyrob config`, chat, or hand-edit.", ""]
    for group, leaves in groups.items():
        out.append(f"[{group}]")
        out.extend(f"{leaf} = {_to_toml_value(v)}" for leaf, v in leaves.items())
        out.append("")
    return "\n".join(out)


def _atomic_text(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        from core.fs_copy import shared_file_mode
        os.chmod(tmp, shared_file_mode(path.parent))   # mkstemp births 0600
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_raw(path: Path, parse: Callable[[str], dict]) -> dict:
    """The file as a RAW flat dict — every key, valid or not (125 S19). A missing file
    is {}; an unreadable one raises :class:`PrefsUnreadable` (never overwritten)."""
    if not path.is_file():
        return {}
    try:
        return parse(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise PrefsUnreadable(path, e) from e


def locked_update(path: Path, parse: Callable[[str], dict], key: str,
                  fn: Callable[[object], tuple], render: Callable[[dict], str],
                  cache: dict) -> tuple[bool, str]:
    """Read-modify-write ONE key under a cross-process lock (125 S20).

    ``fn(current)`` gets the stored raw value (None when absent) and returns
    ``(ok, new | REMOVE, err)``; a refusal writes nothing. The write merges into the
    RAW file, so a key this version cannot validate is kept (125 S19); a value the
    renderer cannot write back unchanged refuses the write and names the key."""
    from core.state.files import file_lock
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(str(path)):
            flat = _read_raw(path, parse)
            ok, value, err = fn(flat.get(key))
            if not ok:
                return False, err
            if value is REMOVE:
                flat.pop(key, None)
            else:
                flat[key] = value
            text = render(flat)
            try:
                back = parse(text)
            except Exception:
                back = {}
            lost = sorted(k for k in set(flat) | set(back) if flat.get(k) != back.get(k))
            if lost:
                return False, ("nothing written: preferences.toml holds a value this version "
                               f"cannot write back ({', '.join(lost)}); fix or remove it "
                               f"in {path} first")
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


def locked_write(path: Path, parse: Callable[[str], dict], key: str, value: object,
                 render: Callable[[dict], str], cache: dict) -> tuple[bool, str]:
    """Set *key* (or drop it when *value* is :data:`REMOVE`) — :func:`locked_update`
    with a constant value."""
    return locked_update(path, parse, key, lambda _cur: (True, value, ""), render, cache)
