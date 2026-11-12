"""
Workspace context tracking for agent context enrichment.

This module provides lightweight tracking of workspace changes
to enable agents to see workspace changes. A snapshot diff cannot tell WHO
wrote a file; only files recorded by notify_upload are called uploads.
"""

from pathlib import Path
from typing import Dict, List, Optional, Set
from dataclasses import dataclass, field
from datetime import datetime
import re
import time
import threading
import os
import stat as _stat

from core.doc_kind import is_doc_path, parse_front_matter
from core.walk_skip import prune_dirs
from agents.task.path import pm
from agents.task.agent.session import SessionManager


@dataclass
class FileInfo:
    """Information about a file in the workspace."""
    name: str
    path: str  # Relative to workspace
    size: int
    mtime: float
    age_seconds: float = 0.0

    @property
    def size_kb(self) -> float:
        return self.size / 1024

    @property
    def size_mb(self) -> float:
        return self.size / 1024 / 1024


def _is_noise(rel_path: str) -> bool:
    """Lock / dot / temp files are never worth a harness note (prod: 35 of 52
    notices listed only ``workspace.turn.lock``)."""
    parts = Path(rel_path).parts
    if any(p.startswith('.') for p in parts):
        return True
    name = parts[-1] if parts else rel_path
    return name.endswith(('.lock', '.tmp', '.swp', '.part', '~'))


#: The scan is bounded: a workspace can be a whole project tree (prod: 21,101
#: entries, 1 GB, and the walk held the event loop for up to 26 s).
SCAN_MAX_ENTRIES = 5000
SCAN_MAX_SECONDS = 1.0


def scan_workspace(workspace_dir, *, max_entries: int = SCAN_MAX_ENTRIES,
                   max_seconds: float = SCAN_MAX_SECONDS):
    """``({relative path: FileInfo}, truncated)`` for the files under
    ``workspace_dir``. Dot directories and ``core.walk_skip`` directories are
    pruned BEFORE descent (a new file under one of them is never announced),
    in sorted order so a cut scan stops at the same place each time."""
    root = str(workspace_dir)
    now = time.time()
    deadline = time.monotonic() + max_seconds
    files: Dict[str, FileInfo] = {}
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = prune_dirs(dirnames, dot_dirs=True)
        for name in sorted(filenames):
            seen += 1
            if seen > max_entries or time.monotonic() > deadline:
                return files, True
            full = os.path.join(dirpath, name)
            rel_path = os.path.relpath(full, root)
            if _is_noise(rel_path):
                continue
            try:
                st = os.stat(full)
            except OSError:
                continue
            if not _stat.S_ISREG(st.st_mode):
                continue
            files[rel_path] = FileInfo(name=name, path=rel_path, size=st.st_size,
                                       mtime=st.st_mtime, age_seconds=now - st.st_mtime)
    return files, False


_WRITER_RE = re.compile(r"[^A-Za-z0-9 ._-]")


def _declared_writer(file_path: Path) -> Optional[str]:
    """The ``authored_by:`` a markdown file declares in its front-matter (the
    maintenance loop and operators stamp their drops), or None. Read from the
    first 2 KB only; the value is sanitised and capped — it is file content."""
    if not is_doc_path(str(file_path)):
        return None
    try:
        with open(file_path, 'r', encoding='utf-8', errors='replace') as fh:
            head = fh.read(2048)
    except OSError:
        return None
    writer = parse_front_matter(head)[0].get('authored_by', '')
    writer = _WRITER_RE.sub('', writer).strip()[:40]
    return writer or None


def _size_str(file: FileInfo) -> str:
    return f"{file.size_kb:.1f} KB" if file.size_kb < 1024 else f"{file.size_mb:.1f} MB"


@dataclass
class WorkspaceChanges:
    """Represents changes to workspace since last check."""
    added: List[FileInfo] = field(default_factory=list)
    modified: List[FileInfo] = field(default_factory=list)
    deleted: List[str] = field(default_factory=list)
    # Relative paths (or bare names, for a legacy notify) recorded by the console
    # upload endpoint via notify_upload — the ONLY files we may call "uploaded".
    uploaded: Set[str] = field(default_factory=set)
    # Relative path -> the writer the file itself declares (``authored_by:``).
    declared_by: Dict[str, str] = field(default_factory=dict)
    # The scan hit its entry or time cap: the lists are what the scanned part
    # shows, and no deletion is claimed (a cut scan cannot prove one).
    truncated: bool = False

    def has_changes(self) -> bool:
        return bool(self.added or self.modified or self.deleted)

    def format_for_agent(self, max_files: int = 5) -> str:
        """Format changes as a harness note.

        The writer of a file is NOT known from a snapshot diff: on a shared
        workspace the agent's own writes, scheduled runs and operators all show
        up here. Only a file recorded by notify_upload is called an upload.
        Paths are workspace-relative so the agent can open them.
        """
        if not self.has_changes():
            return ""

        lines = ["[HARNESS NOTE — workspace changes; the writer is unknown unless a line names it]"]

        for file in self.added[:max_files]:
            age = int(file.age_seconds)
            time_str = f"{age}s ago" if age < 60 else f"{age//60}m ago"
            if file.path in self.uploaded:
                lines.append(f"New: {file.path} ({_size_str(file)}, uploaded by the owner via the console {time_str})")
            elif file.path in self.declared_by:
                lines.append(
                    f"New: {file.path} ({_size_str(file)}, appeared {time_str}) — the file declares "
                    f"authored_by: {self.declared_by[file.path]} (NOT the owner)"
                )
            else:
                lines.append(
                    f"New: {file.path} ({_size_str(file)}, appeared {time_str}) — writer not recorded "
                    f"(may be your own write, a scheduled run, or an operator; NOT necessarily the owner)"
                )

        if len(self.added) > max_files:
            lines.append(f"   ... and {len(self.added) - max_files} more files")

        for file in self.modified[:max_files]:
            lines.append(f"Modified: {file.path} (now {_size_str(file)}) — writer not recorded")

        for path in self.deleted[:max_files]:
            lines.append(f"Removed: {path}")

        if self.truncated:
            lines.append("(the workspace is large: the scan was cut, so a change "
                         "deeper in the tree is not listed)")

        return "\n".join(lines)


