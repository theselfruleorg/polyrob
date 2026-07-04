"""Task management tool for AutoV2 sessions.

Provides todo list management actions for AI agents.
All TODO logic is self-contained in this tool - no external TodoManager needed.

Actions (all namespaced with 'task_' prefix):
- task_todo_list: List all todos
- task_todo_add: Add new todo item
- task_todo_complete: Mark todo as complete
- task_todo_progress: Get progress stats
- task_todo_next: Get next incomplete task

NOTE: The 'done' action is NOT part of this tool - it's a core Controller action.
"""

import logging
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional, List

from pydantic import BaseModel
from tools.base_tool import BaseTool, ToolStatus
from core.config import BotConfig
from core.exceptions import ServiceError
from tools.controller.types import ActionResult
from tools.controller.views import (
    TodoListAction,
    TodoAddAction,
    TodoCompleteAction,
    TodoProgressAction,
    TodoNextAction
)


@dataclass
class TodoItem:
    """Single TODO item."""
    id: int
    text: str
    completed: bool = False
    priority: int = 0
    created_at: datetime = field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None


class TaskTool(BaseTool):
    """Task management tool with complete TODO functionality.

    This tool is the single source of truth for TODO management:
    - Manages per-session TODO lists in memory
    - Persists todos to todo.md files (markdown format)
    - Provides progress tracking and completion checks
    - Used by Agent to enforce TODO completion

    All TODO state and logic is contained in this tool.
    """

    @property
    def required_services(self) -> Dict[str, str]:
        """Get required services."""
        return {
            'rate_limit_manager': 'Rate limit management'
        }

    @property
    def optional_services(self) -> Dict[str, str]:
        """Get optional services."""
        return {}

    def __init__(self, name: str, config: BotConfig, container: Optional[Any] = None):
        """Initialize task tool.

        Args:
            name: Tool name (should be 'task')
            config: Bot configuration
            container: Dependency container
        """
        super().__init__(name=name, config=config, container=container)

        # Per-session TODO storage (session_id -> List[TodoItem])
        self._todos: Dict[str, List[TodoItem]] = {}

        # Track next ID per session
        self._next_id: Dict[str, int] = {}

    async def _initialize(self) -> None:
        """Initialize task tool."""
        # Call parent initialization to register decorated actions
        await super()._initialize()
        self.logger.info("Task tool initialized - ready for session-based todo management")

    async def _cleanup(self) -> None:
        """Cleanup task tool resources."""
        # Save all TODO lists before cleanup
        for session_id in list(self._todos.keys()):
            try:
                self._save_to_file(session_id)
                self.logger.debug(f"Saved todos for session {session_id[:8]}")
            except Exception as e:
                self.logger.warning(f"Failed to save todos for {session_id[:8]}: {e}")

        # Clear state
        self._todos.clear()
        self._next_id.clear()
        self.logger.info("Task tool cleanup complete")

    # ===== Internal State Management =====

    def _get_todos(self, session_id: str, user_id: Optional[str] = None) -> List[TodoItem]:
        """Get todos for a session, loading from file if needed.

        Args:
            session_id: Session identifier
            user_id: Optional user identifier

        Returns:
            List of TodoItem objects for this session
        """
        from agents.task.path import pm

        # Clean session ID
        clean_id = pm().clean_session_id(session_id)

        # Load from file if not in memory
        if clean_id not in self._todos:
            self._load_from_file(clean_id, user_id)

        return self._todos[clean_id]

    def _load_from_file(self, session_id: str, user_id: Optional[str] = None) -> None:
        """Load todos from todo.md file.

        Args:
            session_id: Session identifier
            user_id: Optional user identifier
        """
        from agents.task.path import pm

        # Get file path
        todo_path = pm().get_todo_file_path(session_id, user_id)

        # Initialize empty list
        self._todos[session_id] = []
        self._next_id[session_id] = 1

        # Load if file exists
        if not todo_path.exists():
            self.logger.debug(f"No todo file found for {session_id[:8]}")
            return

        try:
            with open(todo_path, 'r', encoding='utf-8') as f:
                content = f.read()

            # Parse markdown checkboxes
            # Format: - [ ] Task text (id: 1)
            #         - [x] Completed task (id: 2)
            pattern = r'^- \[([ x])\] (.+?)(?: \(id: (\d+)\))?$'

            for line in content.split('\n'):
                match = re.match(pattern, line.strip())
                if match:
                    completed_marker, text, task_id = match.groups()

                    # Extract ID or assign new one
                    if task_id:
                        item_id = int(task_id)
                    else:
                        item_id = self._next_id[session_id]
                        self._next_id[session_id] += 1

                    # Create TodoItem
                    item = TodoItem(
                        id=item_id,
                        text=text.strip(),
                        completed=(completed_marker == 'x')
                    )
                    self._todos[session_id].append(item)

                    # Update next_id tracker
                    if item_id >= self._next_id[session_id]:
                        self._next_id[session_id] = item_id + 1

            self.logger.info(f"Loaded {len(self._todos[session_id])} todos for {session_id[:8]}")

        except Exception as e:
            self.logger.error(f"Failed to load todos from {todo_path}: {e}")
            self._todos[session_id] = []
            self._next_id[session_id] = 1

    def _save_to_file(self, session_id: str, user_id: Optional[str] = None) -> None:
        """Save todos to todo.md file atomically.

        Args:
            session_id: Session identifier
            user_id: Optional user identifier
        """
        from agents.task.path import pm

        # Get file path
        todo_path = pm().get_todo_file_path(session_id, user_id)

        # Ensure directory exists
        todo_path.parent.mkdir(parents=True, exist_ok=True)

        # Get todos
        todos = self._todos.get(session_id, [])

        # Build markdown content
        lines = []
        for item in todos:
            marker = 'x' if item.completed else ' '
            lines.append(f"- [{marker}] {item.text} (id: {item.id})")

        content = '\n'.join(lines) + '\n'

        # Atomic write (temp file + rename)
        try:
            fd, temp_path = tempfile.mkstemp(
                dir=todo_path.parent,
                prefix='.todo_',
                suffix='.tmp',
                text=True
            )

            try:
                with open(fd, 'w', encoding='utf-8') as f:
                    f.write(content)
                    f.flush()
                    import os
                    os.fsync(f.fileno())

                # Atomic rename
                import os
                os.replace(temp_path, str(todo_path))
                self.logger.debug(f"Saved {len(todos)} todos to {todo_path}")

            except Exception:
                # Clean up temp file on error
                import os
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
                raise

        except Exception as e:
            self.logger.error(f"Failed to save todos to {todo_path}: {e}")
            raise

    # ===== Public Helper Methods (for Agent) =====

    def check_completion(self, session_id: str, user_id: Optional[str] = None) -> bool:
        """Check if all todos are completed for a session.

        Args:
            session_id: Session identifier
            user_id: Optional user identifier

        Returns:
            True if all todos are complete (or no todos exist)
        """
        todos = self._get_todos(session_id, user_id)

        # No todos = complete
        if not todos:
            return True

        # Check if all are completed
        return all(item.completed for item in todos)

    def get_progress(self, session_id: str, user_id: Optional[str] = None) -> Dict[str, int]:
        """Get progress statistics for a session.

        Args:
            session_id: Session identifier
            user_id: Optional user identifier

        Returns:
            Dictionary with total, completed, pending counts and percentage
        """
        todos = self._get_todos(session_id, user_id)

        total = len(todos)
        completed = sum(1 for item in todos if item.completed)
        pending = total - completed
        percentage = (completed / total * 100) if total > 0 else 100.0

        return {
            'total': total,
            'completed': completed,
            'pending': pending,
            'percentage': percentage
        }

    def get_all_tasks(self, session_id: str, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get all tasks as dictionaries.

        Args:
            session_id: Session identifier
            user_id: Optional user identifier

        Returns:
            List of task dictionaries
        """
        todos = self._get_todos(session_id, user_id)

        return [
            {
                'id': item.id,
                'text': item.text,
                'completed': item.completed,
                'priority': item.priority,
                'created_at': item.created_at.isoformat() if item.created_at else None,
                'completed_at': item.completed_at.isoformat() if item.completed_at else None
            }
            for item in todos
        ]

    # ===== Todo Actions =====

    @BaseTool.action(
        'List all todos in the current task',
        param_model=TodoListAction
    )
    async def todo_list(self, params: TodoListAction, execution_context=None) -> ActionResult:
        """List all todos for the current session.

        Args:
            params: Empty parameters (no params needed)
            execution_context: Execution context with session_id

        Returns:
            ActionResult with todo list and summary
        """
        await self.ensure_initialized()

        if not self._enabled:
            raise ServiceError(f"{self.name} service is not enabled")

        try:
            session_id = self._get_session_id(execution_context)
            user_id = self._get_user_id(execution_context)

            todos = self._get_todos(session_id, user_id)
            progress = self.get_progress(session_id, user_id)

            # Build summary
            result_text = (
                f"TODO List ({progress['completed']}/{progress['total']} complete, "
                f"{progress['percentage']:.0f}%)\n\n"
            )

            if todos:
                for item in todos:
                    status = "✓" if item.completed else "○"
                    result_text += f"{status} [{item.id}] {item.text}\n"
            else:
                result_text += "No todos yet. Use task_todo_add to create tasks."

            return ActionResult(extracted_content=result_text)

        except Exception as e:
            self.logger.error(f"Error listing todos: {e}")
            return ActionResult(error=f"Failed to list todos: {str(e)}", include_in_memory=True)

    @BaseTool.action(
        'Add a new todo item',
        param_model=TodoAddAction
    )
    async def todo_add(self, params: TodoAddAction, execution_context=None) -> ActionResult:
        """Add a new todo item to the list.

        Args:
            params: TodoAddAction with task text and priority
            execution_context: Execution context with session_id

        Returns:
            ActionResult with success message
        """
        await self.ensure_initialized()

        if not self._enabled:
            raise ServiceError(f"{self.name} service is not enabled")

        try:
            session_id = self._get_session_id(execution_context)
            user_id = self._get_user_id(execution_context)

            from agents.task.path import pm
            clean_id = pm().clean_session_id(session_id)

            # Get or initialize todos
            todos = self._get_todos(clean_id, user_id)

            # Initialize next_id if needed
            if clean_id not in self._next_id:
                self._next_id[clean_id] = 1

            # Create new todo item
            new_item = TodoItem(
                id=self._next_id[clean_id],
                text=params.text,
                completed=False
            )

            todos.append(new_item)
            self._next_id[clean_id] += 1

            # Save to file
            self._save_to_file(clean_id, user_id)

            self.logger.info(f"Added todo [{new_item.id}] for {clean_id[:8]}: {params.text}")
            return ActionResult(extracted_content=f"Added todo [{new_item.id}]: {params.text}")

        except Exception as e:
            self.logger.error(f"Error adding todo: {e}")
            return ActionResult(error=f"Failed to add todo: {str(e)}", include_in_memory=True)

    @BaseTool.action(
        'Mark a todo item as complete',
        param_model=TodoCompleteAction
    )
    async def todo_complete(self, params: TodoCompleteAction, execution_context=None) -> ActionResult:
        """Mark a todo item as complete.

        Args:
            params: TodoCompleteAction with pattern (ID or text to match)
            execution_context: Execution context with session_id

        Returns:
            ActionResult with progress update
        """
        await self.ensure_initialized()

        if not self._enabled:
            raise ServiceError(f"{self.name} service is not enabled")

        try:
            session_id = self._get_session_id(execution_context)
            user_id = self._get_user_id(execution_context)

            from agents.task.path import pm
            clean_id = pm().clean_session_id(session_id)

            todos = self._get_todos(clean_id, user_id)

            # Try to parse pattern as ID first
            try:
                task_id = int(params.pattern)
                match_by_id = True
            except (ValueError, TypeError):
                task_id = None
                match_by_id = False

            # Find and mark complete
            found = False
            matched_item = None

            for item in todos:
                if item.completed:
                    continue  # Skip already completed items

                # Match by ID if pattern is numeric
                if match_by_id and item.id == task_id:
                    matched_item = item
                    found = True
                    break

                # Match by text pattern (case-insensitive substring)
                if not match_by_id and params.pattern.lower() in item.text.lower():
                    matched_item = item
                    found = True
                    break

            if not found:
                error_msg = f"No incomplete todo found matching: {params.pattern}"
                self.logger.warning(error_msg)
                return ActionResult(error=error_msg, include_in_memory=True)

            # Mark complete
            matched_item.completed = True
            matched_item.completed_at = datetime.now()

            # Save to file
            self._save_to_file(clean_id, user_id)

            # Get updated progress
            progress = self.get_progress(clean_id, user_id)
            result_text = (
                f"Completed todo [{matched_item.id}]: {matched_item.text}. "
                f"Progress: {progress['completed']}/{progress['total']} "
                f"({progress['percentage']:.0f}%)"
            )

            self.logger.info(f"Completed todo [{matched_item.id}] for {clean_id[:8]}")
            return ActionResult(extracted_content=result_text)

        except Exception as e:
            self.logger.error(f"Error completing todo: {e}")
            return ActionResult(error=f"Failed to complete todo: {str(e)}", include_in_memory=True)

    @BaseTool.action(
        'Get current todo progress',
        param_model=TodoProgressAction
    )
    async def todo_progress(self, params: TodoProgressAction, execution_context=None) -> ActionResult:
        """Get current todo completion progress.

        Args:
            params: Empty parameters
            execution_context: Execution context with session_id

        Returns:
            ActionResult with progress statistics
        """
        await self.ensure_initialized()

        if not self._enabled:
            raise ServiceError(f"{self.name} service is not enabled")

        try:
            session_id = self._get_session_id(execution_context)
            user_id = self._get_user_id(execution_context)

            progress = self.get_progress(session_id, user_id)

            result_text = (
                f"Progress: {progress['completed']}/{progress['total']} "
                f"({progress['percentage']:.0f}%)"
            )

            return ActionResult(extracted_content=result_text)

        except Exception as e:
            self.logger.error(f"Error getting todo progress: {e}")
            return ActionResult(error=f"Failed to get progress: {str(e)}", include_in_memory=True)

    @BaseTool.action(
        'Get the next incomplete task',
        param_model=TodoNextAction
    )
    async def todo_next(self, params: TodoNextAction, execution_context=None) -> ActionResult:
        """Get the next incomplete task.

        Args:
            params: Empty parameters
            execution_context: Execution context with session_id

        Returns:
            ActionResult with next task or message if none
        """
        await self.ensure_initialized()

        if not self._enabled:
            raise ServiceError(f"{self.name} service is not enabled")

        try:
            session_id = self._get_session_id(execution_context)
            user_id = self._get_user_id(execution_context)

            todos = self._get_todos(session_id, user_id)

            # Find first incomplete task
            next_task = None
            for item in todos:
                if not item.completed:
                    next_task = item
                    break

            if next_task:
                result_text = f"Next task [{next_task.id}]: {next_task.text}"
            else:
                result_text = "No pending tasks - all todos complete!"

            return ActionResult(extracted_content=result_text)

        except Exception as e:
            self.logger.error(f"Error getting next todo: {e}")
            return ActionResult(error=f"Failed to get next task: {str(e)}", include_in_memory=True)

    # ===== Helper Methods =====

    def _get_session_id(self, execution_context) -> str:
        """Extract session_id from execution context.

        Args:
            execution_context: ActionExecutionContext or None

        Returns:
            Session ID string

        Raises:
            ValueError: If no session_id available
        """
        if execution_context and hasattr(execution_context, 'session_id') and execution_context.session_id:
            return execution_context.session_id
        elif hasattr(self, 'session_id') and self.session_id:
            return self.session_id
        else:
            raise ValueError("No session_id available for task tool operation")

    def _get_user_id(self, execution_context) -> Optional[str]:
        """Extract user_id from execution context.

        Args:
            execution_context: ActionExecutionContext or None

        Returns:
            User ID string or None
        """
        if execution_context and hasattr(execution_context, 'user_id'):
            return execution_context.user_id
        elif hasattr(self, 'user_id'):
            return self.user_id
        return None

    def get_session_todos(self, session_id: str, user_id: Optional[str] = None) -> Dict[str, Any]:
        """Get todo information for a session (for external queries).

        Args:
            session_id: Session identifier
            user_id: Optional user identifier

        Returns:
            Dictionary with todos and progress
        """
        try:
            return {
                'tasks': self.get_all_tasks(session_id, user_id),
                'progress': self.get_progress(session_id, user_id),
                'completed': self.check_completion(session_id, user_id)
            }
        except Exception as e:
            self.logger.error(f"Error getting session todos: {e}")
            return {'error': str(e)}
