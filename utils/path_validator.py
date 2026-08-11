"""
Path validation utilities for secure file system operations.

This module provides utilities to validate file paths against allowed
directories and prevent directory traversal attacks. Confinement delegates to
core.path_safety.is_within_root (the repo SSOT) — the previous
``realpath().startswith(root)`` check accepted sibling directories sharing the
root as a string prefix (``/tmp/ws-evil`` passed for root ``/tmp/ws``).
"""

import os
from pathlib import Path
from typing import List, Optional
import logging

from core.path_safety import is_within_root


class PathValidator:
    """Validates file paths against allowed directories."""

    def __init__(self, allowed_paths: Optional[List[str]] = None):
        """
        Initialize the path validator.

        Args:
            allowed_paths: List of allowed directory paths
        """
        self.logger = logging.getLogger(__name__)
        self.allowed_paths = [os.path.abspath(p) for p in (allowed_paths or [])]

    def is_path_allowed(self, path: str, workspace_dir: Optional[str] = None) -> bool:
        """
        Check if a path is allowed based on configured restrictions.

        Args:
            path: The path to validate
            workspace_dir: Optional workspace directory to allow

        Returns:
            True if path is allowed, False otherwise
        """
        try:
            abs_path = os.path.abspath(os.path.expanduser(path))

            # If no allowed paths are configured, only allow workspace directory
            # This is more secure than allowing all paths by default
            if not self.allowed_paths and not workspace_dir:
                self.logger.warning(f"Path validation denied - no allowed paths configured: {path}")
                return False

            roots = list(self.allowed_paths)
            if workspace_dir:
                roots.append(os.path.abspath(workspace_dir))

            return any(is_within_root(abs_path, root) for root in roots)

        except Exception as e:
            self.logger.error(f"Error validating path '{path}': {e}")
            return False


def sanitize_filename(filename: str) -> str:
    """Sanitize filename to prevent path traversal and invalid characters.

    This is a standalone version for use in API endpoints where a PathValidator
    instance may not be available.

    Args:
        filename: Original filename from upload

    Returns:
        Safe filename with only alphanumeric, underscore, dash, dot
    """
    import re

    # Get just the filename, no directory parts
    filename = Path(filename).name

    # Remove any non-alphanumeric except underscore, dash, dot
    safe_name = re.sub(r'[^\w\-\.]', '_', filename)

    # Remove leading/trailing dots and spaces
    safe_name = safe_name.strip('. ')

    # Ensure not empty
    if not safe_name:
        safe_name = "unnamed_file"

    # Limit length
    if len(safe_name) > 200:
        stem = safe_name[:180]
        ext = Path(safe_name).suffix
        safe_name = stem + ext

    return safe_name
