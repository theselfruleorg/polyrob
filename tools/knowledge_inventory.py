"""Bounded discovery of candidate KB files, before file reads or parsing."""
import os
from pathlib import Path
import subprocess
import threading
import time

from tools.code_exec.backends.bounded_capture import capture

MAX_ENTRIES = 20_000
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_DEPTH = 64
TIMEOUT_SECONDS = 5


def rg_files(root: Path):
    """Return (paths, capped), or None if rg is unavailable/failed.

    NUL records preserve newlines in filenames. Exhausting a budget never starts
    a second, unbounded traversal via the fallback.
    """
    try:
        proc = subprocess.Popen(
            ["rg", "--files", "--null", "--threads", "1", str(root)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={"PATH": os.environ.get("PATH", os.defpath)},
        )
    except OSError:
        return None
    def kill(child):
        try:
            child.kill()
        except ProcessLookupError:
            pass
    output, _, timed_out, overflow = capture(
        proc, data=None, timeout=TIMEOUT_SECONDS, limit=MAX_OUTPUT_BYTES,
        stop=threading.Event(), kill=kill,
    )
    capped = timed_out or overflow
    if proc.returncode not in (0, 1) and not capped:
        return None
    # Ignore the final incomplete record, including after a timeout/overflow.
    records = output.split(b"\0")[:-1]
    capped = capped or len(records) > MAX_ENTRIES
    paths = []
    for record in records[:MAX_ENTRIES]:
        if record:
            path = Path(os.fsdecode(record))
            paths.append(path if path.is_absolute() else root / path)
    return paths, capped


def walk_files(root: Path, *, recursive: bool):
    """Stream scandir entries; bound directories, depth, bytes and elapsed time.

    Unlike os.walk/Path.iterdir this never materializes every entry of a huge
    directory. A filesystem syscall can still block on an unhealthy mount.
    """
    paths = []
    stack = []
    count = output_bytes = 0
    capped = False
    deadline = time.monotonic() + TIMEOUT_SECONDS
    try:
        stack.append(os.scandir(root))
        while stack:
            if count >= MAX_ENTRIES or time.monotonic() >= deadline:
                capped = True
                break
            entry = next(stack[-1], None)
            if entry is None:
                stack.pop().close()
                continue
            count += 1
            output_bytes += len(os.fsencode(entry.path))
            if output_bytes > MAX_OUTPUT_BYTES:
                capped = True
                break
            try:
                if entry.is_dir(follow_symlinks=False):
                    if recursive and not entry.name.startswith('.') and entry.name != '__pycache__':
                        if len(stack) >= MAX_DEPTH:
                            capped = True
                        else:
                            stack.append(os.scandir(entry.path))
                else:
                    paths.append(Path(entry.path))
            except OSError:
                continue
    except OSError:
        capped = True
    finally:
        for iterator in stack:
            iterator.close()
    return paths, capped
