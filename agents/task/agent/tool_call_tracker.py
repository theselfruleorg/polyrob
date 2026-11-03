"""
Tool Call ID Tracking System.

This module provides robust tracking of tool call IDs throughout their lifecycle,
ensuring proper pairing of tool calls with their responses and preventing ID loss
during execution.
"""

import logging
from collections import OrderedDict
from typing import Dict, List, Optional, Set, Tuple, Any
from dataclasses import dataclass, field
from datetime import datetime
from threading import RLock
import json

from core.state.machine import StateMachine

#: 098 AG-18: a call is ``pending`` until its result pairs (``completed`` /
#: ``failed``) or its step closes unpaired (``abandoned``). All three are final.
TOOL_CALL_MACHINE = StateMachine(
    "tool_call",
    states=frozenset({"pending", "completed", "failed", "abandoned"}),
    transitions={"pending": frozenset({"completed", "failed", "abandoned"})},
    terminal=frozenset({"completed", "failed", "abandoned"}),
    initial=frozenset({"pending"}),
)

#: Bounds on the in-memory history (098 AG-18: it grew for the session's life).
MAX_COMPLETED_CALLS = 500
MAX_STEP_HISTORY = 50


@dataclass
class ToolCallRecord:
    """Record of a single tool call for tracking."""
    id: str
    name: str
    args: Dict[str, Any]
    timestamp: datetime = field(default_factory=datetime.now)
    status: str = "pending"  # TOOL_CALL_MACHINE: pending, completed, failed, abandoned
    result: Optional[Any] = None
    error: Optional[str] = None
    normalized_call: Optional[Dict[str, Any]] = None


