"""Descriptor-anchored file I/O for the agent's file tools (security analysis
2026-09-23, H09).

The filesystem tool confined on realpath and then did a plain ``open()`` /
``shutil.move`` on the path string: a sandbox that shares the workspace could
swap a component for a symlink between the check and the use (TOCTOU), and the
write temp name ``<file>.<ms>.tmp`` was predictable, so a pre-planted symlink
at that name redirected a host write.

These helpers close both on top of ``confined_write.confined_parent`` (an
``O_NOFOLLOW`` descriptor walk from ``/``):

- the PARENT directory is resolved (realpath), checked to sit inside the root,
  then re-opened component by component with ``O_NOFOLLOW`` — a component
  swapped after the check fails the walk instead of redirecting it;
- reads open the (resolved) final component with ``O_NOFOLLOW`` and refuse
  anything that is not a regular file;
- writes go to a RANDOM sibling temp opened ``O_CREAT|O_EXCL|O_NOFOLLOW`` and
  land with ``os.replace`` at the directory descriptor, which replaces the
  directory ENTRY and never follows a link at the final name.

Unlike ``write_confined_text`` these keep the ordinary permission shape
(``0o666`` minus umask, an existing target's mode preserved): a docker sandbox
that runs as another uid must still read what the agent wrote.
"""
import os
import secrets
import stat
from pathlib import Path

from core.security.confined_write import confined_parent


class UnsafePath(OSError):
    """The path cannot be used safely inside its allowed root."""


def _split(path, root):
    """(resolved parent / name, resolved root). The final component is kept
    LEXICAL so an entry (a symlink included) is replaced/unlinked, never followed."""
    root_real = Path(os.path.realpath(root))
    lexical = Path(os.path.abspath(path))
    if not lexical.name or lexical.name in (".", ".."):
        raise UnsafePath(f"invalid file name: {path}")
    parent_real = Path(os.path.realpath(lexical.parent))
    try:
        parent_real.relative_to(root_real)
    except ValueError as exc:
        raise UnsafePath(f"path escapes the allowed root: {path}") from exc
    return parent_real / lexical.name, root_real


def _resolved(path, root):
    """Full realpath (a legitimate in-root symlink is followed ONCE, here),
    checked inside the root."""
    root_real = Path(os.path.realpath(root))
    real = Path(os.path.realpath(path))
    try:
        rel = real.relative_to(root_real)
    except ValueError as exc:
        raise UnsafePath(f"path escapes the allowed root: {path}") from exc
    if not rel.parts:
        raise UnsafePath(f"not a file: {path}")
    return real, root_real


def read_bytes(path, root) -> bytes:
    """Read a regular file inside *root*; no component is followed after the check."""
    real, root_real = _resolved(path, root)
    with confined_parent(real, root_real) as (directory, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise UnsafePath(f"not a regular file: {path}")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                return stream.read()
        finally:
            os.close(fd)


def read_text(path, root, *, encoding: str = "utf-8", errors: str = "replace") -> str:
    return read_bytes(path, root).decode(encoding, errors=errors)


def write_bytes(path, root, data: bytes) -> None:
    """Atomically replace *path* with *data* (random O_EXCL|O_NOFOLLOW temp +
    os.replace at the parent descriptor)."""
    target, root_real = _split(path, root)
    with confined_parent(target, root_real, create=True) as (directory, name):
        mode = None
        try:
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                raise UnsafePath(f"destination is a directory: {path}")
            if stat.S_ISREG(info.st_mode):
                mode = stat.S_IMODE(info.st_mode)
        except FileNotFoundError:
            pass
        temporary = f".{name[:40]}.{secrets.token_hex(8)}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o666, dir_fd=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
                if mode is not None:
                    os.fchmod(stream.fileno(), mode)
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


def write_text(path, root, text: str, *, encoding: str = "utf-8") -> None:
    write_bytes(path, root, text.encode(encoding))


def append_bytes(path, root, data: bytes) -> None:
    """O_APPEND onto the entry at *path* (created if absent); a symlink at the
    final name is refused, not followed."""
    target, root_real = _split(path, root)
    with confined_parent(target, root_real, create=True) as (directory, name):
        fd = os.open(name, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
                     0o666, dir_fd=directory)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise UnsafePath(f"not a regular file: {path}")
            with os.fdopen(fd, "ab", closefd=False) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(fd)


def unlink(path, root) -> None:
    """Remove the ENTRY at *path* (a symlink is removed, never its target)."""
    target, root_real = _split(path, root)
    with confined_parent(target, root_real) as (directory, name):
        os.unlink(name, dir_fd=directory)
