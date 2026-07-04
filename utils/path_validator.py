"""
Path validation utilities for secure file system operations.

This module provides utilities to validate file paths against allowed
directories and prevent directory traversal attacks.
"""

import os
from pathlib import Path
from typing import List, Optional
import logging


class PathValidator:
    """Validates file paths against allowed directories."""

    def __init__(self, allowed_paths: Optional[List[str]] = None):
        """
        Initialize the path validator.

        Args:
            allowed_paths: List of allowed directory paths
        """
        self.logger = logging.getLogger(__name__)
        self.allowed_paths = allowed_paths or []
        # Normalize allowed paths
        self.allowed_paths = [os.path.abspath(p) for p in self.allowed_paths]

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
            # Get absolute path and resolve any symlinks
            abs_path = os.path.abspath(os.path.expanduser(path))
            real_path = os.path.realpath(abs_path)

            # If no allowed paths are configured, only allow workspace directory
            # This is more secure than allowing all paths by default
            if not self.allowed_paths:
                if workspace_dir:
                    workspace_abs = os.path.abspath(workspace_dir)
                    return real_path.startswith(workspace_abs)
                else:
                    # No allowed paths and no workspace - deny by default for security
                    self.logger.warning(f"Path validation denied - no allowed paths configured: {path}")
                    return False

            # Check workspace directory if provided
            if workspace_dir:
                workspace_abs = os.path.abspath(workspace_dir)
                if real_path.startswith(workspace_abs):
                    return True

            # Check against allowed paths
            for allowed in self.allowed_paths:
                allowed_abs = os.path.abspath(allowed)
                # Check if path is within allowed directory
                if real_path.startswith(allowed_abs):
                    return True
                # Check if path exactly matches allowed path
                if real_path == allowed_abs:
                    return True

            return False

        except Exception as e:
            self.logger.error(f"Error validating path '{path}': {e}")
            return False

    def validate_path(self, path: str, workspace_dir: Optional[str] = None) -> str:
        """
        Validate and return the normalized path.

        Args:
            path: The path to validate
            workspace_dir: Optional workspace directory to allow

        Returns:
            Normalized absolute path

        Raises:
            ValueError: If path is not allowed
        """
        if not self.is_path_allowed(path, workspace_dir):
            raise ValueError(f"Path '{path}' is not within allowed directories")

        return os.path.abspath(os.path.expanduser(path))

    def add_allowed_path(self, path: str) -> None:
        """
        Add a path to the allowed list.

        Args:
            path: Path to add to allowed list
        """
        abs_path = os.path.abspath(path)
        if abs_path not in self.allowed_paths:
            self.allowed_paths.append(abs_path)
            self.logger.debug(f"Added '{abs_path}' to allowed paths")

    def remove_allowed_path(self, path: str) -> None:
        """
        Remove a path from the allowed list.

        Args:
            path: Path to remove from allowed list
        """
        abs_path = os.path.abspath(path)
        if abs_path in self.allowed_paths:
            self.allowed_paths.remove(abs_path)
            self.logger.debug(f"Removed '{abs_path}' from allowed paths")

    def is_safe_filename(self, filename: str) -> bool:
        """
        Check if a filename is safe (no directory traversal).

        Args:
            filename: The filename to check

        Returns:
            True if filename is safe, False otherwise
        """
        # Check for directory traversal patterns
        dangerous_patterns = ['..', '~', '/', '\\']
        for pattern in dangerous_patterns:
            if pattern in filename:
                return False

        # Check for absolute paths
        if os.path.isabs(filename):
            return False

        return True

    def sanitize_filename(self, filename: str) -> str:
        """
        Sanitize a filename by removing dangerous characters.

        Args:
            filename: The filename to sanitize

        Returns:
            Sanitized filename
        """
        # Remove directory separators and traversal patterns
        sanitized = filename.replace('/', '_')
        sanitized = sanitized.replace('\\', '_')
        sanitized = sanitized.replace('..', '_')
        sanitized = sanitized.replace('~', '_')

        # Remove leading dots (hidden files)
        while sanitized.startswith('.'):
            sanitized = sanitized[1:]

        # Ensure we have a valid filename
        if not sanitized:
            sanitized = 'unnamed'

        return sanitized


# Global instance for convenience
_global_validator = None


def get_path_validator(allowed_paths: Optional[List[str]] = None) -> PathValidator:
    """
    Get or create a global path validator instance.

    Args:
        allowed_paths: Optional list of allowed paths

    Returns:
        PathValidator instance
    """
    global _global_validator
    if _global_validator is None:
        _global_validator = PathValidator(allowed_paths)
    elif allowed_paths:
        # Update allowed paths if provided
        for path in allowed_paths:
            _global_validator.add_allowed_path(path)
    return _global_validator


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