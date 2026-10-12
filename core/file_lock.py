"""A cross-platform file lock with a plain lock-file fallback (``SafeFileLock``).

Moved from ``agents/task/utils.py`` (W7) so the core tier (``core.interactive_gate``)
can take the lock without importing the agents tier. Uses the ``filelock`` package
when it is installed, else a simple ``O_EXCL`` lock file.
"""
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)


class SafeFileLock:
    """
    Cross-platform file locking implementation with fallback mechanism.
    
    This provides a unified locking mechanism that works across:
    1. Linux/macOS (using filelock package if available)
    2. Windows (using filelock package if available)
    3. Any platform (using a simple lock file as fallback)
    
    Usage:
        with SafeFileLock('/path/to/file.lock'):
            # Perform operations requiring exclusive access
    """
    __slots__ = ('lock_file', 'timeout', 'poll_interval', '_lock', '_locked', '_use_filelock', '_filelock')
    
    # FIXED: Track acquired locks with bounded cache and proper cleanup
    _active_locks = {}  # Maps lock file paths to lock counters
    _REGISTRY_LOCK = threading.RLock()  # Lock for modifying _active_locks
    _last_cleanup = 0  # Track last cleanup time
    _MAX_CACHE_SIZE = 1000  # FIXED: Limit cache size to prevent memory leaks
    _CLEANUP_INTERVAL = 60  # FIX 9: Reduced from 300 to 60 seconds for more aggressive cleanup
    _STALE_LOCK_AGE = 300  # FIX 9: Consider locks stale after 5 minutes (was 3600/1 hour)
    
    def __init__(self, lock_file: str, timeout: float = 60.0, poll_interval: float = 0.1):
        """Initialize the SafeFileLock.

        Args:
            lock_file: Path to the lock file
            timeout: Maximum time to wait for lock acquisition
            poll_interval: Time between lock acquisition attempts
        """
        self.lock_file = os.path.abspath(lock_file)
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._lock = None
        self._locked = False
        self._use_filelock = False
        self._filelock = None

        # FIX 9: Check if our specific lock file is stale before trying to acquire
        if os.path.exists(self.lock_file):
            try:
                file_age = time.time() - os.path.getmtime(self.lock_file)
                if file_age > self._STALE_LOCK_AGE:
                    # Try to remove our specific stale lock file
                    try:
                        os.unlink(self.lock_file)
                        logger.debug(f"Removed stale lock on init: {self.lock_file} (age: {file_age:.1f}s)")
                    except (OSError, PermissionError):
                        logger.debug(f"Could not remove stale lock on init: {self.lock_file}")
            except (OSError, FileNotFoundError):
                pass

        # Try to use filelock package if available
        try:
            import filelock
            self._filelock = filelock.FileLock(self.lock_file, timeout=timeout)
            self._use_filelock = True
        except ImportError:
            # Fall back to simple file-based locking
            pass

        # FIXED: Trigger cleanup periodically to prevent memory leaks
        self._maybe_cleanup_stale_locks()
    
    @classmethod
    def _maybe_cleanup_stale_locks(cls):
        """Cleanup stale locks and limit cache size to prevent memory leaks."""
        current_time = time.time()
        
        # Only cleanup every _CLEANUP_INTERVAL seconds
        if current_time - cls._last_cleanup < cls._CLEANUP_INTERVAL:
            return
            
        with cls._REGISTRY_LOCK:
            try:
                # FIXED: Remove locks that are no longer held
                stale_locks = []
                for lock_path, count in list(cls._active_locks.items()):
                    # Remove locks with zero or negative count
                    if count <= 0:
                        stale_locks.append(lock_path)
                        continue
                        
                    # FIX 9: More aggressive stale lock detection and cleanup
                    try:
                        if os.path.exists(lock_path):
                            # Check if lock file is older than _STALE_LOCK_AGE seconds
                            file_age = current_time - os.path.getmtime(lock_path)
                            if file_age > cls._STALE_LOCK_AGE:  # FIX 9: Use configurable timeout
                                # Try to remove stale lock file
                                try:
                                    os.unlink(lock_path)
                                    stale_locks.append(lock_path)
                                    logger.debug(f"Removed stale lock file {lock_path} (age: {file_age:.1f}s)")
                                except (OSError, PermissionError):
                                    # Can't remove, but don't count it as active
                                    logger.debug(f"Could not remove stale lock {lock_path}: permission denied")
                                    pass
                        else:
                            # Lock file doesn't exist, remove from tracking
                            stale_locks.append(lock_path)
                    except (OSError, FileNotFoundError):
                        # File system error, remove from tracking
                        stale_locks.append(lock_path)
                
                # Remove stale locks from tracking
                for lock_path in stale_locks:
                    cls._active_locks.pop(lock_path, None)
                
                # FIXED: Implement bounded cache - if cache is too large, remove oldest entries
                if len(cls._active_locks) > cls._MAX_CACHE_SIZE:
                    # Sort by path and remove oldest (we don't have timestamps, so use lexicographic)
                    sorted_locks = sorted(cls._active_locks.items())
                    locks_to_remove = len(cls._active_locks) - (cls._MAX_CACHE_SIZE // 2)  # Remove half
                    
                    for i in range(locks_to_remove):
                        if i < len(sorted_locks):
                            lock_path, count = sorted_locks[i]
                            # Only remove if not currently held
                            if count <= 0:
                                cls._active_locks.pop(lock_path, None)
                
                # Update last cleanup time
                cls._last_cleanup = current_time
                
                if stale_locks:
                    # Use basic logging to avoid circular imports
                    print(f"SafeFileLock: Cleaned up {len(stale_locks)} stale locks")
                    
            except Exception as e:
                # Log cleanup errors but don't raise
                print(f"SafeFileLock cleanup error: {e}")
    
    def __enter__(self):
        """Acquire the lock."""
        if self._use_filelock and self._filelock:
            try:
                self._filelock.acquire(timeout=self.timeout)
                self._locked = True
                
                # Track in registry
                with self._REGISTRY_LOCK:
                    self._active_locks[self.lock_file] = self._active_locks.get(self.lock_file, 0) + 1
                
                return self
            except Exception as e:
                # Fall back to manual locking if filelock fails
                self._use_filelock = False
        
        # Manual file-based locking. Attempt acquisition at least once regardless of
        # timeout (do-while), so a FREE lock is acquired even with timeout=0; only a
        # CONTENDED lock fails fast at timeout=0. (The old `while elapsed < timeout`
        # never ran the body at timeout=0, so a free lock could never be acquired —
        # which silently disabled the goal dispatcher / cron tick on machines without
        # the `filelock` package, where this fallback path is taken.)
        start_time = time.time()

        while True:
            try:
                # FIXED: Use exclusive create mode to prevent race conditions
                fd = os.open(self.lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                try:
                    # Write PID to lock file for debugging
                    os.write(fd, f"{os.getpid()}\n".encode())
                finally:
                    os.close(fd)

                self._locked = True

                # Track in registry
                with self._REGISTRY_LOCK:
                    self._active_locks[self.lock_file] = self._active_locks.get(self.lock_file, 0) + 1

                return self

            except FileExistsError:
                # Lock file already exists; retry until the timeout budget is spent.
                if time.time() - start_time >= self.timeout:
                    break
                time.sleep(self.poll_interval)
                continue
            except OSError as e:
                # Other file system error
                raise RuntimeError(f"Failed to acquire lock {self.lock_file}: {e}")

        # Timeout reached
        raise TimeoutError(f"Failed to acquire lock {self.lock_file} within {self.timeout} seconds")
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Release the lock."""
        if not self._locked:
            return
        
        try:
            if self._use_filelock and self._filelock:
                self._filelock.release()
            else:
                # Manual lock release
                try:
                    # FIXED: Verify we still own the lock before removing
                    if os.path.exists(self.lock_file):
                        # Check if we own this lock by reading PID
                        try:
                            with open(self.lock_file, 'r') as f:
                                lock_pid = f.read().strip()
                                if lock_pid == str(os.getpid()):
                                    os.unlink(self.lock_file)
                                else:
                                    # Lock was taken by another process, don't remove
                                    pass
                        except (OSError, ValueError):
                            # Can't read lock file, try to remove anyway
                            try:
                                os.unlink(self.lock_file)
                            except (OSError, FileNotFoundError):
                                pass
                except (OSError, FileNotFoundError):
                    # Lock file already removed or can't be removed
                    pass
            
            # Update registry
            with self._REGISTRY_LOCK:
                if self.lock_file in self._active_locks:
                    self._active_locks[self.lock_file] -= 1
                    # FIXED: Clean up zero-count entries immediately
                    if self._active_locks[self.lock_file] <= 0:
                        self._active_locks.pop(self.lock_file, None)
            
        finally:
            self._locked = False


def get_safe_file_lock(lock_file: str, timeout: float = 60.0) -> Any:
    """Get a SafeFileLock instance.
    
    Args:
        lock_file: Path to the lock file
        timeout: Timeout for lock acquisition
        
    Returns:
        SafeFileLock instance
    """
    return SafeFileLock(lock_file, timeout)
