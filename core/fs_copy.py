"""The ONE way production code copies or moves a tree: safe inside the service sandbox.

Every agent unit runs with ``RestrictSUIDSGID=true`` and ``ProtectSystem=strict``,
and every directory of the data home carries the setgid bit (``drwxrws---``, the
shared ``polyrob-data`` group). Proven under the unit on 2026-11-20:

* ``shutil.copytree`` of ANY data-home directory fails with EPERM, on one mount
  and with ``copy_function=shutil.copyfile`` too: it always ends each directory
  with ``copystat``, whose ``chmod`` carries the setgid bit the sandbox refuses.
* ``os.rename`` between two ``ReadWritePaths`` (the data home, ``HOME``, the
  private ``/tmp``) raises EXDEV — they are separate bind mounts — and
  ``shutil.move`` then falls back to that same ``copytree``.

So: a directory is created, never ``chmod``-ed from its source (it inherits the
group of its parent, which is the point of setgid); a file keeps its permission
bits without setuid/setgid; a move renames, and copies then deletes only on
EXDEV. ``tests/test_fs_copy_ratchet.py`` keeps ``shutil.copytree``,
``copystat``, ``copymode`` and ``move`` out of production code, and every mode
copied from a source (``S_IMODE``) masked to the permission bits.

:func:`widen_dir_mode` is the one way to open a directory's bits after creation:
a plain ``chmod(0o770)`` silently CLEARS the inherited setgid bit (proven under
the unit; the perms audit then names the dir ``no_setgid``), and re-setting it
is EPERM under ``RestrictSUIDSGID`` (the runtime-sandbox contract).
"""
from __future__ import annotations

import errno
import logging
import os
import shutil
import stat
from pathlib import Path
from typing import Callable, Iterable, Optional, Union

PathLike = Union[str, os.PathLike]
IgnoreFn = Callable[[str, list], Iterable[str]]

#: setuid | setgid. Never copied from a source and never set by a sandboxed process.
SPECIAL_BITS = stat.S_ISUID | stat.S_ISGID


def perm_bits(mode: int) -> int:
    """The ``rwx`` bits of *mode* only — the mode a copy may carry."""
    return stat.S_IMODE(mode) & 0o777


def shared_file_mode(directory: PathLike) -> int:
    """The birth mode of a state file: 0660 in a SHARED (setgid) data-home dir, so
    the console and email units read what the agent wrote (and the reverse); 0600
    in a private dir (``wallet/`` is not setgid). ``mkstemp`` and an explicit
    ``0o600`` made every such file unreadable to the sibling units until the next
    deploy's ownership pass (runtime-sandbox review 2026-11-20)."""
    try:
        return 0o660 if os.stat(directory).st_mode & stat.S_ISGID else 0o600
    except OSError:
        return 0o600


def widen_dir_mode(path: PathLike, mode: int, *, dir_fd: Optional[int] = None,
                   required: bool = False) -> int:
    """Give a directory at least *mode*'s ``rwx`` bits and KEEP its setgid bit.

    No call when the bits are already there (the usual case: ``mkdir`` under
    ``UMask=0002`` already made it). Root outside a unit can re-set setgid in the
    same ``chmod``; inside a ``RestrictSUIDSGID`` unit that ``chmod`` is EPERM: by
    default the directory then stays as it is (a narrower mode beats a lost group
    rule). With ``required=True`` (another uid MUST reach the
    dir — a container user, a socket client) the access wins and setgid goes.
    Returns the resulting mode bits. Never follows a symlink."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, dir_fd=dir_fd) if dir_fd is not None else os.open(path, flags)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise NotADirectoryError(str(path)) from exc
        raise
    try:
        have = stat.S_IMODE(os.fstat(fd).st_mode)
        want = have | (mode & 0o777)
        if want == have:
            return have
        try:
            os.fchmod(fd, want)
        except PermissionError:
            if not have & stat.S_ISGID:
                raise
            if required:                       # RestrictSUIDSGID: clearing is allowed
                os.fchmod(fd, want & ~SPECIAL_BITS)
                return want & ~SPECIAL_BITS
            logging.getLogger(__name__).debug(
                "kept %s at %o (setgid) instead of %o: the unit refuses re-setting setgid",
                path, have, want)
            return have
        return want
    finally:
        os.close(fd)


def copy_file(src: PathLike, dst: PathLike) -> Path:
    """Content, the ``rwx`` bits (never setuid/setgid/sticky) and the times."""
    shutil.copyfile(src, dst)
    st = os.stat(src)
    os.chmod(dst, perm_bits(st.st_mode))
    try:
        os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
    except OSError:
        pass
    return Path(dst)


def copy_tree(src: PathLike, dst: PathLike, *, ignore: Optional[IgnoreFn] = None,
              dirs_exist_ok: bool = False, symlinks: bool = False,
              copy_function: Callable[[PathLike, PathLike], object] = copy_file) -> Path:
    """``shutil.copytree`` with the same arguments, minus the directory ``copystat``.

    ``symlinks=True`` recreates a link; False copies what it points to (the
    ``shutil`` default). Raises ``shutil.Error`` on the first file error (copytree raises
    it after the walk) — a half copy is the caller's
    to remove (every caller copies into a temp dir and renames)."""
    src, dst = Path(src), Path(dst)
    names = os.listdir(src)
    skipped = set(ignore(str(src), names)) if ignore else set()
    # The source's rwx bits at birth (never a chmod, never setgid): a 0700 dir
    # stays private across the copy; the umask still applies.
    dst.mkdir(mode=perm_bits(os.stat(src).st_mode) | 0o700, parents=True,
              exist_ok=dirs_exist_ok)
    for name in names:
        if name in skipped:
            continue
        s, d = src / name, dst / name
        if s.is_dir() and not (symlinks and s.is_symlink()):
            copy_tree(s, d, ignore=ignore, dirs_exist_ok=dirs_exist_ok,
                      symlinks=symlinks, copy_function=copy_function)
            continue
        try:
            if symlinks and s.is_symlink():
                os.symlink(os.readlink(s), d)
            else:
                copy_function(s, d)
        except shutil.Error:
            raise
        except OSError as why:   # the type shutil.copytree callers catch
            raise shutil.Error([(str(s), str(d), str(why))]) from why
    return dst


def move(src: PathLike, dst: PathLike) -> Path:
    """Rename; across two mounts (EXDEV) copy with :func:`copy_tree`, then delete.

    ``dst`` is the final path (never "into a directory", unlike ``shutil.move``)."""
    try:
        os.rename(src, dst)
        return Path(dst)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    if os.path.isdir(src) and not os.path.islink(src):
        copy_tree(src, dst, symlinks=True)
        shutil.rmtree(src)
    else:
        copy_file(src, dst)
        os.unlink(src)
    return Path(dst)
