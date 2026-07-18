from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from agents.task.path import pm


class WorkspaceMixin:
    """Workspace directory accessors for SessionOrchestrator.

    The sync ``workspace_dir`` property is the canonical accessor. (The old
    async ``get_workspace_dir()`` shim was deprecation-warned through 2026-07
    and deleted with zero production callers — F-3e; note the name collision
    with the unrelated ``PathManager.get_workspace_dir(session_id, user_id)``,
    which is live and takes arguments.)
    """

    @property
    def workspace_dir(self) -> str:
        """Get the workspace directory for this session.

        Returns:
            Path to the workspace directory as a string
        """
        return self._workspace_dir if hasattr(self, '_workspace_dir') else None

    def get_subdirectory(self, subdir_name: str) -> Optional[Path]:
        """Get a subdirectory within the session directory.

        Args:
            subdir_name: Name of the subdirectory

        Returns:
            Path to the subdirectory or None if error
        """
        try:
            if self.session_manager:
                return self.session_manager.get_subdirectory(self.session_id, subdir_name, self.user_id)
            else:
                # Fallback using path manager directly
                from agents.task.path import pm
                return pm().get_subdir(self.session_id, subdir_name, self.user_id)
        except Exception as e:
            self.logger.error(f"Failed to get subdirectory {subdir_name}: {e}")
            return None

    def get_workspace_files(self, limit: int = 10) -> List[Path]:
        """Get list of workspace files.

        Used by ChatHITL to generate completion summaries.

        Args:
            limit: Maximum number of files to return

        Returns:
            List of file paths in the workspace
        """
        try:
            workspace_dir = pm().get_workspace_dir(self.session_id, self.user_id)
            if not workspace_dir.exists():
                return []

            # Get all files (not directories)
            files = [f for f in workspace_dir.rglob('*') if f.is_file()]

            # Sort by modification time (newest first)
            files.sort(key=lambda f: f.stat().st_mtime, reverse=True)

            # Return limited list
            return files[:limit]
        except Exception as e:
            self.logger.debug(f"Error getting workspace files: {e}")
            return []
