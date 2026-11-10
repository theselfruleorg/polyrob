"""Atomic and locked JSON state files (098 S8).

``atomic_write_json`` never leaves a torn file; ``locked_update_json`` makes a
read-modify-write exclusive across processes. The lock lives on a sidecar
``<path>.lock`` that is never replaced, so an ``os.replace`` of the data file
cannot move the lock to an unlinked inode.
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
from typing import Any, Callable

try:  # POSIX only; on other platforms the update is atomic but not exclusive
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


def atomic_write_json(path: str, obj: Any, *, mode: int = 0o600, indent: int = 2) -> None:
    """mkstemp in the same directory + fsync + os.replace (+ best-effort dir fsync)."""
    path = os.fspath(path)
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=indent, sort_keys=False, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    with contextlib.suppress(OSError):
        dfd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


def read_json(path: str, default: Any = None) -> Any:
    """The file's JSON, or ``default`` when it is missing or torn — never a
    special "fail-closed" value that callers must recognise."""
    try:
        with open(os.fspath(path), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


class UnreadableStateFile(RuntimeError):
    """A state file EXISTS but cannot be read or parsed. Not "empty": a writer
    that went on from a default would overwrite what it could not read."""

    def __init__(self, path: str, error: BaseException):
        super().__init__(f"state file {path} is unreadable: {type(error).__name__}: {error}")
        self.path, self.error = path, error


def read_json_strict(path: str, default: Any = None) -> Any:
    """The file's JSON; ``default`` ONLY when the file is missing. A torn,
    unparsable or unreadable file raises :class:`UnreadableStateFile`."""
    try:
        with open(os.fspath(path), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as e:
        raise UnreadableStateFile(os.fspath(path), e) from e


@contextlib.contextmanager
def file_lock(path: str):
    """Exclusive cross-process lock on ``<path>.lock``."""
    lock_path = os.fspath(path) + ".lock"
    os.makedirs(os.path.dirname(os.path.abspath(lock_path)), exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def locked_update_json(path: str, fn: Callable[[Any], Any], *, default: Any,
                       mode: int = 0o600) -> Any:
    """Read, ``fn(data)``, write — exclusive across processes.

    ``fn`` mutates ``data`` in place and returns a result, or returns a NEW
    object to store by wrapping it as ``Replace(obj)``. The result is returned.
    An existing file that cannot be read raises :class:`UnreadableStateFile`
    and is never overwritten.
    """
    with file_lock(path):
        data = read_json_strict(path, default() if callable(default) else _copy(default))
        result = fn(data)
        if isinstance(result, Replace):
            data, result = result.value, result.value
        atomic_write_json(path, data, mode=mode)
        return result


class Replace:
    """Returned by an update ``fn`` to store a new top-level object."""

    __slots__ = ("value",)

    def __init__(self, value: Any):
        self.value = value


def _copy(v: Any) -> Any:
    return json.loads(json.dumps(v)) if v is not None else None
