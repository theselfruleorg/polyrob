"""Bounded regular-file snapshots anchored by directory descriptors (POSIX)."""
import os
import stat
from pathlib import Path


class UnsafeRead(OSError):
    """The requested file cannot safely be snapshotted inside its allowed root."""


def read_confined_bytes(path: Path, root: Path, max_bytes: int) -> bytes:
    # Resolve the trusted root, never the attacker-controlled candidate.
    root = root.resolve()
    candidate = Path(os.path.abspath(path))
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise UnsafeRead('file outside allowed root') from exc
    if not relative.parts or max_bytes < 0:
        raise UnsafeRead('invalid file snapshot request')
    if not hasattr(os, 'O_NOFOLLOW') or os.open not in os.supports_dir_fd:
        raise UnsafeRead('safe descriptor-relative file access is unavailable')
    # Walk from / so replacing a parent component cannot redirect later opens.
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open(root.anchor, flags)
    try:
        for component in (*root.parts[1:], *relative.parts[:-1]):
            child = os.open(component, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise UnsafeRead('only regular files without hard-link aliases may be ingested')
            if info.st_size > max_bytes:
                raise UnsafeRead('file exceeds remaining ingestion byte budget')
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                content = stream.read(max_bytes + 1)
            if len(content) > max_bytes:
                raise UnsafeRead('file grew beyond ingestion byte budget')
            return content
        finally:
            os.close(fd)
    finally:
        os.close(directory)
