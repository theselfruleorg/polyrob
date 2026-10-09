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
from contextlib import contextmanager
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


def _check_readable(info, path, shared_ok: bool = False) -> None:
    """A read needs a single-link regular file.

    ``shared_ok`` (the agent's own file tools only: read, copy, grep, edit)
    also reads a hard-linked file that is WORLD-readable: uv and pnpm trees
    hard-link 0644 files from their caches, and those are ordinary project
    files. A link planted to a private file (the data home is born 0660,
    secrets 0600) stays refused, so a hard link cannot expose a file the path
    rule hides. Every other caller (ledgers, stores, exports) stays strict."""
    if not stat.S_ISREG(info.st_mode):
        raise UnsafePath(f"not a regular file: {path}")
    if info.st_nlink != 1 and not (shared_ok and info.st_mode & stat.S_IROTH):
        raise UnsafePath(f"not a single-link regular file: {path}")


def read_bytes(path, root, *, max_bytes: int | None = None,
               shared_ok: bool = False) -> bytes:
    """Read a regular file inside *root*; no component is followed after the check."""
    real, root_real = _resolved(path, root)
    with confined_parent(real, root_real) as (directory, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            _check_readable(os.fstat(fd), path, shared_ok)
            with os.fdopen(fd, "rb", closefd=False) as stream:
                return stream.read() if max_bytes is None else stream.read(max_bytes)
        finally:
            os.close(fd)


def read_text(path, root, *, encoding: str = "utf-8", errors: str = "replace") -> str:
    return read_bytes(path, root).decode(encoding, errors=errors)


@contextmanager
def open_read(path, root, *, shared_ok: bool = False):
    """Yield a binary stream over a regular file inside *root*, opened the same
    way as :func:`read_bytes` (pinned parent walk, ``O_NOFOLLOW``), for callers
    that stream lines instead of loading the whole file."""
    real, root_real = _resolved(path, root)
    with confined_parent(real, root_real) as (directory, name):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            _check_readable(os.fstat(fd), path, shared_ok)
            with os.fdopen(fd, "rb", closefd=False) as stream:
                yield stream
        finally:
            os.close(fd)


def read_trusted_config(path, *, max_bytes=1024 * 1024) -> str:
    """Read credential-equivalent config from a pinned, non-replaceable path."""
    target = Path(path).absolute()
    with confined_parent(target, target.parent) as (directory, name):
        parent = os.dup(directory)
        try:
            while True:
                info = os.fstat(parent)
                if info.st_uid not in {0, os.geteuid()} or (
                        info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX):
                    raise UnsafePath("configuration ancestor is not trusted or is group/world-writable")
                above = os.open("..", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                upper = os.fstat(above)
                if (upper.st_dev, upper.st_ino) == (info.st_dev, info.st_ino):
                    os.close(above)
                    break
                os.close(parent)
                parent = above
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022):
                    raise UnsafePath("configuration is not a trusted single-link regular file")
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    raw = stream.read(max_bytes + 1)
                if len(raw) > max_bytes:
                    raise UnsafePath("configuration is too large")
                return raw.decode("utf-8")
            finally:
                os.close(fd)
        finally:
            os.close(parent)


def write_bytes(path, root, data: bytes, *, mode: int | None = None) -> None:
    """Atomically replace *path* with *data* (random O_EXCL|O_NOFOLLOW temp +
    os.replace at the parent descriptor)."""
    target, root_real = _split(path, root)
    with confined_parent(target, root_real, create=True) as (directory, name):
        try:
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                raise UnsafePath(f"destination is a directory: {path}")
            if stat.S_ISREG(info.st_mode) and mode is None:
                mode = stat.S_IMODE(info.st_mode)
        except FileNotFoundError:
            pass
        temporary = f".{name[:40]}.{secrets.token_hex(8)}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o666 if mode is None else mode, dir_fd=directory)
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


def write_text(path, root, text: str, *, encoding: str = "utf-8", mode: int | None = None) -> None:
    write_bytes(path, root, text.encode(encoding), mode=mode)


def append_bytes(path, root, data: bytes) -> None:
    """O_APPEND onto the entry at *path* (created if absent); a symlink at the
    final name is refused, not followed."""
    target, root_real = _split(path, root)
    with confined_parent(target, root_real, create=True) as (directory, name):
        fd = os.open(name, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                     0o666, dir_fd=directory)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise UnsafePath(f"not a single-link regular file: {path}")
            with os.fdopen(fd, "ab", closefd=False) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(fd)


def move(src, dest, root, *, overwrite: bool = False) -> None:
    """Rename the ENTRY at *src* to *dest*, both inside *root*, through pinned
    parent descriptors: a component swapped for a symlink after the caller's
    check fails the walk instead of moving a file from or to outside the root.
    Without *overwrite* an existing destination is refused atomically (a hard
    link for a file, so nothing is replaced in a race)."""
    source, root_real = _split(src, root)
    target, _ = _split(dest, root)
    with confined_parent(source, root_real) as (src_dir, src_name):
        info = os.stat(src_name, dir_fd=src_dir, follow_symlinks=False)
        with confined_parent(target, root_real, create=True) as (dst_dir, dst_name):
            try:
                existing = os.stat(dst_name, dir_fd=dst_dir, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None and stat.S_ISDIR(existing.st_mode):
                raise UnsafePath(f"destination is a directory: {dest}")
            if existing is not None and not overwrite:
                raise FileExistsError(f"destination exists: {dest}")
            if overwrite or stat.S_ISDIR(info.st_mode):
                os.replace(src_name, dst_name, src_dir_fd=src_dir, dst_dir_fd=dst_dir)
            else:
                os.link(src_name, dst_name, src_dir_fd=src_dir, dst_dir_fd=dst_dir,
                        follow_symlinks=False)
                os.unlink(src_name, dir_fd=src_dir)


def unlink(path, root) -> None:
    """Remove the ENTRY at *path* (a symlink is removed, never its target)."""
    target, root_real = _split(path, root)
    with confined_parent(target, root_real) as (directory, name):
        os.unlink(name, dir_fd=directory)
