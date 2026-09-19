"""Stale per-session directory GC (056 WS8, 2026-09-19).

`<data>/sessions/<user>/<session-uuid>/` accumulates `data/` (telemetry, history,
llm_usage), `feed/`, `logs/`, `screenshots/` for every session ever run. On the
prod box that was 2,118 dirs / 4.0 GB with 1,642 untouched for more than a week,
because the existing workspace GC (a) first fires after 24 h of uptime — never,
with ~28 restarts a day — and (b) only removes `workspace/`, which under
`POLYROB_PROJECT_DIR` is the SHARED project root and is rightly skipped.

Rules, in order of what they protect:
- only directory names that parse as a UUID are candidates — the shared project
  dir, `_anonymous_`, `local` and anything an operator put there are not;
- a session with ANY file modified inside the TTL is kept (the newest mtime in the
  tree decides, not the top-level dir);
- `protect` (live/bound session ids) are never candidates;
- **dry-run by default**: `apply=False` reports candidates and bytes and removes
  nothing. `SESSION_DIR_GC_APPLY=true` (or apply=True) deletes. The 2026-08-16/17
  "removed 198/202 old workspaces" incident destroyed a week of work; a
  destructive sweep earns its switch by showing its list first.

Pure filesystem + stdlib; no tier imports (core).
"""
from __future__ import annotations

import logging
import os
import shutil
import time
import uuid
from typing import Dict, Iterable, Optional, Set

logger = logging.getLogger(__name__)


def gc_apply_enabled() -> bool:
    from core.env import bool_env
    return bool_env("SESSION_DIR_GC_APPLY", False)


def _is_uuid(name: str) -> bool:
    try:
        uuid.UUID(name)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _newest_mtime_and_size(path: str) -> tuple[float, int]:
    newest = 0.0
    size = 0
    try:
        newest = os.lstat(path).st_mtime
    except OSError:
        pass
    for root, dirs, files in os.walk(path):
        for n in dirs + files:
            p = os.path.join(root, n)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            newest = max(newest, st.st_mtime)
            if not os.path.islink(p) and os.path.isfile(p):
                size += st.st_size
    return newest, size


def collect_stale_sessions(sessions_root: str, *, max_age_days: int = 14,
                           apply: Optional[bool] = None,
                           protect: Optional[Iterable[str]] = None,
                           now: Optional[float] = None) -> Dict[str, object]:
    """Walk ``<sessions_root>/<user>/<uuid>`` and report (or remove) stale sessions."""
    apply = gc_apply_enabled() if apply is None else bool(apply)
    now = time.time() if now is None else now
    cutoff = now - max_age_days * 86400
    prot: Set[str] = {str(x) for x in (protect or ())}
    rep: Dict[str, object] = {"apply": apply, "candidates": 0, "removed": 0, "bytes": 0,
                              "kept_recent": 0, "protected": 0, "errors": 0, "sample": []}
    if not sessions_root or not os.path.isdir(sessions_root):
        rep["reason"] = "sessions root missing"
        return rep
    for user in sorted(os.listdir(sessions_root)):
        udir = os.path.join(sessions_root, user)
        if not os.path.isdir(udir) or os.path.islink(udir):
            continue
        for sid in sorted(os.listdir(udir)):
            sdir = os.path.join(udir, sid)
            if not _is_uuid(sid) or not os.path.isdir(sdir) or os.path.islink(sdir):
                continue
            if sid in prot:
                rep["protected"] = int(rep["protected"]) + 1
                continue
            newest, size = _newest_mtime_and_size(sdir)
            if newest > cutoff:
                rep["kept_recent"] = int(rep["kept_recent"]) + 1
                continue
            rep["candidates"] = int(rep["candidates"]) + 1
            rep["bytes"] = int(rep["bytes"]) + size
            if len(rep["sample"]) < 5:  # type: ignore[arg-type]
                rep["sample"].append(f"{user}/{sid[:8]} {size // 1024} KB")  # type: ignore[attr-defined]
            if apply:
                try:
                    shutil.rmtree(sdir)
                    rep["removed"] = int(rep["removed"]) + 1
                except OSError as e:
                    rep["errors"] = int(rep["errors"]) + 1
                    logger.warning("session gc: could not remove %s: %s", sdir, e)
    return rep
