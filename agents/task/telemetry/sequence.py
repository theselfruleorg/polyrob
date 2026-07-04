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

import threading
import time
import uuid
from typing import Dict, Optional


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
    def get(cls, session_id: str) -> 'SequenceGenerator':
        """Get or create sequence generator for session.

        This is the primary way to obtain a sequence generator.
        Generators are cached per session ID.

        Args:
            session_id: The session to get generator for

        Returns:
            SequenceGenerator instance for the session
        """
        with cls._global_lock:
            if session_id not in cls._instances:
                cls._instances[session_id] = cls(session_id)
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

    def set_minimum(self, min_seq: int) -> None:
        """Set minimum sequence (for recovery/replay).

        If current sequence is less than min_seq, sets it to min_seq.
        This is useful when replaying events to avoid sequence conflicts.

        Args:
            min_seq: Minimum sequence number to ensure
        """
        with self._lock:
            if self._sequence < min_seq:
                self._sequence = min_seq


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


def enrich_event(event: dict, session_id: str) -> dict:
    """Add sequence number, timestamp, and ID to event.

    This is the primary function for enriching events with
    ordering and identification fields.

    Args:
        event: The event dictionary to enrich
        session_id: The session ID for sequence generation

    Returns:
        The same event dict with _seq, _ts_ms, _id added
    """
    seq_gen = SequenceGenerator.get(session_id)

    event['_seq'] = seq_gen.next()
    event['_ts_ms'] = get_timestamp_ms()
    event['_id'] = generate_event_id()

    return event
