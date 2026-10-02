"""
Sequence Number Generator for Telemetry Events

Provides monotonically increasing sequence numbers per session.
Thread-safe for concurrent agent execution.

Usage:
    from agents.task.telemetry.sequence import SequenceGenerator, generate_event_id

    seq_gen = SequenceGenerator.get(session_id)
    seq = seq_gen.next()
    event_id = generate_event_id()
"""

import os
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, Optional, Union


def _highest_seq_on_disk(feed_dir: Union[str, Path]) -> int:
    """Highest leading integer of a ``[0-9]*_*.json`` name in ``feed_dir``.

    Returns 0 when the folder holds no such file or cannot be read.
    """
    best = 0
    try:
        with os.scandir(feed_dir) as it:
            for entry in it:
                name = entry.name
                if not name.endswith(".json") or not name[:1].isdigit():
                    continue
                head, sep, _ = name.partition("_")
                if sep and head.isdigit():
                    best = max(best, int(head))
    except OSError:
        return 0
    return best


class SequenceGenerator:
    """Thread-safe sequence number generator per session.

    Each session gets its own sequence generator that produces
    monotonically increasing integers. Sequence numbers are:
    - Unique within a session
    - Monotonically increasing
    - Thread-safe for concurrent access

    Sequence numbers are used for:
    - Guaranteed event ordering regardless of timestamp
    - Delta sync (fetch events after sequence N)
    - Deduplication (detect gaps in sequence)
    """

    _instances: Dict[str, 'SequenceGenerator'] = {}
    _global_lock = threading.Lock()

    def __init__(self, session_id: str):
        """Initialize sequence generator for a session.

        Args:
            session_id: The session ID this generator is for
        """
        self.session_id = session_id
        self._sequence = 0
        self._lock = threading.Lock()

    @classmethod
    def get(cls, session_id: str, feed_dir: Optional[Union[str, Path]] = None) -> 'SequenceGenerator':
        """Get or create sequence generator for session.

        This is the primary way to obtain a sequence generator.
        Generators are cached per session ID.

        Args:
            session_id: The session to get generator for
            feed_dir: The session's feed folder. On first creation only, the
                counter starts at the highest leading integer of any
                ``[0-9]*_*.json`` name there, so a restarted process does not
                write ``000001_*`` again (W1.4). A cached generator is never
                reseeded.

        Returns:
            SequenceGenerator instance for the session
        """
        with cls._global_lock:
            if session_id not in cls._instances:
                gen = cls(session_id)
                if feed_dir is not None:
                    gen._sequence = _highest_seq_on_disk(feed_dir)
                cls._instances[session_id] = gen
            return cls._instances[session_id]

    @classmethod
    def reset(cls, session_id: str) -> None:
        """Reset sequence for session (for testing).

        Removes the cached generator for a session, so next call
        to get() will create a fresh generator starting at 0.

        Args:
            session_id: The session to reset
        """
        with cls._global_lock:
            cls._instances.pop(session_id, None)

    @classmethod
    def reset_all(cls) -> None:
        """Reset all sequence generators (for testing)."""
        with cls._global_lock:
            cls._instances.clear()

    def next(self) -> int:
        """Get next sequence number (thread-safe).

        Returns:
            The next sequence number (1, 2, 3, ...)
        """
        with self._lock:
            self._sequence += 1
            return self._sequence

    def current(self) -> int:
        """Get current sequence without incrementing.

        Returns:
            The current sequence number (0 if no events yet)
        """
        with self._lock:
            return self._sequence

def generate_event_id() -> str:
    """Generate unique event ID.

    Creates a short but unique identifier for events.
    Uses UUID4 truncated to 12 characters for readability
    while maintaining sufficient uniqueness.

    Returns:
        12-character hex string (e.g., "a1b2c3d4e5f6")
    """
    return uuid.uuid4().hex[:12]


def get_timestamp_ms() -> int:
    """Get current timestamp in milliseconds.

    Returns:
        Unix timestamp in milliseconds
    """
    return int(time.time() * 1000)


