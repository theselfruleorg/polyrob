"""Write an uploaded file into a session workspace safely (API18).

Split out of ``api/task_http_api.py`` (a ratcheted god-file).
"""
import asyncio
import os
import uuid


def write_new_file(path, data: bytes) -> None:
    """Create ``path`` and write ``data``; refuse an existing name or a symlink.

    API18: ``open(path, 'wb')`` followed a symlink planted at ``path`` — a
    DANGLING one passes ``Path.exists()`` as False — and wrote wherever it
    pointed. ``O_EXCL`` refuses any existing entry (a symlink included,
    dangling or not); ``O_NOFOLLOW`` is the belt to that brace.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), flags, 0o644)
    try:
        with os.fdopen(fd, "wb") as f:
            fd = None
            f.write(data)
    finally:
        if fd is not None:
            os.close(fd)


async def store_upload(workspace_dir, safe_filename: str, data: bytes):
    """Write an upload under ``workspace_dir`` without clobbering, in a thread.

    Tries the sanitized name, then a timestamped name, then a timestamped name
    with a random tag. Returns the path written.
    """
    from datetime import datetime
    from pathlib import Path

    base = Path(safe_filename)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    names = [safe_filename, f"{base.stem}_{stamp}{base.suffix}",
             f"{base.stem}_{stamp}_{uuid.uuid4().hex[:8]}{base.suffix}"]
    last_error = None
    for name in names:
        path = workspace_dir / name
        try:
            await asyncio.to_thread(write_new_file, path, data)
            return path
        except FileExistsError as e:
            last_error = e
    raise last_error