class WorkspaceContext:
    """
    Lightweight workspace context tracker.

    Tracks recent file uploads and workspace changes for agent context.
    Uses in-memory cache per session to avoid filesystem overhead.
    """

    def __init__(self):
        # Session-scoped cache of workspace state
        # session_id -> {last_check_time, files_snapshot}
        self._session_cache: Dict[str, Dict] = {}
        self._lock = threading.RLock()

        # Recent upload tracking (session_id -> list of FileInfo)
        # Expires after 5 minutes
        self._recent_uploads: Dict[str, List[FileInfo]] = {}
        self._upload_ttl = 300  # 5 minutes

    def notify_upload(self, session_id: str, user_id: str, filename: str, size: int,
                      path: Optional[str] = None):
        """Notify of a file upload (called from upload API).

        ``path`` is the workspace-relative path; without it the bare filename is
        recorded (matches a file at the workspace root only)."""
        with self._lock:
            if session_id not in self._recent_uploads:
                self._recent_uploads[session_id] = []

            file_info = FileInfo(
                name=filename,
                path=path or filename,
                size=size,
                mtime=time.time(),
                age_seconds=0
            )

            self._recent_uploads[session_id].append(file_info)

            # Clean old uploads (> TTL)
            now = time.time()
            self._recent_uploads[session_id] = [
                f for f in self._recent_uploads[session_id]
                if (now - f.mtime) < self._upload_ttl
            ]

    def get_recent_uploads(self, session_id: str, max_age_seconds: int = 300) -> List[FileInfo]:
        """Get recently uploaded files (within last 5 minutes)."""
        with self._lock:
            uploads = self._recent_uploads.get(session_id, [])
            now = time.time()

            recent = []
            for file_info in uploads:
                age = now - file_info.mtime
                if age < max_age_seconds:
                    file_info.age_seconds = age
                    recent.append(file_info)

            return recent

    def get_workspace_changes(
        self,
        session_id: str,
        user_id: str,
        since_last_check: bool = True
    ) -> WorkspaceChanges:
        """
        Get workspace changes since last check.

        Args:
            session_id: Session ID
            user_id: User ID
            since_last_check: If True, compare with last snapshot

        Returns:
            WorkspaceChanges object with added/modified/deleted files
        """
        with self._lock:
            # Get workspace directory
            workspace_dir = pm().get_workspace_dir(session_id, user_id)
            if not workspace_dir.exists():
                return WorkspaceChanges()

            now = time.time()
            current_files, truncated = scan_workspace(workspace_dir)

            # Get previous snapshot
            cache = self._session_cache.get(session_id, {})
            previous_files = cache.get('files_snapshot', {})
            # A cut scan (now or last time) does not see the same part of the
            # tree twice for certain: a file is "new" only when it was also
            # written after the last check, and nothing is called removed.
            partial = truncated or bool(cache.get('truncated'))
            last_check = float(cache.get('last_check_time') or 0.0)

            # Calculate changes
            changes = WorkspaceChanges(
                uploaded={f.path for f in self.get_recent_uploads(session_id)},
                truncated=truncated,
            )

            if since_last_check and previous_files:
                # Added files
                for path, file_info in current_files.items():
                    if path not in previous_files:
                        if partial and file_info.mtime < last_check:
                            continue
                        changes.added.append(file_info)
                        writer = _declared_writer(workspace_dir / path)
                        if writer:
                            changes.declared_by[path] = writer
                    elif file_info.mtime > previous_files[path].mtime:
                        changes.modified.append(file_info)

                # Deleted files
                for path in ([] if partial else previous_files):
                    if path not in current_files:
                        changes.deleted.append(path)
            else:
                # First check - all files are "existing" (not new)
                # Only show files uploaded in last 5 minutes as "new"
                for file_info in current_files.values():
                    if file_info.path in changes.uploaded:
                        changes.added.append(file_info)

            # Update cache
            if partial:
                # Keep what an earlier scan saw: a later full scan must not
                # announce those files as new.
                current_files = {**previous_files, **current_files}
            self._session_cache[session_id] = {
                'last_check_time': now,
                'files_snapshot': current_files,
                'truncated': truncated,
            }

            return changes

    def clear_session(self, session_id: str):
        """Clear cached data for a session (on session end)."""
        with self._lock:
            self._session_cache.pop(session_id, None)
            self._recent_uploads.pop(session_id, None)


# Global singleton instance
_workspace_context = None
_lock = threading.Lock()


def get_workspace_context() -> WorkspaceContext:
    """Get global workspace context singleton."""
    global _workspace_context
    if _workspace_context is None:
        with _lock:
            if _workspace_context is None:
                _workspace_context = WorkspaceContext()
    return _workspace_context
