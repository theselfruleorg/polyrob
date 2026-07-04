"""
Centralized path management for Auto v2.

This module provides the single, canonical source of truth for all path operations
in the Auto v2 system. All path management, normalization, cleaning, etc. should
go through this module's PathManager instance.

Usage:
    from agents.task.path import pm
    
    # Clean a session ID
    clean_id = pm().clean_session_id("agent_123-456")
    
    # Get workspace directory
    workspace = pm().get_workspace_dir(session_id)
    
    # Create a file path
    file_path = pm().create_file_path(session_id, "workspace", "data.json")
"""

import os
import re
import logging
from pathlib import Path
from typing import Optional, Callable, Any, Dict
import threading
from functools import lru_cache

logger = logging.getLogger("task.path")

# Singleton instance
_INSTANCE = None
_LOCK = threading.RLock()

# Central registry for all singletons
_SINGLETON_REGISTRY = {}
_REGISTRY_LOCK = threading.RLock()

def get_safe_singleton(factory_func: Callable[..., Any]) -> Callable[..., Any]:
    """
    Generic thread-safe singleton factory to replace multiple implementations.
    
    This function should be used for all singleton implementations across the codebase.
    It provides thread-safe access to singleton instances and prevents race conditions
    when multiple threads attempt to initialize the same singleton.
    
    Args:
        factory_func: Function that creates the instance when called
        
    Returns:
        A function that returns the singleton instance of the specified factory
    """
    def get_instance(*args, **kwargs):
        # Use the function's fully qualified name as the key
        key = f"{factory_func.__module__}.{factory_func.__name__}"
        
        with _REGISTRY_LOCK:
            if key not in _SINGLETON_REGISTRY:
                _SINGLETON_REGISTRY[key] = factory_func(*args, **kwargs)
            
            return _SINGLETON_REGISTRY[key]
    
    return get_instance

def pm() -> 'PathManager':
    """
    Get the singleton PathManager instance.

    Returns:
        The PathManager instance
    """
    global _INSTANCE
    with _LOCK:
        if _INSTANCE is None:
            _INSTANCE = PathManager()
        return _INSTANCE


def set_path_manager(instance: 'PathManager') -> None:
    """Install a configured PathManager as the process-global pm() singleton.

    Lets the composition root (build_cli_container) point ALL pm() call sites at
    one project-scoped manager without an env-var shim — pm() and the container's
    'path_manager' service become the same instance (single source of truth).
    Overrides any instance already created (pm() is lazy, so an earlier call must
    not pin the default root for the rest of the process).
    """
    global _INSTANCE
    with _LOCK:
        _INSTANCE = instance


def reset_path_manager() -> None:
    """Drop the installed pm() singleton so the next pm() rebuilds the default.

    For test isolation (an autouse conftest fixture calls this around every test)
    and for any caller that needs to un-install a project-scoped manager set via
    set_path_manager (e.g. build_cli_container in-process). Idempotent.
    """
    global _INSTANCE
    with _LOCK:
        _INSTANCE = None


def get_path_manager(data_root: Optional[str] = None,
                     workspace_is_project_root: bool = False,
                     project_root: Optional[str] = None) -> 'PathManager':
    """Construct a PathManager with an explicit root (for container injection).

    Unlike pm() (the process-global default), this returns a fresh, configured
    instance — the seam that lets local mode use cwd/.polyrob without a mode flag.
    """
    return PathManager(data_root=data_root,
                       workspace_is_project_root=workspace_is_project_root,
                       project_root=project_root)

