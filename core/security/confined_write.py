"""Descriptor-anchored file mutations for bounded, tenant-owned stores.

The trusted root may be a platform alias (e.g. /tmp on macOS). Components
below it are never resolved through symlinks. Unsupported platforms refuse.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import secrets
import stat


def confined_path(path: Path, root: Path) -> Path:
    """Normalize the trusted root only; reject traversal in the candidate."""
    root = Path(root).absolute()
    path = Path(path).absolute()
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise OSError("file outside allowed root") from exc
    if ".." in relative.parts:
        raise OSError("parent traversal is not allowed")
    return root.resolve() / relative


@contextmanager
def confined_parent(path: Path, root: Path, *, create: bool = False):
    """Open the parent without following any untrusted directory component."""
    path = confined_path(path, root)
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise OSError("safe descriptor-relative file access is unavailable")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open(path.anchor, flags)
    try:
        for component in path.parts[1:-1]:
            if create:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=directory)
                except FileExistsError:
                    pass
            child = os.open(component, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        yield directory, path.name
    finally:
        os.close(directory)


def _regular_or_absent(directory: int, name: str) -> None:
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise OSError("only regular files without link aliases may be modified")


def write_confined_text(path: Path, root: Path, text: str) -> None:
    """Write and fsync a sibling temporary file, then atomically replace."""
    with confined_parent(path, root, create=True) as (directory, name):
        _regular_or_absent(directory, name)
        temporary = f".write-{secrets.token_hex(12)}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
            _regular_or_absent(directory, name)
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass


def replace_confined(source: Path, target: Path, root: Path) -> None:
    with confined_parent(source, root) as (src_fd, src_name):
        with confined_parent(target, root, create=True) as (dst_fd, dst_name):
            _regular_or_absent(src_fd, src_name)
            _regular_or_absent(dst_fd, dst_name)
            os.replace(src_name, dst_name, src_dir_fd=src_fd, dst_dir_fd=dst_fd)
            os.fsync(dst_fd)


def unlink_confined(path: Path, root: Path, *, directory: bool = False) -> None:
    with confined_parent(path, root) as (parent, name):
        if directory:
            os.rmdir(name, dir_fd=parent)
        else:
            _regular_or_absent(parent, name)
            os.unlink(name, dir_fd=parent)


def list_confined_directory(path: Path, root: Path) -> list[str]:
    with confined_parent(path, root) as (parent, name):
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            return sorted(os.listdir(fd))
        finally:
            os.close(fd)
