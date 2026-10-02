"""032 — copy the TESTED tree an app runs from.

The supervisor never runs an app from the agent's live project dir: a later edit
must not mutate a public app until the next approved redeploy, and the copy is
where the refusals live — symlinks (an escape from the tree), ``.git`` and
caches (size, secrets in reflogs), and credential-shaped files
(``core.security.secret_guard.is_credential_file``) are never copied.

Which files ship is decided in ONE place, :mod:`core.ship_tree`, shared with
``tools/hf_deploy/digest.py`` so the recorded "tested" digest identifies exactly
these bytes.

Every file is opened ``O_NOFOLLOW`` and copied FROM THE DESCRIPTOR: a path
swapped to a symlink between the walk and the copy fails the open (``ELOOP``)
and is recorded as refused, instead of being followed out of the tree.

The DESTINATION side runs as root inside a group-writable data home (H07), so
it never resolves a path either: the old snapshot is removed with a
``dir_fd``-relative ``rmtree`` (symlink-attack safe), every directory is made
with ``mkdir(dir_fd=…)`` and re-opened ``O_DIRECTORY|O_NOFOLLOW``, and every file
is created ``O_CREAT|O_EXCL|O_NOFOLLOW`` relative to its parent's descriptor. A
planted symlink or pre-created file fails the copy instead of redirecting a root
write. :func:`open_owned_dir` is the same rule for the supervisor's own tree
(``<data>/apps/<tenant>/<slug>/…``): each component must be a real directory
owned by this process, and group/other write is stripped from it.
"""
import errno
import os
import shutil
import stat
from typing import Dict, List, Optional, Sequence, Tuple

from core.ship_tree import SKIP_DIRS, walk_shippable  # noqa: F401  (SKIP_DIRS re-exported)

_COPY_CHUNK = 1024 * 1024
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | os.O_NOFOLLOW


class SnapshotTooLarge(ValueError):
    """The tree exceeds ``max_mb``; nothing is left behind at ``dst``."""


class UnsafePath(OSError):
    """A path component is a symlink, not a directory, or not ours."""


def _check_name(name: str) -> str:
    if not name or name in (".", "..") or "/" in name or "\0" in name or os.sep in name:
        raise UnsafePath(errno.EINVAL, f"unsafe path component {name[:60]!r}")
    return name


def _open_child_dir(parent_fd: int, name: str, *, create: bool, own: bool) -> int:
    """Open (optionally mkdir) *name* under *parent_fd* without following a
    symlink. With *own*, the directory must belong to this euid and loses
    group/other write."""
    _check_name(name)
    if create:
        try:
            os.mkdir(name, 0o755, dir_fd=parent_fd)
        except FileExistsError:
            pass
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR):
            raise UnsafePath(e.errno, f"{name!r} is not a real directory (symlink?)") from e
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode):
            raise UnsafePath(errno.ENOTDIR, f"{name!r} is not a directory")
        if own:
            if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
                raise UnsafePath(errno.EPERM, f"{name!r} is owned by uid {st.st_uid}, not us")
            if st.st_mode & 0o022:
                os.fchmod(fd, stat.S_IMODE(st.st_mode) & ~0o022)
    except BaseException:
        os.close(fd)
        raise
    return fd


def open_owned_dir(root: str, parts: Sequence[str], *, create: bool = True) -> int:
    """A descriptor for ``root/parts…`` where *root* is trusted configuration
    (the data home) and every component under it is opened no-follow, must be
    owned by this process and is stripped of group/other write. Caller closes."""
    if create:
        os.makedirs(root, exist_ok=True)
    fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        for part in parts:
            nxt = _open_child_dir(fd, part, create=create, own=True)
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd


def _remove_entry(parent_fd: int, name: str) -> None:
    """Remove *name* under *parent_fd* whatever it is, never following a link."""
    try:
        st = os.lstat(name, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode):
        shutil.rmtree(name, dir_fd=parent_fd)
    else:
        os.unlink(name, dir_fd=parent_fd)


