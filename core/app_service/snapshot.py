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
"""
import errno
import os
import shutil
import stat
from typing import List, Tuple

from core.ship_tree import SKIP_DIRS, walk_shippable  # noqa: F401  (SKIP_DIRS re-exported)

_COPY_CHUNK = 1024 * 1024


class SnapshotTooLarge(ValueError):
    """The tree exceeds ``max_mb``; nothing is left behind at ``dst``."""


def _copy_nofollow(src: str, dst: str, *, budget: int) -> int:
    """Copy *src* -> *dst* without ever following a symlink. Returns the byte
    count. Raises ``SnapshotTooLarge`` when the file would blow *budget*, and
    ``OSError(ELOOP)`` when *src* is (or became) a symlink."""
    fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(errno.ELOOP, "not a regular file", src)
        if st.st_size > budget:
            raise SnapshotTooLarge(f"{src} exceeds the remaining snapshot budget")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as out:
            while True:
                chunk = os.read(fd, _COPY_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
        os.chmod(dst, stat.S_IMODE(st.st_mode))
        os.utime(dst, (st.st_atime, st.st_mtime))
        return int(st.st_size)
    finally:
        os.close(fd)


def snapshot_tree(src: str, dst: str, *, max_mb: int) -> Tuple[int, int, List[str]]:
    """Copy *src* into *dst* (created fresh). Returns ``(bytes, files, skipped)``
    where *skipped* names every refused path (relative), so the result can say
    exactly what the app does NOT carry."""
    src_real = os.path.realpath(src)
    if not os.path.isdir(src_real):
        raise ValueError(f"not a directory: {src!r}")
    limit = int(max_mb) * 1024 * 1024
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    os.makedirs(dst, exist_ok=True)
    total = 0
    files = 0
    skipped: List[str] = []
    try:
        for rel, full in walk_shippable(src_real, skipped):
            target = os.path.join(dst, *rel.split("/"))
            try:
                size = _copy_nofollow(full, target, budget=limit - total)
            except SnapshotTooLarge:
                raise SnapshotTooLarge(
                    f"tree exceeds APP_SERVICE_SNAPSHOT_MAX_MB={max_mb} at {rel}")
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
        shutil.rmtree(dst, ignore_errors=True)
        raise
    return total, files, skipped
