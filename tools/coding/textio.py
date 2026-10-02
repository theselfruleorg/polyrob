"""Faithful text I/O for the coding tool (coding-agent review B4, 2026-09-24).

Every coding write used to open the file in text mode: a CRLF file came back
LF on the first edit (a whole-file diff), a crash mid-write left a torn file,
and a binary file failed with a generic decode error.

``read_text`` returns the content with ``\\n`` line ends plus what it takes to
put the file back the way it was (line ending, BOM); ``write_text`` restores
both and replaces the file atomically (temp file in the same directory, fsync,
``os.replace``, then fsync of the directory), keeping the file's mode, writing
THROUGH a symlink rather than replacing the link — after re-checking that the
resolved target is still inside the workspace — and in place for a hard link.

A file with MIXED line endings is passed through untouched (no normalisation),
so an edit never rewrites lines it did not touch.

LANDMINE: NO ``from __future__ import annotations`` anywhere in ``tools/coding/``.
"""
import os
import tempfile
from dataclasses import dataclass
from typing import Optional

_BOM = "﻿"
_SNIFF_BYTES = 8192


class NotTextError(ValueError):
    """The file is binary or not UTF-8; the coding tool edits text only."""


@dataclass(frozen=True)
class TextFile:
    content: str   # line ends normalised to "\n" (unless ``eol`` is "")
    eol: str       # "\n", "\r\n", or "" = mixed, passed through verbatim
    bom: bool


def read_text(path: str) -> TextFile:
    with open(path, "rb") as f:
        raw = f.read()
    if b"\x00" in raw[:_SNIFF_BYTES]:
        raise NotTextError(f"binary file (NUL byte): {os.path.basename(path)}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise NotTextError(
            f"not UTF-8 text (byte {e.start}): {os.path.basename(path)}") from None
    bom = text.startswith(_BOM)
    if bom:
        text = text[1:]
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    if crlf and lf:
        return TextFile(text, "", bom)
    if crlf:
        return TextFile(text.replace("\r\n", "\n"), "\r\n", bom)
    return TextFile(text, "\n", bom)


def to_file_eol(fragment: str, eol: str) -> str:
    """Normalise a model-supplied fragment to the in-memory form of a file
    read with *eol* (``"\\n"`` for a uniform file, verbatim for a mixed one)."""
    return fragment.replace("\r\n", "\n") if eol else fragment


def write_text(path: str, content: str, eol: str = "\n", bom: bool = False, *,
               root: Optional[str] = None) -> None:
    if eol == "\r\n":
        content = content.replace("\r\n", "\n").replace("\n", "\r\n")
    if bom:
        content = _BOM + content
    atomic_write_bytes(path, content.encode("utf-8"), root=root)


def atomic_write_bytes(path: str, data: bytes, *, root: Optional[str] = None) -> None:
    """Replace *path* with *data* atomically.

    *root* re-checks confinement on the RESOLVED destination right before the
    write: the caller's check ran before an awaited snapshot, and a symlink
    swapped in meanwhile must not redirect the write outside the workspace
    (codex review 2026-09-25). A read-only file is refused (a rename would
    replace it anyway), and a hard-linked file is rewritten in place so every
    link keeps seeing the change.
    """
    dest = os.path.realpath(path)
    if root is not None:
        from core.path_safety import is_within_root
        if not is_within_root(dest, root):
            raise PermissionError(f"refusing to write outside the workspace: {path}")
    directory = os.path.dirname(dest) or "."
    try:
        st = os.stat(dest)
    except FileNotFoundError:
        st = None
    if st is not None and not os.access(dest, os.W_OK):
        raise PermissionError(f"file is read-only: {path}")
    if st is not None and st.st_nlink > 1:
        with open(dest, "r+b") as f:
            f.write(data)
            f.truncate()
            f.flush()
            os.fsync(f.fileno())
        return
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".polyrob-edit-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, (st.st_mode & 0o7777) if st is not None else (0o666 & ~_umask()))
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(directory)


def _umask() -> int:
    """The process umask WITHOUT setting it (``os.umask`` must write to read,
    which races every other thread's file creation). Linux exposes it in
    /proc; elsewhere assume the common 022."""
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("Umask:"):
                    return int(line.split()[1], 8)
    except (OSError, ValueError, IndexError):
        pass
    return 0o022


def _fsync_dir(directory: str) -> None:
    """Make the rename durable (best effort: not every platform allows it)."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
