"""Faithful text I/O for the coding tool (coding-agent review B4, 2026-09-24).

Every coding write used to open the file in text mode: a CRLF file came back
LF on the first edit (a whole-file diff), a crash mid-write left a torn file,
and a binary file failed with a generic decode error.

``read_text`` returns the content with ``\\n`` line ends plus what it takes to
put the file back the way it was (line ending, BOM); ``write_text`` restores
both and replaces the file atomically (temp file in the same directory, fsync,
``os.replace``, then fsync of the directory), keeping the file's mode, writing
THROUGH a symlink rather than replacing the link — after re-checking that the
resolved target is still inside the workspace — and refusing hard links with unknown destinations.

A file with MIXED line endings is passed through untouched (no normalisation),
so an edit never rewrites lines it did not touch.

LANDMINE: NO ``from __future__ import annotations`` anywhere in ``tools/coding/``.
"""
import os
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


def read_text(path: str, *, root: Optional[str] = None) -> TextFile:
    from core.security.workspace_io import read_bytes
    raw = read_bytes(path, root or os.path.dirname(os.path.realpath(path)), shared_ok=True)
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
    """Replace a confined, single-link file through pinned directory descriptors."""
    from core.security.workspace_io import write_bytes
    dest = os.path.realpath(path)
    if root is not None:
        from core.path_safety import is_within_root
        if not is_within_root(dest, root):
            raise PermissionError(f"refusing to write outside the workspace: {path}")
    try:
        st = os.stat(dest)
    except FileNotFoundError:
        st = None
    # A hard-linked file (uv/pnpm trees) is fine: write_bytes replaces the
    # directory ENTRY, so the other links keep their bytes and nothing is
    # written through the link.
    if st is not None and not os.access(dest, os.W_OK):
        raise PermissionError(f"file is read-only: {path}")
    write_bytes(dest, root or os.path.dirname(dest), data)