def _copy_nofollow(src: str, dst_dir_fd: int, name: str, *, budget: int) -> int:
    """Copy *src* -> ``dst_dir_fd/name`` without ever following a symlink on
    either side. Returns the byte count. Raises ``SnapshotTooLarge`` when the
    file would blow *budget*, and ``OSError(ELOOP)`` when *src* is (or became)
    a symlink."""
    fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(errno.ELOOP, "not a regular file", src)
        if st.st_size > budget:
            raise SnapshotTooLarge(f"{src} exceeds the remaining snapshot budget")
        out_fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                         dir_fd=dst_dir_fd)
        try:
            while True:
                chunk = os.read(fd, _COPY_CHUNK)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    n = os.write(out_fd, view)
                    view = view[n:]
            os.fchmod(out_fd, stat.S_IMODE(st.st_mode) & ~0o022)
            os.utime(out_fd, (st.st_atime, st.st_mtime))
        finally:
            os.close(out_fd)
        return int(st.st_size)
    finally:
        os.close(fd)


def snapshot_tree(src: str, dst: str, *, max_mb: int,
                  parent_fd: Optional[int] = None) -> Tuple[int, int, List[str]]:
    """Copy *src* into *dst* (created fresh). Returns ``(bytes, files, skipped)``
    where *skipped* names every refused path (relative), so the result can say
    exactly what the app does NOT carry.

    With *parent_fd*, *dst* is ONE name under that directory descriptor (the
    supervisor's no-follow path); without it, *dst* is a path whose parent is
    created and opened by path (tests, local use)."""
    src_real = os.path.realpath(src)
    if not os.path.isdir(src_real):
        raise ValueError(f"not a directory: {src!r}")
    limit = int(max_mb) * 1024 * 1024
    own_parent = parent_fd is None
    if own_parent:
        parent = os.path.dirname(os.path.abspath(dst))
        os.makedirs(parent, exist_ok=True)
        parent_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        name = os.path.basename(os.path.abspath(dst))
    else:
        name = dst
    try:
        _check_name(name)
        _remove_entry(parent_fd, name)
        root_fd = _open_child_dir(parent_fd, name, create=True, own=False)
        dir_fds: Dict[str, int] = {"": root_fd}

        def _dir_fd(rel_dir: str) -> int:
            if rel_dir in dir_fds:
                return dir_fds[rel_dir]
            head, _, tail = rel_dir.rpartition("/")
            fd = _open_child_dir(_dir_fd(head), tail, create=True, own=False)
            dir_fds[rel_dir] = fd
            return fd

        total = 0
        files = 0
        skipped: List[str] = []
        try:
            for rel, full in walk_shippable(src_real, skipped):
                rel_dir, _, fname = rel.rpartition("/")
                try:
                    size = _copy_nofollow(full, _dir_fd(rel_dir), _check_name(fname),
                                          budget=limit - total)
                except SnapshotTooLarge:
                    raise SnapshotTooLarge(
                        f"tree exceeds APP_SERVICE_SNAPSHOT_MAX_MB={max_mb} at {rel}")
                except UnsafePath:
                    raise
                except OSError as e:
                    if e.errno in (errno.ELOOP, errno.EMLINK, errno.ENOENT):
                        # Swapped to a symlink (or vanished) after the walk saw a
                        # regular file: refuse it exactly like a declared symlink.
                        skipped.append(f"{rel} (symlink)")
                        continue
                    raise
                total += size
                files += 1
        except BaseException:
            for fd in dir_fds.values():
                os.close(fd)
            dir_fds.clear()
            try:
                _remove_entry(parent_fd, name)
            except OSError:
                pass
            raise
        finally:
            for fd in dir_fds.values():
                os.close(fd)
        return total, files, skipped
    finally:
        if own_parent:
            os.close(parent_fd)