class PathManager:
    """
    Central path management system for Auto V2.
    
    This class is responsible for:
    1. Standardizing paths across the system
    2. Ensuring proper user and session isolation
    3. Providing a single interface for path operations
    """
    
    __slots__ = ('data_root', '_user_session_cache', '_cache_lock', 'logger', '_cache_access_order', '_max_cache_size', '_workspace_is_project_root', '_project_root')
    
    
    def __init__(self, data_root: Optional[str] = None,
                 workspace_is_project_root: bool = False,
                 project_root: Optional[str] = None):
        """Initialize PathManager with data root directory.

        Args:
            data_root: Root directory for all data. Defaults to environment variable or ./data
            workspace_is_project_root: If True, get_workspace_dir returns project_root directly.
            project_root: The project folder used as workspace when workspace_is_project_root=True.
        """
        # Initialize logger first
        self.logger = logging.getLogger('task.path')
        
        # Determine data root
        if data_root:
            self.data_root = Path(data_root)
        else:
            # Get from environment or use default
            # Default to data/task for task agent sessions (NEW structure)
            data_root_env = os.getenv('DATA_ROOT', './data/task')
            self.data_root = Path(data_root_env)
        
        # Ensure data root exists
        self.data_root = self.data_root.resolve()
        os.makedirs(self.data_root, exist_ok=True)

        # Local "launch in a folder" mode: workspace IS the project folder.
        self._workspace_is_project_root = workspace_is_project_root
        self._project_root = Path(project_root).resolve() if project_root else None

        # Initialize thread-safe caches with automatic eviction using LRU
        try:
            from cachetools import LRUCache
            self._user_session_cache = LRUCache(maxsize=512)  # LRU cache for automatic eviction
            # Actions now managed by ToolManager - no cache needed
            # Set fallback attributes to None when using cachetools
            self._cache_access_order = None
            self._max_cache_size = None
        except ImportError:
            # Fallback to manual LRU implementation if cachetools not available
            self._user_session_cache = {}
            # Actions now managed by ToolManager - no cache needed
            self._cache_access_order = []  # For manual LRU tracking
            self._max_cache_size = 512
        
        # Thread safety for cache operations
        self._cache_lock = threading.RLock()
        
        self.logger.debug(f"PathManager initialized with data_root: {self.data_root}")
    
    def clean_session_id(self, session_id: str) -> str:
        """Clean and standardize a session ID with strict security validation.
        
        This method implements strict whitelisting to prevent path traversal attacks
        and other security vulnerabilities. Only alphanumeric characters, hyphens,
        and underscores are allowed.
        
        Args:
            session_id: The session ID to clean
            
        Returns:
            Cleaned session ID
            
        Raises:
            ValueError: If the session ID contains dangerous characters or patterns
        """
        if not session_id:
            return ""
            
        # Convert to string if it's not already
        session_id = str(session_id)
        original_id = session_id
        
        # SECURITY CHECK: Reject dangerous patterns before any processing
        dangerous_patterns = [
            '..',  # Directory traversal
            './',  # Current directory reference
            '~',   # Home directory expansion
            '\x00',  # Null byte injection
            '\n', '\r',  # Newline injection
            '|', '&', ';', '$', '`',  # Command injection
            '<', '>',  # Redirection
            '*', '?',  # Glob patterns
            '[', ']',  # Glob brackets
        ]
        
        for pattern in dangerous_patterns:
            if pattern in session_id:
                raise ValueError(f"Security violation: Session ID contains dangerous character/pattern '{pattern}': {session_id}")
        
        # Check for URL encoding attempts
        if '%' in session_id:
            # Could be URL encoded dangerous chars
            import urllib.parse
            try:
                decoded = urllib.parse.unquote(session_id)
                if decoded != session_id:
                    # URL encoding detected - check decoded version
                    for pattern in dangerous_patterns:
                        if pattern in decoded:
                            raise ValueError(f"Security violation: URL-encoded dangerous pattern detected: {session_id}")
            except Exception:
                # If we can't decode, reject it
                raise ValueError(f"Security violation: Invalid URL encoding in session ID: {session_id}")
        
        # FIXED: Enhanced session ID cleaning with better prefix detection
        # Remove common agent role prefixes but preserve legitimate session IDs
        prefixes_to_check = [
            'planner_', 'executor_', 'evaluator_', 'orchestrator_',
            'agent_executor_', 'agent_planner_', 'agent_evaluator_'
        ]
        
        # Check for agent prefixes
        for prefix in prefixes_to_check:
            if session_id.startswith(prefix):
                # Extract the actual session ID after the prefix
                potential_session_id = session_id[len(prefix):]
                
                # Validate that what remains looks like a valid session ID
                # (UUIDs, alphanumeric strings, etc.)
                if self._is_valid_session_id_format(potential_session_id):
                    session_id = potential_session_id
                    self.logger.debug(f"Cleaned session ID: {original_id} -> {session_id}")
                    break
        
        # STRICT WHITELIST: Only allow alphanumeric, dash, and underscore
        import re
        clean_id = re.sub(r'[^a-zA-Z0-9_\-]', '', session_id)
        
        # Additional cleanup: remove any double underscores that might result from cleaning
        clean_id = re.sub(r'_{2,}', '_', clean_id)
        clean_id = re.sub(r'-{2,}', '-', clean_id)
        
        # Remove leading/trailing separators
        clean_id = clean_id.strip('_-')
        
        # Ensure it's not too long (prevent DoS via long IDs)
        MAX_ID_LENGTH = 50  # Reduced from 128 for security
        if len(clean_id) > MAX_ID_LENGTH:
            clean_id = clean_id[:MAX_ID_LENGTH]
        
        # Ensure the cleaned ID is still valid
        if not clean_id or len(clean_id) < 3:
            # Generate a safe fallback ID
            import hashlib
            safe_hash = hashlib.sha256(original_id.encode()).hexdigest()[:12]
            clean_id = f"session_{safe_hash}"
            self.logger.warning(f"Generated safe session ID from invalid input: {original_id} -> {clean_id}")
            
        return clean_id
    
    def _is_valid_session_id_format(self, session_id: str) -> bool:
        """Check if a session ID has a valid format.
        
        Args:
            session_id: Session ID to validate
            
        Returns:
            True if the format looks like a valid session ID
        """
        if not session_id or len(session_id) < 3:
            return False
            
        # Check for UUID format (with or without hyphens)
        import re
        uuid_pattern = r'^[a-f0-9]{8}-?[a-f0-9]{4}-?[a-f0-9]{4}-?[a-f0-9]{4}-?[a-f0-9]{12}$'
        if re.match(uuid_pattern, session_id, re.IGNORECASE):
            return True
            
        # Check for alphanumeric format (common for session IDs)
        if re.match(r'^[a-zA-Z0-9_-]{3,64}$', session_id):
            return True
            
        # Check for timestamp-based IDs
        if re.match(r'^\d{8,}[_-]?[a-zA-Z0-9]*$', session_id):
            return True
            
        return False
    
    def clean_user_id(self, user_id: str) -> str:
        """Clean and sanitize a user ID while preserving important characters.

        Unlike clean_session_id(), this method preserves underscores and other
        characters that are meaningful in user identifiers (wallet addresses, etc).

        Args:
            user_id: The user ID to clean

        Returns:
            Cleaned user ID

        Raises:
            ValueError: If the user ID contains dangerous characters
        """
        if not user_id:
            return ""

        user_id = str(user_id)
        original_id = user_id

        # SECURITY CHECK: Reject dangerous patterns
        dangerous_patterns = [
            '..',  # Directory traversal
            './',  # Current directory reference
            '~',   # Home directory expansion
            '\x00',  # Null byte injection
            '\n', '\r',  # Newline injection
            '|', '&', ';', '$', '`',  # Command injection
            '<', '>',  # Redirection
            '*', '?',  # Glob patterns
            '[', ']',  # Glob brackets
        ]

        for pattern in dangerous_patterns:
            if pattern in user_id:
                raise ValueError(f"Security violation: User ID contains dangerous character/pattern '{pattern}': {user_id}")

        # WHITELIST: Allow alphanumeric, underscore, dash, and period (for wallet addresses)
        # Also allow 'x' prefix for hex addresses (0x...)
        import re
        clean_id = re.sub(r'[^a-zA-Z0-9_\-\.]', '', user_id)

        # Remove any double separators
        clean_id = re.sub(r'_{2,}', '_', clean_id)
        clean_id = re.sub(r'-{2,}', '-', clean_id)
        clean_id = re.sub(r'\.{2,}', '.', clean_id)

        # DON'T strip underscores/dashes - they're meaningful in user IDs
        # (wallet addresses often have prefixes like _anonymous_)

        # Ensure it's not too long
        MAX_USER_ID_LENGTH = 128  # Longer than session IDs for wallet addresses
        if len(clean_id) > MAX_USER_ID_LENGTH:
            clean_id = clean_id[:MAX_USER_ID_LENGTH]

        # Ensure the cleaned ID is still valid
        if not clean_id or len(clean_id) < 1:
            import hashlib
            safe_hash = hashlib.sha256(original_id.encode()).hexdigest()[:12]
            clean_id = f"user_{safe_hash}"
            self.logger.warning(f"Generated safe user ID from invalid input: {original_id} -> {clean_id}")

        return clean_id

    def get_user_root(self, user_id: Optional[str] = None) -> Path:
        """
        Get the root directory for a user.

        Args:
            user_id: Optional user ID, defaults to anonymous

        Returns:
            Path to the user's root directory
        """
        # Use default user ID if none provided
        if not user_id:
            import traceback
            # Get the calling function for debugging
            stack = traceback.extract_stack()
            caller_info = stack[-2] if len(stack) >= 2 else "unknown"
            self.logger.warning(f"Using anonymous user directory - user_id was: {repr(user_id)}. "
                              f"Called from: {caller_info.filename}:{caller_info.lineno} in {caller_info.name}")
            from agents.task.constants import DEFAULT_USER_ID
            user_id = DEFAULT_USER_ID

        # Clean the user ID (NOT session ID - different rules!)
        clean_user_id = self.clean_user_id(user_id)

        # Create the path and resolve it
        user_root = (self.data_root / clean_user_id).resolve(strict=False)

        return user_root
    
    def get_session_user(self, session_id: str) -> Optional[str]:
        """Get the user ID associated with a session.
        
        Public wrapper for _discover_user_for_session.
        
        Args:
            session_id: The session ID
            
        Returns:
            The user ID if found, None otherwise
        """
        return self._discover_user_for_session(session_id)

    def _discover_user_for_session(self, session_id: str) -> Optional[str]:
        """Discover which user a session belongs to.

        This method searches all user directories for the session and reads the
        metadata to get the accurate user_id. This is important for backward
        compatibility where directory names might not match metadata user_ids.

        MIGRATION SUPPORT: Also checks legacy paths (data/auto/) during transition.

        Args:
            session_id: The session ID to find

        Returns:
            The user ID from metadata, or None if not found
        """
        # CRITICAL FIX: Always clean the session ID first
        clean_id = self.clean_session_id(session_id)

        # Check cache first under lock
        with self._cache_lock:
            if clean_id in self._user_session_cache:
                self.logger.debug(f"Using cached user for session {clean_id}: {self._user_session_cache[clean_id]}")
                return self._user_session_cache[clean_id]

        # Not in cache, perform the expensive lookup
        # Note: clean_id is already cleaned

        # PRIORITY 1: Search new structure (data/task/{user}/{session})
        # IMPORTANT: Check non-anonymous users first (skip _anonymous_ in main loop)
        for user_dir in self.data_root.glob('*'):
            # Skip non-directories, hidden directories, AND _anonymous_ (check later as fallback)
            if not user_dir.is_dir() or user_dir.name.startswith('.') or user_dir.name == '_anonymous_':
                continue

            # Check direct session path (NEW structure)
            session_dir = user_dir / clean_id
            if session_dir.exists() and session_dir.is_dir():
                # CRITICAL: Only use this session if it has valid metadata
                user_id = self._read_user_from_metadata(session_dir)
                if user_id:  # Only return if metadata exists and is valid
                    self.logger.debug(f"Found session {clean_id} in NEW structure with user_id: {user_id}")

                    # Cache the result under lock
                    with self._cache_lock:
                        self._user_session_cache[clean_id] = user_id

                    return user_id
                else:
                    # Directory exists but no valid metadata - skip it
                    self.logger.debug(f"Skipping {session_dir} - no valid metadata")

            # Check old sessions subdirectory (OLD structure - backward compat)
            user_sessions_dir = user_dir / 'sessions'
            if user_sessions_dir.exists() and user_sessions_dir.is_dir():
                session_dir = user_sessions_dir / clean_id
                if session_dir.exists() and session_dir.is_dir():
                    # CRITICAL: Only use this session if it has valid metadata
                    user_id = self._read_user_from_metadata(session_dir)
                    if user_id:  # Only return if metadata exists and is valid
                        self.logger.info(f"Found session {clean_id} in OLD structure (sessions subdir) - migration recommended")

                        # Cache the result under lock
                        with self._cache_lock:
                            self._user_session_cache[clean_id] = user_id

                        return user_id
                    else:
                        self.logger.debug(f"Skipping {session_dir} - no valid metadata")

        # DO NOT fallback to _anonymous_ - if we can't find the real owner, fail explicitly
        # This prevents sessions from being incorrectly associated with _anonymous_
        # when they actually belong to a real user

        # PRIORITY 2: Check legacy root (data/auto/) if we're in new structure (data/task/)
        if self.data_root.name == 'task':
            legacy_root = self.data_root.parent / 'auto'
            if legacy_root.exists():
                self.logger.debug(f"Checking legacy root for session {clean_id}: {legacy_root}")
                for user_dir in legacy_root.glob('*'):
                    if not user_dir.is_dir() or user_dir.name.startswith('.'):
                        continue

                    # Check sessions subdirectory in legacy structure
                    sessions_dir = user_dir / 'sessions'
                    if not sessions_dir.exists():
                        continue

                    session_dir = sessions_dir / clean_id
                    if session_dir.exists() and session_dir.is_dir():
                        user_id = self._read_user_from_metadata(session_dir) or user_dir.name
                        self.logger.info(f"Found session {clean_id} in LEGACY location (data/auto/) - migration needed")

                        # Cache the result under lock
                        with self._cache_lock:
                            self._user_session_cache[clean_id] = user_id

                        return user_id

        # PRIORITY 3: Check wrong location (data/_anonymous_/)
        if self.data_root.parent.exists():
            wrong_anonymous = self.data_root.parent / '_anonymous_'
            if wrong_anonymous.exists():
                sessions_dir = wrong_anonymous / 'sessions'
                if sessions_dir.exists():
                    session_dir = sessions_dir / clean_id
                    if session_dir.exists() and session_dir.is_dir():
                        user_id = '_anonymous_'
                        self.logger.info(f"Found session {clean_id} in WRONG location (data/_anonymous_/) - migration needed")

                        # Cache the result under lock
                        with self._cache_lock:
                            self._user_session_cache[clean_id] = user_id

                        return user_id

            # Also check legacy "anonymous" naming
            legacy_anonymous = self.data_root.parent / 'anonymous'
            if legacy_anonymous.exists():
                sessions_dir = legacy_anonymous / 'sessions'
                if sessions_dir.exists():
                    session_dir = sessions_dir / clean_id
                    if session_dir.exists() and session_dir.is_dir():
                        user_id = '_anonymous_'
                        self.logger.info(f"Found session {clean_id} in LEGACY location (data/anonymous/) - migration needed")

                        # Cache the result under lock
                        with self._cache_lock:
                            self._user_session_cache[clean_id] = user_id

                        return user_id

        # Not found, cache the negative result to avoid repeated lookups
        with self._cache_lock:
            self._user_session_cache[clean_id] = None

        return None

    def _read_user_from_metadata(self, session_dir: Path) -> Optional[str]:
        """Read user_id from session metadata.

        Args:
            session_dir: Path to session directory

        Returns:
            User ID from metadata, or None if not found
        """
        metadata_file = session_dir / 'metadata.json'
        if metadata_file.exists():
            try:
                import json
                with metadata_file.open('r') as f:
                    metadata = json.load(f)
                    return metadata.get('user_id')
            except Exception as e:
                self.logger.warning(f"Failed to read metadata from {metadata_file}: {e}")
        return None

    def get_session_root(self, session_id: str, user_id: Optional[str] = None) -> Path:
        """Get the root directory for a session.

        Args:
            session_id: The session ID
            user_id: Optional user ID, will be auto-detected if None

        Returns:
            The session root directory path
        """
        # CRITICAL FIX: Always clean the session ID first, before any operations
        clean_id = self.clean_session_id(session_id)

        # If user ID is not provided, try to discover it from the session
        if user_id is None:
            user_id = self._discover_user_for_session(clean_id)

        # If still no user ID, retry with delay (handles race conditions during session creation)
        if user_id is None:
            import time
            self.logger.debug(f"First discovery attempt failed for {clean_id}, retrying with delays...")

            for attempt in range(3):
                time.sleep(0.1)  # 100ms delay
                user_id = self._discover_user_for_session(clean_id)
                if user_id:
                    self.logger.debug(f"Discovery succeeded on attempt {attempt + 2} for {clean_id}")
                    break

        # If STILL no user ID, check if session exists ANYWHERE before falling back
        if user_id is None:
            # Check if session exists in any user directory
            session_exists = False
            for user_dir in self.data_root.glob('*'):
                if not user_dir.is_dir() or user_dir.name.startswith('.'):
                    continue
                # Check direct path (new structure)
                if (user_dir / clean_id).exists():
                    session_exists = True
                    break
                # Check old structure with sessions/ subdir
                if (user_dir / 'sessions' / clean_id).exists():
                    session_exists = True
                    break

            if session_exists:
                # Session exists but we can't determine user - CRITICAL ERROR
                import traceback
                stack = traceback.extract_stack()
                caller_info = stack[-2] if len(stack) >= 2 else "unknown"
                error_msg = (
                    f"CRITICAL: Session {clean_id} exists but user_id cannot be determined. "
                    f"This indicates a path isolation bug. Please provide user_id explicitly. "
                    f"Called from: {caller_info.filename}:{caller_info.lineno} in {caller_info.name}"
                )
                self.logger.error(error_msg)
                raise RuntimeError(error_msg)
            else:
                # Session doesn't exist - fallback to default user is OK
                import traceback
                stack = traceback.extract_stack()
                caller_info = stack[-2] if len(stack) >= 2 else "unknown"
                self.logger.info(f"Session {clean_id} not found, using default user. "
                              f"Called from: {caller_info.filename}:{caller_info.lineno} in {caller_info.name}")
                from agents.task.constants import DEFAULT_USER_ID
                user_id = DEFAULT_USER_ID
        
        # Get the user root directory
        user_root = self.get_user_root(user_id)

        # Create the session path (NEW: Direct under user, no "sessions" subdirectory)
        session_root = user_root / clean_id

        # Ensure directory exists
        self.ensure_directory_exists(session_root)
        
        # If we found this session, update the cache
        with self._cache_lock:
            self._user_session_cache[clean_id] = user_id
        
        return session_root
    
    def get_subdir(self, session_id: str, subdir_name: str, user_id: Optional[str] = None) -> Path:
        """
        Get a subdirectory within a session.
        
        Args:
            session_id: The session ID
            subdir_name: Name of the subdirectory
            user_id: Optional user ID, defaults to anonymous
            
        Returns:
            Path to the subdirectory
        """
        # Get session root
        session_root = self.get_session_root(session_id, user_id)
        
        # Create the subdirectory path
        subdir = (session_root / subdir_name).resolve(strict=False)
        
        # Ensure directory exists
        os.makedirs(subdir, exist_ok=True)
        
        return subdir
    
    @property
    def is_project_root_workspace(self) -> bool:
        """True iff this manager serves ONE shared project folder as the workspace.

        Mirrors the guard in get_workspace_dir (both flag AND a concrete project_root).
        The goal-concurrency clamp reads THIS (the installed pm()), never an env var,
        so the multi-tenant server — whose global pm() is the per-session default —
        is never clamped even if POLYROB_PROJECT_DIR leaks into its env (MT-5).
        """
        return self._workspace_is_project_root and self._project_root is not None

    @property
    def project_root(self) -> Optional[Path]:
        """The configured project folder (None unless project-root workspace mode)."""
        return self._project_root

    def get_workspace_dir(self, session_id: str, user_id: Optional[str] = None) -> Path:
        """
        Get the workspace directory for a session.

        Args:
            session_id: The session ID
            user_id: Optional user ID, defaults to anonymous

        Returns:
            Path to the workspace directory
        """
        if self._workspace_is_project_root and self._project_root is not None:
            os.makedirs(self._project_root, exist_ok=True)
            return self._project_root

        # Clean the session ID to ensure consistent path resolution
        clean_id = self.clean_session_id(session_id)
        
        # Get session root
        session_root = self.get_session_root(clean_id, user_id)
        
        # Create the workspace directory path
        workspace_dir = (session_root / "workspace").resolve(strict=False)
        
        # Ensure directory exists with enhanced error handling
        try:
            os.makedirs(workspace_dir, exist_ok=True)
            
            # Verify the directory was actually created and is writable
            if not workspace_dir.exists():
                error_msg = f"Workspace directory {workspace_dir} doesn't exist after creation attempt"
                self.logger.error(error_msg)
                raise RuntimeError(error_msg)
                
            if not os.access(str(workspace_dir), os.W_OK):
                error_msg = f"Workspace directory {workspace_dir} is not writable"
                self.logger.error(error_msg)
                raise RuntimeError(error_msg)
                
            # Log success for debugging
            self.logger.debug(f"Verified workspace directory at {workspace_dir} (exists and is writable)")
            
        except Exception as e:
            self.logger.error(f"Error creating workspace directory {workspace_dir}: {e}")
            # No fallback - if we can't create the proper workspace, fail
            raise RuntimeError(f"Cannot create workspace directory {workspace_dir}: {e}. Session cannot continue without proper workspace.")
        
        return workspace_dir
    
    def create_file_path(self, session_id: str, subdir_name: str, filename: str, 
                       user_id: Optional[str] = None, ensure_dir: bool = True) -> Path:
        """
        Create a file path within a session's subdirectory.
        
        Args:
            session_id: The session ID
            subdir_name: Name of the subdirectory
            filename: Name of the file
            user_id: Optional user ID, defaults to anonymous
            ensure_dir: Whether to ensure the directory exists
            
        Returns:
            Path to the file
        """
        # Get subdirectory
        subdir = self.get_subdir(session_id, subdir_name, user_id)
        
        # Create the file path
        file_path = (subdir / filename).resolve(strict=False)
        
        # Ensure parent directory exists if requested
        if ensure_dir:
            os.makedirs(file_path.parent, exist_ok=True)
        
        return file_path
        
    def ensure_directory_exists(self, path: Path, lock: bool = False) -> Path:
        """
        Create a directory with proper locking to prevent race conditions.
        
        Args:
            path: Path to the directory
            lock: Whether to use file locking
            
        Returns:
            The resolved path to the directory
        """
        if lock:
            # Create a lock file path
            lock_file = str(path.parent / f"{path.name}.lock")
            
            # Acquire lock
            from agents.task.utils import get_safe_file_lock
            with get_safe_file_lock(lock_file):
                # Create directory with lock
                os.makedirs(path, exist_ok=True)
        else:
            # Create directory without lock
            os.makedirs(path, exist_ok=True)
            
        return path.resolve(strict=False)
    
    def normalize_path(self, path: str, session_id: Optional[str] = None) -> str:
        """Normalize a path for use within the workspace.
        
        The previous implementation tried to be smart about stripping duplicated
        `…/sessions/<session_id>/workspace/…` fragments. Unfortunately it also
        triggered for *normal* workspace-absolute paths which only contain that
        sequence once (the expected case).  This led to valid absolute paths
        being converted to a relative path like ``todo.md`` – breaking file
        consistency between agents.
        
        The new logic keeps the original absolute path **unless** it clearly
        contains *duplicated* session/workspace segments.  We detect a duplicate
        when either:
        1. The session id appears **more than once** in the parts list, **or**
        2. The string "workspace" appears **more than once**.
        
        Only in those cases do we strip the leading redundant sections and
        return a workspace-relative path.  Otherwise the absolute path is
        returned unchanged.
        """
        # Handle None or empty path
        if not path:
            return ""
        
        p = Path(path)
        
        # If already relative, convert to absolute path within workspace
        if not p.is_absolute():
            # For relative paths, resolve to workspace directory
            if session_id:
                clean_id = self.clean_session_id(session_id)
                workspace = self.get_workspace_dir(clean_id)
                resolved = workspace / p
                return str(resolved)
            # No session_id, return as-is
            return str(p)
        
        parts = list(p.parts)

        # In-workspace absolute paths are safe even when they do NOT contain the
        # session_id segment — e.g. workspace_is_project_root mode, where the
        # workspace is the bare project folder. Without this, every write in
        # "launch in a folder" mode is wrongly rejected as an external path.
        if session_id:
            try:
                ws_abs = os.path.abspath(str(self.get_workspace_dir(self.clean_session_id(session_id))))
                path_abs = os.path.abspath(path)
                if path_abs == ws_abs or path_abs.startswith(ws_abs + os.sep):
                    return path_abs
            except Exception:
                pass

        # If we do not have a session id or it's not present in the path we
        # treat the absolute path as *external* and reject it for security
        if not session_id or session_id not in parts:
            # SECURITY FIX: Reject potentially malicious absolute paths instead of just stripping
            if any(part in ['.', '..'] for part in parts):
                raise ValueError(f"Security violation: Path contains directory traversal elements: '{path}'")

            # Only allow simple filenames without directory separators
            filename = parts[-1]
            if '/' in filename or '\\' in filename or filename.startswith('.'):
                raise ValueError(f"Security violation: Invalid filename in external path: '{filename}'")

            self.logger.warning(
                f"Rejected external absolute path '{path}' for security - use relative paths within workspace")
            raise ValueError(f"External absolute paths not allowed: '{path}'. Use relative paths within workspace.")
        
        # Count occurrences of session_id and "workspace" to detect duplicates
        session_id_count = parts.count(session_id)
        workspace_count = parts.count("workspace")

        # Only strip duplicates if we have clear evidence of duplication
        if session_id_count > 1 or workspace_count > 1:
            # Find the LAST occurrence of the session_id to keep the most recent path structure
            try:
                last_session_idx = len(parts) - 1 - parts[::-1].index(session_id)
                # Keep everything from the last session_id occurrence onward
                deduplicated_parts = parts[last_session_idx:]
                deduplicated_path = Path(*deduplicated_parts)
                self.logger.info(f"Removed duplicate path segments: '{path}' -> '{deduplicated_path}'")
                return str(deduplicated_path)
            except ValueError:
                # Shouldn't happen since we checked session_id in parts, but be safe
                pass
        
        # At this point we have exactly one session_id and workspace in the
        # path – which is the *canonical* absolute workspace path.  Keep it as
        # is so that all agents share the same file.
        return str(p)

    def get_screenshots_dir(self, session_id: str, user_id: Optional[str] = None) -> Path:
        """Return the screenshots directory for a session."""
        return self.get_subdir(session_id, "screenshots", user_id)

    def get_logs_dir(self, session_id: str, user_id: Optional[str] = None) -> Path:
        """Return the logs directory for a session."""
        return self.get_subdir(session_id, "logs", user_id)

    def get_data_dir(self, session_id: str, user_id: Optional[str] = None) -> Path:
        """Return the data directory for a session.
        
        This directory is used for storing agent data that should not be in the workspace.
        For example: telemetry data, metadata, agent history files, etc.
        
        Args:
            session_id: The session ID
            user_id: Optional user ID, will be auto-detected if None
            
        Returns:
            Path to the data directory
        """
        return self.get_subdir(session_id, "data", user_id)
    
    def get_telemetry_dir(self, session_id: str, user_id: Optional[str] = None) -> Path:
        """Return the telemetry directory for a session.
        
        Args:
            session_id: The session ID
            user_id: Optional user ID, will be auto-detected if None
            
        Returns:
            Path to the telemetry directory
        """
        data_dir = self.get_data_dir(session_id, user_id)
        telemetry_dir = data_dir / "telemetry"
        os.makedirs(telemetry_dir, exist_ok=True)
        return telemetry_dir

    def get_history_dir(self, session_id: str, user_id: Optional[str] = None) -> Path:
        """Return the history directory for storing agent history files.
        
        Args:
            session_id: The session ID
            user_id: Optional user ID, will be auto-detected if None
            
        Returns:
            Path to the history directory
        """
        data_dir = self.get_data_dir(session_id, user_id)
        history_dir = data_dir / "history"
        os.makedirs(history_dir, exist_ok=True)
        return history_dir

    def get_feed_dir(self, session_id: str, user_id: Optional[str] = None) -> Path:
        """Return the feed directory for storing event feed files.
        
        The feed directory contains lightweight JSON snapshots used by
        interactive clients (CLI/WebView) to stream updates.
        
        Args:
            session_id: The session ID
            user_id: Optional user ID, will be auto-detected if None
            
        Returns:
            Path to the feed directory
        """
        clean_id = self.clean_session_id(session_id)
        return self.get_subdir(clean_id, "feed", user_id)
    
    def get_todo_file_path(self, session_id: str, user_id: Optional[str] = None) -> Path:
        """Return the path to the todo.md file for a session.
        
        Args:
            session_id: The session ID
            user_id: Optional user ID, will be auto-detected if None
            
        Returns:
            Path to the todo.md file
        """
        clean_id = self.clean_session_id(session_id)
        workspace_dir = self.get_workspace_dir(clean_id, user_id)
        # OR-5: in local-CLI mode the workspace IS the project root (CWD), so a bare
        # todo.md lands in the user's repo and pollutes `git status`. Nest it under a
        # `.polyrob/` metadata subdir instead. Set CLI_TODO_DOT_ROB=off to restore the
        # legacy bare-todo.md location.
        if (
            getattr(self, "_workspace_is_project_root", False)
            and os.getenv("CLI_TODO_DOT_ROB", "true").strip().lower()
            not in ("0", "false", "off", "no")
        ):
            # B2/C3: get_workspace_dir ignores session_id in project-root mode, so a
            # <project>/.polyrob/todo.md is shared by EVERY session/sub-agent/parallel
            # `rob`. Session-scope it under the per-session tree (sibling to
            # feed/logs/screenshots, already gitignored). CLI_TODO_SESSION_SCOPED=off
            # restores the legacy single shared <project>/.polyrob/todo.md.
            if os.getenv("CLI_TODO_SESSION_SCOPED", "true").strip().lower() not in (
                "0", "false", "off", "no"
            ):
                session_root = self.get_session_root(clean_id, user_id)
                os.makedirs(session_root, exist_ok=True)
                return session_root / "todo.md"
            dot_rob = workspace_dir / ".polyrob"
            os.makedirs(dot_rob, exist_ok=True)
            return dot_rob / "todo.md"
        return workspace_dir / "todo.md"