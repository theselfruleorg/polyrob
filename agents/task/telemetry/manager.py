"""
TelemetryManager - Facade for telemetry operations

This manager provides a simplified interface for Agent and Orchestrator to capture
telemetry events without needing to understand ProductTelemetry's complexity.

Key responsibilities:
- Build telemetry events from agent state
- Hide ProductTelemetry implementation details
- Provide mockable interface for testing
- Single source of truth for event construction
"""

import logging
from typing import List, Dict, Any, Optional
from pathlib import Path

from agents.task.telemetry.service import ProductTelemetry
from agents.task.telemetry.views import (
    AgentStepTelemetryEvent,
    SessionStartTelemetryEvent,
    AgentRegistrationEvent,
    SessionCompletionTelemetryEvent,
    MultiAgentRelationshipEvent
)
from agents.task.path import pm

# Import logger
from agents.task.logging_config import get_task_logger
logger = get_task_logger('telemetry.manager')


class TelemetryManager:
    """
    Facade for all telemetry operations.

    Hides ProductTelemetry complexity and provides a clean interface for
    Agent and Orchestrator to capture telemetry without building events.
    """

    def __init__(self, session_id: str, agent_id: Optional[str] = None):
        """Initialize telemetry manager.

        Args:
            session_id: The session identifier (already cleaned by caller)
            agent_id: Optional agent identifier (format: name_session_id)
        """
        # Session ID is already cleaned by Orchestrator - do NOT re-clean
        self._session_id = session_id
        self._agent_id = agent_id
        self._service = ProductTelemetry()
        self.logger = logger

    def capture_step(
        self,
        step: int,
        actions: List[Dict[str, Any]],
        errors: List[str],
        brain_state: Any,
        consecutive_failures: int = 0,
        agent_name: Optional[str] = None,
        agent_type: Optional[str] = None,
        current_task: Optional[str] = None,
        inputs: Optional[Dict[str, Any]] = None,
        outputs: Optional[Dict[str, Any]] = None,
        metrics: Optional[Dict[str, Any]] = None,
        # New iteration fields for enhanced UI rendering
        iteration: Optional[int] = None,
        iteration_type: str = "mixed",
        iteration_status: str = "active",
        files_created: Optional[List[str]] = None,
        files_modified: Optional[List[str]] = None,
        files_read: Optional[List[str]] = None,
        files_deleted: Optional[List[str]] = None,
        error_message: Optional[str] = None,
        loop_warning: Optional[str] = None,
        is_done: bool = False,
        reasoning_synthesized: bool = False
    ) -> None:
        """Capture an agent step event.

        Agent calls this single method instead of building AgentStepTelemetryEvent.

        Args:
            step: Current step number
            actions: List of actions taken
            errors: List of error messages
            brain_state: Agent's brain state (AgentBrain or similar)
            consecutive_failures: Number of consecutive failures
            agent_name: Optional agent name (extracted from agent_id if not provided)
            agent_type: Optional agent type
            current_task: Optional current task description
            inputs: Optional input context
            outputs: Optional output context
            metrics: Optional metrics
            iteration: Logical iteration number (defaults to step)
            iteration_type: Type classification (thinking, browser, filesystem, mcp, mixed, done)
            iteration_status: Status (active, completed, partial, failed, thinking, paused, timeout, done)
            files_created: List of files created in this iteration
            files_modified: List of files modified in this iteration
            files_read: List of files read in this iteration
            files_deleted: List of files deleted in this iteration
            error_message: Error message if iteration failed
            loop_warning: Warning about detected loops
            is_done: Whether this iteration completed the task
            reasoning_synthesized: Whether reasoning was synthesized from results
        """
        try:
            # Extract task progress from brain state
            task_progress = ""
            if brain_state and hasattr(brain_state, 'next_goal'):
                task_progress = brain_state.next_goal

            # Extract reasoning from brain state or outputs
            reasoning = ""
            if brain_state and hasattr(brain_state, 'reasoning'):
                reasoning = brain_state.reasoning
            elif outputs and isinstance(outputs, dict):
                reasoning = outputs.get('reasoning', '')

            # Build context data
            context_data = {}
            if inputs or outputs or metrics:
                context_data = {
                    'inputs': inputs or {},
                    'outputs': outputs or {},
                    'metrics': metrics or {}
                }

            # Extract agent name if not provided
            if not agent_name and self._agent_id and '_' in self._agent_id:
                prefix = self._agent_id.split('_')[0]
                agent_name = prefix.capitalize()

            # Build the event with new iteration fields
            event = AgentStepTelemetryEvent(
                agent_id=self._agent_id or f"unknown_{self._session_id}",
                step=step,
                actions=actions,
                step_error=errors or [],
                consecutive_failures=consecutive_failures,
                agent_name=agent_name or 'Unknown',
                agent_type=agent_type or 'Agent',
                task_progress=task_progress,
                current_task=current_task or '',
                reasoning=reasoning,
                context_data=context_data,
                # New iteration fields
                iteration=iteration or step,
                iteration_type=iteration_type,
                iteration_status=iteration_status,
                files_created=files_created or [],
                files_modified=files_modified or [],
                files_read=files_read or [],
                files_deleted=files_deleted or [],
                error_message=error_message,
                loop_warning=loop_warning,
                is_done=is_done,
                reasoning_synthesized=reasoning_synthesized
            )

            # Capture to service
            self._service.capture(event, session_id=self._session_id)

        except Exception as e:
            self.logger.error(f"Failed to capture step telemetry: {e}", exc_info=True)

    def capture_iteration_complete(
        self,
        iteration: int,
        step: int,
        iteration_type: str,
        iteration_status: str,
        reasoning_summary: str,
        actions_executed: List[Dict[str, Any]],
        action_results: List[Dict[str, Any]],
        files_created: List[str],
        files_modified: List[str],
        files_read: List[str],
        files_deleted: List[str],
        success: bool,
        error: Optional[str] = None,
        is_done: bool = False,
        duration_seconds: float = 0.0
    ) -> None:
        """Capture an iteration complete event.

        Marks the end of an iteration with a full summary for UI rendering.

        Args:
            iteration: Logical iteration number
            step: Step number
            iteration_type: Type (thinking, browser, filesystem, mcp, mixed, done)
            iteration_status: Final status (completed, partial, failed, timeout, done)
            reasoning_summary: Summary of reasoning for this iteration
            actions_executed: List of actions that were executed
            action_results: Results for each action
            files_created: Files created in this iteration
            files_modified: Files modified in this iteration
            files_read: Files read in this iteration
            files_deleted: Files deleted in this iteration
            success: Whether the iteration succeeded
            error: Error message if failed
            is_done: Whether this iteration completed the task
            duration_seconds: Duration of the iteration
        """
        try:
            from agents.task.telemetry.views import IterationCompleteEvent

            event = IterationCompleteEvent(
                agent_id=self._agent_id or f"unknown_{self._session_id}",
                iteration=iteration,
                step=step,
                iteration_type=iteration_type,
                iteration_status=iteration_status,
                reasoning_summary=reasoning_summary,
                actions_executed=actions_executed,
                action_results=action_results,
                files_created=files_created,
                files_modified=files_modified,
                files_read=files_read,
                files_deleted=files_deleted,
                success=success,
                error=error,
                is_done=is_done,
                duration_seconds=duration_seconds
            )

            # Capture to service
            self._service.capture(event, session_id=self._session_id)

        except Exception as e:
            self.logger.error(f"Failed to capture iteration complete telemetry: {e}", exc_info=True)

    def capture_llm_call(
        self,
        component: str,
        purpose: str,
        model: str,
        duration: float,
        success: bool,
        token_count: Optional[int] = None,
        prompt_tokens: Optional[int] = None,
        completion_tokens: Optional[int] = None,
        parameters: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None
    ) -> str:
        """Capture an LLM call event.

        Args:
            component: Component making the call (e.g., 'agent', 'planner')
            purpose: Purpose of the call (e.g., 'planning', 'execution')
            model: Model name
            duration: Duration in seconds
            success: Whether the call succeeded
            token_count: Total token count
            prompt_tokens: Prompt token count
            completion_tokens: Completion token count
            parameters: Additional parameters
            error: Error message if failed

        Returns:
            Request ID for deduplication
        """
        try:
            return self._service.capture_llm_usage(
                component=component,
                purpose=purpose,
                model_name=model,
                duration_seconds=duration,
                success=success,
                token_count=token_count,
                session_id=self._session_id,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                parameters=parameters,
                agent_id=self._agent_id
            )
        except Exception as e:
            self.logger.error(f"Failed to capture LLM telemetry: {e}", exc_info=True)
            return ""

    def capture_llm_usage(
        self,
        component: str,
        purpose: str,
        model_name: str,
        duration_seconds: float,
        success: bool,
        token_count: Optional[int] = None,
        prompt_tokens: Optional[int] = None,
        completion_tokens: Optional[int] = None,
        cached_tokens: Optional[int] = None,
        parameters: Optional[Dict[str, Any]] = None,
        agent_id: Optional[str] = None
    ) -> str:
        """Capture LLM usage event (alias for capture_llm_call with additional cached_tokens support).

        This method provides direct interface compatibility with ProductTelemetry.capture_llm_usage().

        Args:
            component: Component making the call (e.g., 'agent', 'planner')
            purpose: Purpose of the call (e.g., 'planning', 'execution')
            model_name: Model name
            duration_seconds: Duration in seconds
            success: Whether the call succeeded
            token_count: Total token count
            prompt_tokens: Prompt token count
            completion_tokens: Completion token count
            cached_tokens: Cached token count (for prompt caching)
            parameters: Additional parameters
            agent_id: Optional agent ID override

        Returns:
            Request ID for deduplication
        """
        try:
            return self._service.capture_llm_usage(
                component=component,
                purpose=purpose,
                model_name=model_name,
                duration_seconds=duration_seconds,
                success=success,
                token_count=token_count,
                session_id=self._session_id,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cached_tokens=cached_tokens,
                parameters=parameters,
                agent_id=agent_id or self._agent_id
            )
        except Exception as e:
            self.logger.error(f"Failed to capture LLM usage telemetry: {e}", exc_info=True)
            return ""

    def capture_session_start(
        self,
        task: str,
        model: str,
        agent_type: Optional[str] = None,
        use_vision: bool = False,
        internal_planning: bool = True
    ) -> None:
        """Capture session start event.

        Args:
            task: The task description
            model: Model name
            agent_type: Optional agent type
            use_vision: Whether vision is enabled
            internal_planning: Whether internal planning is enabled
        """
        try:
            event = SessionStartTelemetryEvent(
                session_id=self._session_id,
                task=task,
                model_name=model,
                agent_id=self._agent_id or '',
                agent_type=agent_type or '',
                use_vision=use_vision,
                internal_planning=internal_planning
            )
            self._service.capture(event, session_id=self._session_id)
        except Exception as e:
            self.logger.error(f"Failed to capture session start: {e}", exc_info=True)

    def capture_session_end(
        self,
        success: bool,
        steps: int,
        duration: float,
        error_message: Optional[str] = None,
        metrics: Optional[Dict[str, Any]] = None
    ) -> None:
        """Capture session completion event.

        Args:
            success: Whether the session succeeded
            steps: Total number of steps
            duration: Session duration in seconds
            error_message: Optional error message
            metrics: Optional metrics
        """
        try:
            event = SessionCompletionTelemetryEvent(
                session_id=self._session_id,
                success=success,
                total_steps=steps,
                duration_seconds=duration,
                error_message=error_message,
                metrics=metrics
            )
            self._service.capture(event, session_id=self._session_id)
        except Exception as e:
            self.logger.error(f"Failed to capture session end: {e}", exc_info=True)

    def capture_agent_registration(
        self,
        agent_id: str,
        agent_name: str,
        agent_type: str,
        model_name: str,
        task: str
    ) -> None:
        """Capture agent registration event.

        Args:
            agent_id: Agent identifier
            agent_name: Agent name
            agent_type: Agent type
            model_name: Model name
            task: Task description
        """
        try:
            event = AgentRegistrationEvent(
                agent_id=agent_id,
                agent_name=agent_name,
                agent_type=agent_type,
                model_name=model_name,
                task=task,
                session_id=self._session_id
            )
            self._service.capture(event, session_id=self._session_id)
        except Exception as e:
            self.logger.error(f"Failed to capture agent registration: {e}", exc_info=True)

    def capture_multi_agent_relationship(
        self,
        agent_ids: List[str],
        agent_types: Dict[str, str],
        execution_sequence: List[str],
        agent_models: Optional[Dict[str, str]] = None,
        agent_details: Optional[List[Dict]] = None
    ) -> None:
        """Capture multi-agent relationship event.

        Args:
            agent_ids: List of agent IDs
            agent_types: Mapping of agent_id to agent_type
            execution_sequence: Order of execution
            agent_models: Optional mapping of agent_id to model
            agent_details: Optional detailed agent information
        """
        try:
            event = MultiAgentRelationshipEvent(
                agent_ids=agent_ids,
                agent_types=agent_types,
                execution_sequence=execution_sequence,
                agent_models=agent_models or {},
                agent_details=agent_details or [],
                session_id=self._session_id
            )
            self._service.capture(event, session_id=self._session_id)
        except Exception as e:
            self.logger.error(f"Failed to capture multi-agent relationship: {e}", exc_info=True)

    def flush_buffers(self) -> None:
        """Flush all buffered telemetry events to disk."""
        try:
            self._service.flush_all_buffers()
        except Exception as e:
            self.logger.error(f"Failed to flush telemetry buffers: {e}", exc_info=True)

    def get_health_stats(self) -> Dict[str, Any]:
        """Get telemetry health statistics.

        Returns:
            Dictionary containing health metrics
        """
        try:
            return self._service.get_health_stats()
        except Exception as e:
            self.logger.error(f"Failed to get health stats: {e}", exc_info=True)
            return {'status': 'error', 'error': str(e)}

    def capture_event(self, event: Any) -> None:
        """Capture arbitrary telemetry event.

        For events not yet having dedicated methods, use generic capture.

        Args:
            event: Any telemetry event
        """
        try:
            self._service.capture(event, session_id=self._session_id)
        except Exception as e:
            self.logger.error(f"Failed to capture event: {e}", exc_info=True)

    def capture_tool_started(
        self,
        step: int,
        tool_name: str,
        action_name: str,
        parameters: Dict[str, Any],
        call_id: Optional[str] = None,
        index: int = 0,
        total_in_batch: int = 1
    ) -> None:
        """Capture a tool-dispatch span-start event (019).

        Fired immediately before the action is awaited; pairs with the
        ``tool_execution`` completion event via ``call_id``.

        Args:
            step: Current step number
            tool_name: Name of the tool (e.g., 'filesystem', 'browser')
            action_name: Name of the action (e.g., 'read_file', 'click')
            parameters: Action parameters (sanitized downstream)
            call_id: LLM tool-call id or synthesized span id
            index: Position within the step's action batch
            total_in_batch: Number of actions in the batch
        """
        try:
            from agents.task.telemetry.views import ToolStartedEvent

            event = ToolStartedEvent(
                agent_id=self._agent_id or f"unknown_{self._session_id}",
                step=step,
                tool_name=tool_name,
                action_name=action_name,
                parameters=parameters,
                call_id=call_id,
                index=index,
                total_in_batch=total_in_batch,
                session_id=self._session_id
            )
            self._service.capture(event, session_id=self._session_id)
        except Exception as e:
            self.logger.error(f"Failed to capture tool started: {e}", exc_info=True)

    def capture_tool_execution(
        self,
        step: int,
        tool_name: str,
        action_name: str,
        parameters: Dict[str, Any],
        duration: float,
        success: bool,
        error: Optional[str] = None,
        result_size: Optional[int] = None,
        result_truncated: bool = False,
        result_preview: Optional[str] = None,
        call_id: Optional[str] = None
    ) -> None:
        """Capture a tool execution event.

        Args:
            step: Current step number
            tool_name: Name of the tool (e.g., 'filesystem', 'browser')
            action_name: Name of the action (e.g., 'read_file', 'click')
            parameters: Action parameters (will be sanitized)
            duration: Execution duration in seconds
            success: Whether execution succeeded
            error: Error message if failed
            result_size: Size of result in characters
            result_truncated: Whether result was truncated
            result_preview: Preview of result (first 200 chars)
        """
        try:
            from agents.task.telemetry.views import ToolExecutionTelemetryEvent
            
            event = ToolExecutionTelemetryEvent(
                agent_id=self._agent_id or f"unknown_{self._session_id}",
                step=step,
                tool_name=tool_name,
                action_name=action_name,
                parameters=parameters,
                duration_seconds=duration,
                success=success,
                error=error,
                result_size=result_size,
                result_truncated=result_truncated,
                result_preview=result_preview,
                session_id=self._session_id,
                call_id=call_id
            )
            self._service.capture(event, session_id=self._session_id)
        except Exception as e:
            self.logger.error(f"Failed to capture tool execution: {e}", exc_info=True)

    def capture_error(
        self,
        step: int,
        error_type: str,
        error_message: str,
        error_stack: Optional[str] = None,
        recoverable: bool = True,
        context: Optional[Dict[str, Any]] = None
    ) -> None:
        """Capture an error event.

        Args:
            step: Current step number
            error_type: Type of error ('tool_error', 'llm_error', 'validation_error', etc.)
            error_message: Error message
            error_stack: Stack trace (will be sanitized)
            recoverable: Whether error is recoverable
            context: Additional context
        """
        try:
            from agents.task.telemetry.views import ErrorTelemetryEvent
            
            event = ErrorTelemetryEvent(
                agent_id=self._agent_id or f"unknown_{self._session_id}",
                step=step,
                error_type=error_type,
                error_message=error_message,
                error_stack=error_stack,
                recoverable=recoverable,
                context=context or {},
                session_id=self._session_id
            )
            self._service.capture(event, session_id=self._session_id)
        except Exception as e:
            self.logger.error(f"Failed to capture error: {e}", exc_info=True)
