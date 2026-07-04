"""Null object pattern for sub-agent task context.

Sub-agents don't need H-MEM but the code expects the interface.
This provides safe no-op implementations that won't crash.

FIX (Jan 2026): Created to prevent AttributeError when sub-agents
access task_context_manager methods on None.

Usage:
    if is_sub_agent:
        agent.task_context_manager = NullTaskContextManager()
    else:
        agent.task_context_manager = TaskContextManager(...)
"""

import logging
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)


class NullTaskContextManager:
    """Null object for sub-agents - safe to call but does nothing.

    This class implements the same interface as TaskContextManager
    but all methods are no-ops. This allows sub-agents to safely
    call any method without special-casing throughout the codebase.

    Benefits:
    - No AttributeError on None access
    - No if-checks scattered throughout code
    - Clean separation between main and sub-agent behavior
    - Explicit about sub-agent's memory isolation
    """

    def __init__(self) -> None:
        """Initialize null context manager."""
        logger.debug("NullTaskContextManager initialized for sub-agent isolation")

    def load_session(
        self,
        session_id: str = "",
        task_description: str = "",
        **kwargs
    ) -> None:
        """No-op: Sub-agents don't load H-MEM sessions.

        Args:
            session_id: Ignored
            task_description: Ignored
            **kwargs: Ignored

        Returns:
            None (sub-agents have no session data)
        """
        return None

    def create_session(
        self,
        session_id: str = "",
        task_description: str = "",
        **kwargs
    ) -> None:
        """No-op: Sub-agents don't create H-MEM sessions.

        Args:
            session_id: Ignored
            task_description: Ignored
            **kwargs: Ignored

        Returns:
            None
        """
        return None

    def record_step(
        self,
        session_id: str = "",
        step_number: int = 0,
        action: str = "",
        result: str = "",
        **kwargs
    ) -> None:
        """No-op: Sub-agents don't record steps to H-MEM.

        Args:
            session_id: Ignored
            step_number: Ignored
            action: Ignored
            result: Ignored
            **kwargs: Ignored
        """
        pass

    def get_context_injection(
        self,
        session_id: str = "",
        brain_state: Optional[Dict[str, Any]] = None,
        **kwargs
    ) -> str:
        """No-op: Return empty string for sub-agents.

        Sub-agents don't have H-MEM context to inject.

        Args:
            session_id: Ignored
            brain_state: Ignored
            **kwargs: Ignored

        Returns:
            Empty string (no context to inject)
        """
        return ""

    def save_session(
        self,
        session_id: str = "",
        **kwargs
    ) -> None:
        """No-op: Sub-agents don't save H-MEM sessions.

        Args:
            session_id: Ignored
            **kwargs: Ignored
        """
        pass

    def add_finding(
        self,
        session_id: str = "",
        finding: str = "",
        phase_name: str = "",
        **kwargs
    ) -> None:
        """No-op: Sub-agents don't add findings to H-MEM.

        Args:
            session_id: Ignored
            finding: Ignored
            phase_name: Ignored
            **kwargs: Ignored
        """
        pass

    def transition_phase(
        self,
        session_id: str = "",
        from_phase: str = "",
        to_phase: str = "",
        **kwargs
    ) -> None:
        """No-op: Sub-agents don't transition phases.

        Args:
            session_id: Ignored
            from_phase: Ignored
            to_phase: Ignored
            **kwargs: Ignored
        """
        pass

    def get_session_stats(
        self,
        session_id: str = "",
        **kwargs
    ) -> Dict[str, Any]:
        """Return empty stats for sub-agents.

        Args:
            session_id: Ignored
            **kwargs: Ignored

        Returns:
            Empty stats dict
        """
        return {
            "is_null_manager": True,
            "session_id": session_id,
            "steps": 0,
            "findings": 0,
            "phases": []
        }

    def drain_promoted_findings(self, session_id: str) -> list:
        """No-op: Sub-agents have no H-MEM findings to drain."""
        return []

    def cleanup(self, session_id: str = "", **kwargs) -> None:
        """No-op: Nothing to clean up.

        Args:
            session_id: Ignored
            **kwargs: Ignored
        """
        pass

    def __bool__(self) -> bool:
        """Return False for truthiness checks.

        This allows code like:
            if self.task_context_manager:
                self.task_context_manager.do_something()

        To skip operations for sub-agents.

        Returns:
            False (null manager is falsy)
        """
        return False

    def __repr__(self) -> str:
        """Return string representation."""
        return "NullTaskContextManager(sub-agent isolation)"
