"""
Tool Call ID Tracking System.

This module provides robust tracking of tool call IDs throughout their lifecycle,
ensuring proper pairing of tool calls with their responses and preventing ID loss
during execution.
"""

import logging
from typing import Dict, List, Optional, Set, Tuple, Any
from dataclasses import dataclass, field
from datetime import datetime
from threading import RLock
import json


@dataclass
class ToolCallRecord:
    """Record of a single tool call for tracking."""
    id: str
    name: str
    args: Dict[str, Any]
    timestamp: datetime = field(default_factory=datetime.now)
    status: str = "pending"  # pending, executing, completed, failed
    result: Optional[Any] = None
    error: Optional[str] = None
    normalized_call: Optional[Dict[str, Any]] = None


class ToolCallTracker:
    """
    Centralized tracker for tool call IDs and their lifecycle.

    This replaces the fragile temporary attribute approach with a proper
    state management system for tool calls.
    """

    def __init__(self, session_id: str, logger: Optional[logging.Logger] = None):
        """
        Initialize the tracker.

        Args:
            session_id: Session identifier for context
            logger: Optional logger instance
        """
        self.session_id = session_id
        
        # Use consistent logger initialization pattern (matches Agent, Controller, Registry, etc.)
        if logger:
            self.logger = logger
        else:
            from agents.task.logging_config import get_task_logger
            self.logger = get_task_logger("tool_call_tracker", session_id)

        # Core tracking structures
        self._active_calls: Dict[str, ToolCallRecord] = {}
        self._completed_calls: Dict[str, ToolCallRecord] = {}
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
            "orphaned_calls": 0
        }

        # MCP validation-failure tracking (schema-injection policy) now lives in
        # tools/mcp/ — that's MCP error policy, not tool-call ID lifecycle.
        from tools.mcp.validation_tracker import MCPValidationTracker
        self._mcp_validation = MCPValidationTracker(logger=self.logger)

        # ID generation counter for globally unique IDs (Issue #7 fix)
        self._id_counter = 0

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

    def get_normalized_calls(self, call_ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """
        Get normalized tool calls by IDs.

        Args:
            call_ids: Optional list of specific IDs (uses current step if not provided)

        Returns:
            List of normalized tool call dictionaries
        """
        with self._lock:
            if call_ids is None:
                call_ids = self._current_step_calls

            normalized = []
            for call_id in call_ids:
                if call_id in self._active_calls:
                    record = self._active_calls[call_id]
                    if record.normalized_call:
                        normalized.append(record.normalized_call)
                elif call_id in self._completed_calls:
                    record = self._completed_calls[call_id]
                    if record.normalized_call:
                        normalized.append(record.normalized_call)

            return normalized


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
                self._completed_calls[call_id] = record
                del self._active_calls[call_id]

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
                self._completed_calls[call_id] = record
                del self._active_calls[call_id]

                self._stats["failed_calls"] += 1
                self.logger.warning(f"Tool call {call_id} failed: {error}")
                return True
            return False


    def complete_step(self) -> None:
        """Complete the current step and start a new one."""
        with self._lock:
            if self._current_step_calls:
                self._step_history.append(self._current_step_calls.copy())
                self._current_step_calls.clear()
                self.logger.debug(f"Completed step with {len(self._step_history[-1])} tool calls")

            # PERSISTENCE: Auto-save state after each step with retry logic
            try:
                from agents.task.path import pm
                import time

                # Clean session ID before getting data dir
                clean_session_id = pm().clean_session_id(self.session_id)
                data_dir = pm().get_data_dir(clean_session_id)
                state_file = data_dir / "tool_calls.json"

                # Ensure directory exists
                data_dir.mkdir(parents=True, exist_ok=True)

                # Retry logic: 3 attempts with exponential backoff
                max_retries = 3
                for attempt in range(max_retries):
                    if self.save_to_file(state_file):
                        if attempt > 0:
                            self.logger.info(f"✓ Auto-saved tool call state to {state_file} (attempt {attempt + 1})")
                        else:
                            self.logger.debug(f"✓ Auto-saved tool call state to {state_file}")
                        break
                    else:
                        if attempt < max_retries - 1:
                            wait_time = 0.1 * (2 ** attempt)  # 0.1s, 0.2s, 0.4s
                            self.logger.warning(f"Failed to save tool call state (attempt {attempt + 1}/{max_retries}), retrying in {wait_time}s...")
                            time.sleep(wait_time)
                        else:
                            self.logger.error(f"❌ Failed to auto-save tool call state after {max_retries} attempts: {state_file}")
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

                # Atomic write
                import tempfile
                import os

                filepath = Path(filepath)
                filepath.parent.mkdir(parents=True, exist_ok=True)

                temp_fd, temp_path = tempfile.mkstemp(
                    dir=filepath.parent,
                    prefix='.tool_calls_',
                    suffix='.tmp'
                )

                try:
                    with os.fdopen(temp_fd, 'w') as f:
                        json.dump(state_data, f, indent=2)
                        f.flush()
                        os.fsync(f.fileno())

                    os.replace(temp_path, str(filepath))
                    return True

                except Exception:
                    if os.path.exists(temp_path):
                        os.unlink(temp_path)
                    raise

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

                # Restore active calls
                for call_id, call_data in state_data.get('active_calls', {}).items():
                    record = ToolCallRecord(
                        id=call_data['id'],
                        name=call_data['name'],
                        args=call_data['args'],
                        timestamp=datetime.fromisoformat(call_data['timestamp']),
                        status=call_data['status'],
                        error=call_data.get('error')
                    )
                    self._active_calls[call_id] = record

                # Restore current step
                self._current_step_calls = state_data.get('current_step_calls', [])

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

    def get_call_record(self, call_id: str) -> Optional[ToolCallRecord]:
        """
        Get complete record for a tool call.

        Args:
            call_id: Tool call ID

        Returns:
            ToolCallRecord or None if not found
        """
        with self._lock:
            if call_id in self._active_calls:
                return self._active_calls[call_id]
            elif call_id in self._completed_calls:
                return self._completed_calls[call_id]
            return None


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
                "orphaned_calls": 0
            }
            self.logger.info(f"ToolCallTracker reset for session {self.session_id}")

    # ========== Issue #7 Fix: Centralized ID Generation ==========

    def generate_call_id(self, tool_name: str = "call") -> str:
        """Generate a globally unique tool call ID.

        Format: call_{session_prefix}_{counter}_{timestamp_suffix}

        Args:
            tool_name: Optional tool name for context

        Returns:
            Unique tool call ID
        """
        with self._lock:
            self._id_counter += 1
            # Use session prefix (first 8 chars) + counter + timestamp suffix
            session_prefix = self.session_id[:8] if self.session_id else "unknown"
            timestamp_suffix = datetime.now().strftime("%H%M%S")
            return f"call_{session_prefix}_{self._id_counter}_{timestamp_suffix}"

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

