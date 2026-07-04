"""
Workspace context tracking for agent context enrichment.

This module provides lightweight tracking of workspace changes
to enable agents to understand file uploads and modifications.
"""

from pathlib import Path
from typing import Dict, List, Optional, Set
from dataclasses import dataclass, field
from datetime import datetime
import time
import threading
import os

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


@dataclass
class WorkspaceChanges:
    """Represents changes to workspace since last check."""
    added: List[FileInfo] = field(default_factory=list)
    modified: List[FileInfo] = field(default_factory=list)
    deleted: List[str] = field(default_factory=list)

    def has_changes(self) -> bool:
        return bool(self.added or self.modified or self.deleted)

    def format_for_agent(self, max_files: int = 5) -> str:
        """Format changes as agent-readable context."""
        if not self.has_changes():
            return ""

        lines = ["[WORKSPACE CHANGES DETECTED]"]

        # Show added files (most important)
        for file in self.added[:max_files]:
            age = int(file.age_seconds)
            size_str = f"{file.size_kb:.1f} KB" if file.size_kb < 1024 else f"{file.size_mb:.1f} MB"
            time_str = f"{age}s ago" if age < 60 else f"{age//60}m ago"
            lines.append(f"✨ New: {file.name} ({size_str}, uploaded {time_str})")

        if len(self.added) > max_files:
            lines.append(f"   ... and {len(self.added) - max_files} more files")

        # Show modified files
        for file in self.modified[:max_files]:
            size_str = f"{file.size_kb:.1f} KB" if file.size_kb < 1024 else f"{file.size_mb:.1f} MB"
            lines.append(f"📝 Modified: {file.name} (now {size_str})")

        # Show deleted files
        for file_name in self.deleted[:max_files]:
            lines.append(f"🗑️  Removed: {file_name}")

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

    def notify_upload(self, session_id: str, user_id: str, filename: str, size: int):
        """Notify of a file upload (called from upload API)."""
        with self._lock:
            if session_id not in self._recent_uploads:
                self._recent_uploads[session_id] = []

            file_info = FileInfo(
                name=filename,
                path=filename,
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

            # Get current files
            current_files = {}
            now = time.time()

            for file_path in workspace_dir.rglob('*'):
                if file_path.is_file():
                    rel_path = str(file_path.relative_to(workspace_dir))
                    stat = file_path.stat()
                    current_files[rel_path] = FileInfo(
                        name=file_path.name,
                        path=rel_path,
                        size=stat.st_size,
                        mtime=stat.st_mtime,
                        age_seconds=now - stat.st_mtime
                    )

            # Get previous snapshot
            cache = self._session_cache.get(session_id, {})
            previous_files = cache.get('files_snapshot', {})

            # Calculate changes
            changes = WorkspaceChanges()

            if since_last_check and previous_files:
                # Added files
                for path, file_info in current_files.items():
                    if path not in previous_files:
                        changes.added.append(file_info)
                    elif file_info.mtime > previous_files[path].mtime:
                        changes.modified.append(file_info)

                # Deleted files
                for path in previous_files:
                    if path not in current_files:
                        changes.deleted.append(previous_files[path].name)
            else:
                # First check - all files are "existing" (not new)
                # Only show files uploaded in last 5 minutes as "new"
                recent_uploads = self.get_recent_uploads(session_id)
                recent_names = {f.name for f in recent_uploads}

                for file_info in current_files.values():
                    if file_info.name in recent_names:
                        changes.added.append(file_info)

            # Update cache
            self._session_cache[session_id] = {
                'last_check_time': now,
                'files_snapshot': current_files
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
