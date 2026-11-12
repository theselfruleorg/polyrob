"""Where an over-size tool result is saved, and how long it lives.

A result too large for the model's context is written to a file the model can
page through. The files live in ONE directory per session inside the
workspace — ``<workspace>/.tool_results/<session id>/`` — never at the
workspace root (in project-root mode the root is the owner's project; prod
held 535 such files there, the oldest 12 weeks old). The directory is not
``.polyrob/…``: in the local shape ``<cwd>/.polyrob`` can be the data home,
which the file tools refuse to read.

Nothing here knows about sessions or agents: the writer
(``agents/task/agent/core/result_offload.py``), the reader
(``tools/filesystem.py``) and the retention pass import this module.
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Optional

logger = logging.getLogger(__name__)

TOOL_RESULTS_DIR = ".tool_results"
#: A saved result older than this is removed (its pointer is far out of context).
MAX_AGE_SEC = 7 * 86400
#: One read of a saved result returns at most this many characters: a larger
#: slice would itself be over the wire cap and be saved again as a new file.
READ_SLICE_CHARS = 20000

_SAFE_ID = re.compile(r"[^\w\-]")


def session_part(session_id: str) -> str:
    return _SAFE_ID.sub("_", str(session_id or ""))[:80] or "session"


def relative_path(session_id: str, filename: str) -> str:
    """The workspace-relative path of one saved result (posix separators: it
    is shown to the model as a ``file_path`` argument)."""
    return f"{TOOL_RESULTS_DIR}/{session_part(session_id)}/{filename}"


def is_saved_result(path: str) -> bool:
    """True when *path* names a file inside a tool-results directory."""
    parts = str(path).replace("\\", "/").split("/")
    return TOOL_RESULTS_DIR in parts[:-1]


def read_slice(text: str, char_offset: Optional[int], char_limit: Optional[int]) -> str:
    """One page of a saved result, with the offset of the next. A whole read
    would be over the wire cap and be saved AGAIN as a new file (prod: a read
    of a saved result made a second saved result)."""
    total = len(text)
    start = max(0, char_offset or 0)
    if start >= total:
        return (f"[EOF — file has {total} chars; char_offset {start} is past the "
                f"end. Nothing more to read.]")
    end = min(start + min(char_limit or READ_SLICE_CHARS, READ_SLICE_CHARS), total)
    more = (f" — a saved tool result is read {READ_SLICE_CHARS:,} chars at a time; "
            f"continue with char_offset={end}" if end < total
            else " — end of the saved result")
    return f"[chars {start}-{end} of {total}{more}]\n" + text[start:end]


def prune(workspace_dir, *, now: Optional[float] = None,
          max_age_sec: float = MAX_AGE_SEC) -> int:
    """Remove saved results older than ``max_age_sec`` under one workspace and
    the session directories this empties. Returns the number of files removed.
    Never follows a link; a failure on one entry is logged and skipped."""
    root = os.path.join(str(workspace_dir), TOOL_RESULTS_DIR)
    if os.path.islink(root) or not os.path.isdir(root):
        return 0
    cutoff = (time.time() if now is None else now) - max_age_sec
    removed = 0
    try:
        sessions = list(os.scandir(root))
    except OSError as exc:
        logger.warning("tool results: cannot list %s: %s", root, exc)
        return 0
    for sess in sessions:
        if not sess.is_dir(follow_symlinks=False):
            continue
        left = 0
        try:
            for entry in os.scandir(sess.path):
                try:
                    if (entry.is_file(follow_symlinks=False)
                            and entry.stat(follow_symlinks=False).st_mtime < cutoff):
                        os.unlink(entry.path)
                        removed += 1
                    else:
                        left += 1
                except OSError as exc:
                    left += 1
                    logger.warning("tool results: cannot remove %s: %s", entry.path, exc)
            if not left:
                os.rmdir(sess.path)
        except OSError as exc:
            logger.warning("tool results: cannot sweep %s: %s", sess.path, exc)
    return removed
