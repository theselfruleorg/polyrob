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

from core.state.proc import ProcIdentity, is_alive, process_identity

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

        # Try to use filelock package if available
        try:
            import filelock
            self._filelock = filelock.FileLock(self.lock_file, timeout=timeout)
            self._use_filelock = True
        except ImportError:
            # Fall back to simple file-based locking
            pass

        # A flock-backed lock file is NEVER removed: the kernel drops the lock when
        # its holder dies, and the file's mtime does not move while a live holder
        # keeps it — deleting an "old" file let a second process lock a new inode
        # beside a live holder (an update that ran past 5 min lost its lock). Only
        # the O_EXCL fallback can leave a lock behind after a crash: that one is
        # cleared when its recorded pid is dead (``_clear_dead_fallback_lock``).
        if not self._use_filelock:
            self._clear_dead_fallback_lock()

        # FIXED: Trigger cleanup periodically to prevent memory leaks
        self._maybe_cleanup_stale_locks()

    def _clear_dead_fallback_lock(self) -> bool:
        """Remove an O_EXCL fallback lock file whose holder is gone. True if removed.

        The holder is the process identity written into the file (pid + start
        time + boot id, ``core.state.proc``): a live identity keeps the lock however
        old it is, and a pid reused after a reboot is not the holder. An old-format
        file holds a bare pid: a live pid (or one owned by another user) keeps it.
        A file with no readable pid is a holder that died between create and write:
        removed only once it is older than ``_STALE_LOCK_AGE``."""
        try:
            with open(self.lock_file, "r") as f:
                text = f.read().strip()
            age = time.time() - os.path.getmtime(self.lock_file)
        except (OSError, ValueError):
            return False
        ident = ProcIdentity.decode(text) if "|" in text else None
        try:
            pid = ident.pid if ident is not None else int(text)
        except ValueError:
            pid = 0
        if ident is not None:
            if pid == os.getpid() or is_alive(ident):
                return False
        elif pid > 0:
            if pid == os.getpid() or _pid_alive(pid):
                return False
        elif self._filelock is not None or age <= self._STALE_LOCK_AGE:
            # With filelock installed an empty file is the flock file (its holder
            # writes nothing): never a dead fallback lock, however old.
            return False
        try:
            os.unlink(self.lock_file)
            logger.debug("Removed a dead holder's lock file: %s (pid %s)", self.lock_file, pid or "?")
            return True
        except OSError:
            return False

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
                        
                    # A positive count is a lock THIS process holds right now: its
                    # file is never removed here, whatever its age (a held lock's
                    # mtime does not move). Only the bookkeeping of a vanished
                    # file is dropped.
                    if not os.path.exists(lock_path):
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
            except TimeoutError:
                # filelock.Timeout subclasses TimeoutError: the flock is HELD by a
                # live holder. Never fall back to O_EXCL on the same file — the
                # fallback read the empty flock file as a dead lock and took it.
                raise TimeoutError(
                    f"Failed to acquire lock {self.lock_file} within {self.timeout} seconds"
                ) from None
            except Exception as e:
                # Any other filelock error: fall back to manual locking.
                logger.warning("filelock failed on %s (%s); using the lock-file fallback",
                               self.lock_file, e)
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
                    # The holder's identity: a contender tells a dead holder (or a
                    # pid reused after a reboot) from a live one.
                    os.write(fd, f"{process_identity().encode()}\n".encode())
                finally:
                    os.close(fd)

                self._locked = True

                # Track in registry
                with self._REGISTRY_LOCK:
                    self._active_locks[self.lock_file] = self._active_locks.get(self.lock_file, 0) + 1

                return self

            except FileExistsError:
                # A holder that crashed leaves its file: clear it and retry at once.
                if self._clear_dead_fallback_lock():
                    continue
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
                                if lock_pid in (process_identity().encode(), str(os.getpid())):
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


def _pid_alive(pid: int) -> bool:
    """True when ``pid`` runs on this host. Unknown (EPERM, any error) = alive."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except Exception:
        return True
    return True


def get_safe_file_lock(lock_file: str, timeout: float = 60.0) -> Any:
    """Get a SafeFileLock instance.
    
    Args:
        lock_file: Path to the lock file
        timeout: Timeout for lock acquisition
        
    Returns:
        SafeFileLock instance
    """
    return SafeFileLock(lock_file, timeout)