class ToolCallTracker:
    """
    Centralized tracker for tool call IDs and their lifecycle.

    This replaces the fragile temporary attribute approach with a proper
    state management system for tool calls.
    """

    def __init__(self, session_id: str, logger: Optional[logging.Logger] = None, *,
                 user_id: Optional[str] = None, file_name: str = "tool_calls.json"):
        """
        Initialize the tracker.

        Args:
            session_id: Session identifier for context
            logger: Optional logger instance
            user_id: the session's tenant (098 AG-18: the save resolves the same
                directory the restore reads)
            file_name: the state file — a sub-agent has its own (never the parent's)
        """
        self.session_id = session_id
        self.user_id = user_id
        self.file_name = file_name
        self._dirty = False  # 098 AG-18: save only after a change
        
        # Use consistent logger initialization pattern (matches Agent, Controller, Registry, etc.)
        if logger:
            self.logger = logger
        else:
            from agents.task.logging_config import get_task_logger
            self.logger = get_task_logger("tool_call_tracker", session_id)

        # Core tracking structures
        self._active_calls: Dict[str, ToolCallRecord] = {}
        self._completed_calls: "OrderedDict[str, ToolCallRecord]" = OrderedDict()
        self._orphaned_calls: Set[str] = set()

        # Step-based tracking for the current execution
        self._current_step_calls: List[str] = []
        self._step_history: List[List[str]] = []

        # Thread safety with re-entrant lock for better async compatibility
        self._lock = RLock()

        # Statistics
        self._stats = {
            "total_calls": 0,
            "completed_calls": 0,
            "failed_calls": 0,
            "orphaned_calls": 0,
            "abandoned_calls": 0,
        }

        # MCP validation-failure tracking (schema-injection policy) now lives in
        # tools/mcp/ — that's MCP error policy, not tool-call ID lifecycle.
        from tools.mcp.validation_tracker import MCPValidationTracker
        self._mcp_validation = MCPValidationTracker(logger=self.logger)

        self.logger.debug(f"ToolCallTracker initialized for session {session_id}")

    def register_tool_calls(self, tool_calls: List[Dict[str, Any]]) -> List[str]:
        """
        Register new tool calls for tracking.

        Args:
            tool_calls: List of normalized tool call dictionaries

        Returns:
            List of registered tool call IDs
        """
        with self._lock:
            registered_ids = []

            for call in tool_calls:
                call_id = call.get("id")
                if not call_id:
                    self.logger.warning(f"Tool call missing ID: {call}")
                    continue

                # Create record
                record = ToolCallRecord(
                    id=call_id,
                    name=call.get("name", "unknown"),
                    args=call.get("args", {}),
                    normalized_call=call
                )

                # Store in active calls
                self._active_calls[call_id] = record
                registered_ids.append(call_id)
                self._current_step_calls.append(call_id)
                self._stats["total_calls"] += 1
                self._dirty = True

                self.logger.debug(f"Registered tool call: {call_id} ({record.name})")

            return registered_ids

    def get_current_step_calls(self) -> List[str]:
        """
        Get tool call IDs for the current step.

        Returns:
            List of tool call IDs in the current execution step
        """
        with self._lock:
            return self._current_step_calls.copy()

    def mark_completed(self, call_id: str, result: Any = None) -> bool:
        """
        Mark a tool call as completed with optional result.

        Args:
            call_id: Tool call ID
            result: Optional execution result

        Returns:
            True if successfully marked, False if not found
        """
        with self._lock:
            if call_id in self._active_calls:
                record = self._active_calls[call_id]
                record.status = "completed"
                # M2: do NOT retain the full tool result. It's kept for the session's
                # lifetime in _completed_calls but never read back (only status/name/
                # timestamp are used) — on a long run that's an unbounded duplicate of
                # every large tool output. The payload already lives in message history.
                record.result = None

                # Move to completed
                self._retire(call_id, record)

                self._stats["completed_calls"] += 1
                self.logger.debug(f"Tool call {call_id} completed")
                return True
            return False

    def mark_failed(self, call_id: str, error: str) -> bool:
        """
        Mark a tool call as failed with error message.

        Args:
            call_id: Tool call ID
            error: Error message

        Returns:
            True if successfully marked, False if not found
        """
        with self._lock:
            if call_id in self._active_calls:
                record = self._active_calls[call_id]
                record.status = "failed"
                record.error = error

                # Move to completed (with failed status)
                self._retire(call_id, record)

                self._stats["failed_calls"] += 1
                self.logger.warning(f"Tool call {call_id} failed: {error}")
                return True
            return False


    def _retire(self, call_id: str, record: ToolCallRecord) -> None:
        """Move a finished call out of the active set into the BOUNDED history."""
        self._active_calls.pop(call_id, None)
        self._completed_calls[call_id] = record
        while len(self._completed_calls) > MAX_COMPLETED_CALLS:
            self._completed_calls.popitem(last=False)
        self._dirty = True

    def complete_step(self) -> None:
        """Complete the current step and start a new one.

        098 AG-18: a call of the closing step that never paired is ``abandoned``
        (it used to stay ``pending`` for the life of the session, and every
        later LLM call re-saved the file). The file is saved only when the
        state changed since the last save."""
        with self._lock:
            if self._current_step_calls:
                for call_id in self._current_step_calls:
                    record = self._active_calls.get(call_id)
                    if record is not None:
                        record.status = "abandoned"
                        self._retire(call_id, record)
                        self._stats["abandoned_calls"] += 1
                self._step_history.append(self._current_step_calls.copy())
                del self._step_history[:-MAX_STEP_HISTORY]
                self._current_step_calls.clear()
                self._dirty = True
                self.logger.debug(f"Completed step with {len(self._step_history[-1])} tool calls")
            if not self._dirty:
                return

            # PERSISTENCE: best-effort auto-save (a durability backstop, never the
            # source of truth). P1 finalization: one attempt, no retry sleep — this
            # runs on the event loop; a miss is re-saved at the next change.
            try:
                from agents.task.path import pm

                clean_session_id = pm().clean_session_id(self.session_id)
                data_dir = pm().get_data_dir(clean_session_id, self.user_id)
                state_file = data_dir / self.file_name
                if self.save_to_file(state_file):
                    self._dirty = False
                    self.logger.debug(f"✓ Auto-saved tool call state to {state_file}")
                else:
                    self.logger.warning(f"tool call state save failed (non-fatal): {state_file}")
            except Exception as e:
                self.logger.error(f"❌ Critical error during tool call state persistence: {e}", exc_info=True)

    def save_to_file(self, filepath) -> bool:
        """Save tracker state to file.

        Args:
            filepath: Path to save state

        Returns:
            True if successful
        """
        from pathlib import Path
        with self._lock:
            try:
                state_data = {
                    'version': '1.0',
                    'session_id': self.session_id,
                    'timestamp': datetime.now().isoformat(),
                    'active_calls': {
                        call_id: {
                            'id': record.id,
                            'name': record.name,
                            'args': record.args,
                            'timestamp': record.timestamp.isoformat(),
                            'status': record.status,
                            'error': record.error
                        }
                        for call_id, record in self._active_calls.items()
                    },
                    'current_step_calls': self._current_step_calls,
                    'stats': self._stats
                }

                from core.state.files import atomic_write_json
                filepath = Path(filepath)
                atomic_write_json(str(filepath), state_data)
                # atomic_write_json births 0600; the shared data home needs group-write.
                from core.data_perms import apply_birth_mode
                apply_birth_mode(filepath)
                return True

            except Exception as e:
                self.logger.error(f"Failed to save tool call state: {e}")
                return False

    def load_from_file(self, filepath) -> bool:
        """Load tracker state from file.

        Args:
            filepath: Path to load from

        Returns:
            True if successful
        """
        from pathlib import Path
        with self._lock:
            try:
                filepath = Path(filepath)
                if not filepath.exists():
                    return False

                with open(filepath, 'r') as f:
                    state_data = json.load(f)

                # Validate
                if state_data.get('version') != '1.0':
                    self.logger.warning(f"Version mismatch: {state_data.get('version')}")
                    return False

                if state_data.get('session_id') != self.session_id:
                    self.logger.warning(f"Session ID mismatch")
                    return False

                # 098 AG-18: a call still active at save time never paired — after
                # a restart it is `abandoned` history, not a live call.
                for call_id, call_data in state_data.get('active_calls', {}).items():
                    record = ToolCallRecord(
                        id=call_data['id'],
                        name=call_data['name'],
                        args=call_data['args'],
                        timestamp=datetime.fromisoformat(call_data['timestamp']),
                        status="abandoned",
                        error=call_data.get('error')
                    )
                    self._retire(call_id, record)

                # Restore stats
                saved_stats = state_data.get('stats', {})
                for key in self._stats:
                    if key in saved_stats:
                        self._stats[key] = saved_stats[key]

                self.logger.info(f"Restored tool call tracker with {len(self._active_calls)} active calls")
                return True

            except Exception as e:
                self.logger.error(f"Failed to load tool call state: {e}")
                return False

    def has_active_calls(self) -> bool:
        """
        Check if there are any active (non-completed) tool calls.

        Returns:
            True if there are active calls
        """
        with self._lock:
            return len(self._active_calls) > 0

    def has_call(self, call_id: str) -> bool:
        """
        Check if a tool call ID exists in tracking.

        Args:
            call_id: Tool call ID to check

        Returns:
            True if call exists in active or completed calls
        """
        with self._lock:
            return call_id in self._active_calls or call_id in self._completed_calls

    def get_statistics(self) -> Dict[str, Any]:
        """
        Get tracker statistics.

        Returns:
            Dictionary of statistics
        """
        with self._lock:
            return {
                **self._stats.copy(),
                "active_calls": len(self._active_calls),
                "completed_calls_cached": len(self._completed_calls),
                "orphaned_ids": len(self._orphaned_calls),
                "total_steps": len(self._step_history)
            }

    def register_call(self, call_id: str, tool_name: str, args: Dict[str, Any]) -> None:
        """
        Simple interface to register a single tool call.

        Args:
            call_id: Tool call ID
            tool_name: Name of the tool being called
            args: Arguments passed to the tool
        """
        self.register_tool_calls([{
            "id": call_id,
            "name": tool_name,
            "args": args
        }])

    def complete_call(self, call_id: str, result: Any = None) -> None:
        """
        Simple interface to mark a tool call as completed.

        Args:
            call_id: Tool call ID
            result: Result of the tool execution
        """
        self.mark_completed(call_id, result)

    def reset(self) -> None:
        """Reset the tracker to initial state."""
        with self._lock:
            self._active_calls.clear()
            self._completed_calls.clear()
            self._orphaned_calls.clear()
            self._current_step_calls.clear()
            self._step_history.clear()
            self._mcp_validation.reset()
            self._stats = {
                "total_calls": 0,
                "completed_calls": 0,
                "failed_calls": 0,
                "orphaned_calls": 0,
                "abandoned_calls": 0,
            }
            self._dirty = True
            self.logger.info(f"ToolCallTracker reset for session {self.session_id}")

    # ========== Issue #1 Fix: MCP Validation Failure Tracking ==========

    # MCP validation-failure policy is delegated to MCPValidationTracker
    # (tools/mcp/validation_tracker.py). These thin wrappers preserve the
    # public API for existing callers (tools/mcp/mcp_tool.py).

    def track_mcp_validation_failure(self, server_name: str, tool_name: str) -> int:
        """Track an MCP validation failure; returns the current count."""
        return self._mcp_validation.track_failure(server_name, tool_name)

    def should_inject_mcp_schema(self, server_name: str, tool_name: str) -> bool:
        """True after repeated validation failures (inject full schema to help the LLM)."""
        return self._mcp_validation.should_inject_schema(server_name, tool_name)

    def clear_mcp_validation_failures(self, server_name: str, tool_name: str) -> None:
        """Clear validation failure count after successful execution."""
        self._mcp_validation.clear_failures(server_name, tool_name)

