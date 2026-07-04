"""
Telemetry Feed Formatters

Extracts feed formatting logic from ProductTelemetry into specialized formatter classes.
Each formatter handles one event type, converting telemetry events into feed-friendly formats.

Architecture:
- BaseFeedFormatter: Abstract base for all formatters
- Concrete formatters: One per event type (AgentStepFormatter, LLMRequestFormatter, etc.)
- FeedFormatterRegistry: Maps event names to formatters
"""

import logging
import time
from datetime import datetime
from typing import Dict, Any, Optional, List
from abc import ABC, abstractmethod

from agents.task.telemetry.views import BaseTelemetryEvent

# Import logger
from agents.task.logging_config import get_task_logger
logger = get_task_logger('telemetry.formatters')


class BaseFeedFormatter(ABC):
    """Base class for feed formatters.

    Each formatter converts a telemetry event into a feed-friendly format
    for consumption by web UI, dashboards, etc.
    """

    @abstractmethod
    def format(self, event: BaseTelemetryEvent) -> Dict[str, Any]:
        """Format event into feed structure.

        Args:
            event: The telemetry event to format

        Returns:
            Dictionary with feed-friendly structure
        """
        pass

    # Known tool/service names that namespace their actions
    KNOWN_TOOLS = {
        'polymarket', 'mcp', 'twitter', 'email', 'perplexity',
        'filesystem', 'browser', 'collabland', 'alchemy', 'task'
    }

    @staticmethod
    def _detect_service_for_action(action_name: str) -> str:
        """Detect which service an action belongs to based on its name.

        Uses a multi-tier detection strategy:
        1. Check for known tool prefix (e.g., 'polymarket_get_markets' → 'polymarket')
        2. Fall back to keyword-based detection for legacy actions
        3. Return 'default' if no match found

        Args:
            action_name: The name of the action

        Returns:
            The service name or 'default' if not detected
        """
        action_name_lower = action_name.lower()

        # Strategy 1: Check for known tool prefix pattern (tool_action)
        for tool in BaseFeedFormatter.KNOWN_TOOLS:
            if action_name_lower.startswith(f"{tool}_"):
                return tool

        # Strategy 2: Keyword-based detection for actions without prefix
        if 'perplexity' in action_name_lower or 'search_web' in action_name_lower:
            return 'perplexity'
        elif any(kw in action_name_lower for kw in ['document', 'doc_', 'extract_text', 'process_',
                                                'file', 'write_file', 'read_file', 'append_file',
                                                'delete_file', 'create_directory', 'list_directory']):
            return 'filesystem'
        elif any(kw in action_name_lower for kw in ['click', 'scroll', 'input', 'navigate', 'go_to',
                                                'browse_to', 'back', 'forward', 'reload', 'screenshot']):
            return 'browser'

        # Strategy 3: Try to extract prefix from first underscore
        # e.g., 'custom_tool_action' would detect 'custom_tool' if underscore found
        if '_' in action_name_lower:
            potential_tool = action_name_lower.split('_')[0]
            # Only use if it looks like a tool name (3+ chars, not a common verb)
            if len(potential_tool) >= 3 and potential_tool not in {'get', 'set', 'add', 'del', 'run'}:
                return potential_tool

        return 'default'


class AgentStepFormatter(BaseFeedFormatter):
    """Formatter for agent step events."""

    def format(self, event: BaseTelemetryEvent) -> Dict[str, Any]:
        """Format agent step event for feed."""
        # Process actions for feed
        processed_actions = []
        try:
            raw_actions = event.properties.get('actions', [])
            if raw_actions:
                for action in raw_actions:
                    try:
                        if isinstance(action, dict) and len(action) > 0:
                            action_keys = list(action.keys())
                            if not action_keys:
                                continue

                            action_name = action_keys[0]
                            service = self._detect_service_for_action(action_name)

                            # Strip service prefix from display name if present
                            # e.g., 'polymarket_get_markets' → 'get_markets' when service='polymarket'
                            display_name = action_name
                            if service != 'default':
                                prefix = f"{service}_"
                                if action_name.lower().startswith(prefix):
                                    display_name = action_name[len(prefix):]

                            # Extract parameters safely
                            parameters = {}
                            if action_name in action and action[action_name] is not None:
                                parameters = action[action_name]

                            processed_action = {
                                'action_type': action_name,  # Keep full name for execution
                                'name': display_name,        # Clean name for display
                                'service': service,
                                'params': parameters
                            }
                            processed_actions.append(processed_action)
                    except Exception as e:
                        logger.debug(f"Error processing action: {e}")
                        continue
        except Exception as e:
            logger.debug(f"Error processing actions: {e}")

        # Get context data if available
        context_data = event.properties.get('context_data', {})

        # Extract reasoning from multiple sources
        reasoning = ""
        if event.properties.get('reasoning'):
            reasoning = event.properties.get('reasoning')
        elif context_data and isinstance(context_data, dict):
            # Check outputs for reasoning
            if 'outputs' in context_data and isinstance(context_data['outputs'], dict):
                reasoning = context_data['outputs'].get('reasoning', '')

        # Check if there's any browser URL information
        browser_url = None
        if context_data and isinstance(context_data, dict):
            # Check inputs and outputs for URL info
            for section in ['inputs', 'outputs']:
                if section in context_data and isinstance(context_data[section], dict):
                    section_data = context_data[section]
                    for key in ['url', 'page_url', 'current_url']:
                        if key in section_data and section_data[key]:
                            browser_url = section_data[key]
                            break
                    if browser_url:
                        break

        # Check actions for URL information if we still don't have it
        if not browser_url and processed_actions:
            for action in processed_actions:
                if action['service'] == 'browser' and action['name'] in ['navigate_to', 'open_tab']:
                    if 'params' in action and 'url' in action['params']:
                        browser_url = action['params']['url']
                        break

        # Create step update data
        update_data = {
            'type': 'step',
            'step': event.properties.get('step', 0),
            'timestamp': time.time(),
            'datetime': datetime.now().isoformat(),
            'data': {
                'actions': processed_actions,
                'errors': event.properties.get('step_error', []),
                'consecutive_failures': event.properties.get('consecutive_failures', 0),
                'agent_name': event.properties.get('agent_name', 'Unknown'),
                'agent_type': event.properties.get('agent_type', 'Unknown'),
                'task_progress': event.properties.get('task_progress', ''),
                'current_task': event.properties.get('current_task', ''),
                'reasoning': reasoning,
                'context': context_data,
                # New iteration fields for enhanced UI rendering
                'iteration': event.properties.get('iteration') or event.properties.get('step', 0),
                'iteration_type': event.properties.get('iteration_type', 'mixed'),
                'iteration_status': event.properties.get('iteration_status', 'active'),
                'files_created': event.properties.get('files_created', []),
                'files_modified': event.properties.get('files_modified', []),
                'files_read': event.properties.get('files_read', []),
                'files_deleted': event.properties.get('files_deleted', []),
                'error_message': event.properties.get('error_message'),
                'loop_warning': event.properties.get('loop_warning'),
                'is_done': event.properties.get('is_done', False),
                'reasoning_synthesized': event.properties.get('reasoning_synthesized', False)
            }
        }

        # Ensure agent name is extracted from agent_id if not explicitly provided
        if update_data['data']['agent_name'] == 'Unknown' and event.properties.get('agent_id'):
            agent_id = event.properties.get('agent_id')
            if '_' in agent_id:
                prefix = agent_id.split('_')[0]
                update_data['data']['agent_name'] = prefix.capitalize()

        # Add agent name at root level for backwards compatibility
        update_data['agent_name'] = update_data['data']['agent_name']

        # Add browser URL information if available
        if browser_url:
            update_data['data']['page_url'] = browser_url
            update_data['data']['current_url'] = browser_url

        return update_data


class LLMRequestFormatter(BaseFeedFormatter):
    """Formatter for LLM request events."""

    @staticmethod
    def _detect_provider(model_name: str) -> str:
        """Detect the provider based on model name."""
        from agents.task.utils import detect_llm_provider
        provider = detect_llm_provider(None, model_name)
        return 'unknown' if provider == 'generic' else provider

    def format(self, event: BaseTelemetryEvent) -> Dict[str, Any]:
        """Format LLM request event for feed."""
        return {
            'type': 'llm_request',
            'timestamp': time.time(),
            'datetime': datetime.now().isoformat(),
            'data': {
                'component': event.properties.get('component', 'Unknown'),
                'purpose': event.properties.get('purpose', 'Unknown'),
                'model_name': event.properties.get('model_name', 'Unknown'),
                'provider': self._detect_provider(event.properties.get('model_name', '')),
                'duration_seconds': event.properties.get('duration_seconds', 0),
                'success': event.properties.get('success', False),
                'token_count': event.properties.get('token_count', None),
                'prompt_tokens': event.properties.get('prompt_tokens', None),
                'completion_tokens': event.properties.get('completion_tokens', None),
                'cost_estimate': event.properties.get('cost_estimate', None),
                'agent_id': event.properties.get('agent_id', None),
                'request_id': event.properties.get('request_id', None)
            }
        }


class SessionStartFormatter(BaseFeedFormatter):
    """Formatter for session start events."""

    def format(self, event: BaseTelemetryEvent) -> Dict[str, Any]:
        """Format session start event for feed."""
        task = event.properties.get('task', '')

        # If task is empty, try to get it from event directly
        if not task and hasattr(event, 'task'):
            task = event.task

        session_data = {
            'session_id': event.properties.get('session_id', 'unknown'),
            'task': task,
            'model_name': event.properties.get('model_name', 'Unknown'),
            'agent_id': event.properties.get('agent_id', ''),
            'agent_type': event.properties.get('agent_type', ''),
            'use_vision': event.properties.get('use_vision', False),
            'internal_planning': event.properties.get('internal_planning', True)
        }

        return {
            'type': 'session_start',
            'timestamp': time.time(),
            'datetime': datetime.now().isoformat(),
            'data': session_data
        }


class MultiAgentRelationshipFormatter(BaseFeedFormatter):
    """Formatter for multi-agent relationship events."""

    def format(self, event: BaseTelemetryEvent) -> Dict[str, Any]:
        """Format multi-agent relationship event for feed."""
        relationship_data = event.properties

        # Build enhanced agent data
        agent_details = []
        agent_types = relationship_data.get('agent_types', {})
        agent_sequence = relationship_data.get('execution_sequence',
                                        relationship_data.get('agent_sequence', []))

        for agent_id in relationship_data.get('agent_ids', []):
            agent_type = agent_types.get(agent_id, "Unknown")

            agent_name = "Unknown"
            if '_' in agent_id:
                prefix = agent_id.split('_')[0]
                agent_name = prefix.capitalize()

            sequence_position = -1
            if agent_id in agent_sequence:
                sequence_position = agent_sequence.index(agent_id)

            agent_details.append({
                'id': agent_id,
                'agent_id': agent_id,
                'name': agent_name,
                'agent_name': agent_name,
                'type': agent_type,
                'agent_type': agent_type,
                'model': relationship_data.get('agent_models', {}).get(agent_id, 'Unknown'),
                'sequence_position': sequence_position
            })

        # Sort by sequence position
        agent_details.sort(key=lambda x: x['sequence_position'] if x['sequence_position'] >= 0 else 999)

        return {
            'type': 'multi_agent_relationship',
            'timestamp': time.time(),
            'datetime': datetime.now().isoformat(),
            'data': {
                'agent_ids': relationship_data.get('agent_ids', []),
                'agent_types': relationship_data.get('agent_types', {}),
                'agent_sequence': relationship_data.get('agent_sequence', []),
                'agent_models': relationship_data.get('agent_models', {}),
                'orchestrator_type': relationship_data.get('orchestrator_type', 'default'),
                'agent_details': agent_details
            }
        }


class ControllerRegisteredFunctionsFormatter(BaseFeedFormatter):
    """Formatter for controller registered functions events.

    Transforms internal controller_registered_functions events into
    user-friendly available_actions format for the webview services panel.
    """

    def format(self, event: BaseTelemetryEvent) -> Dict[str, Any]:
        """Format controller registered functions as available actions."""
        properties = event.properties
        registered_functions = properties.get('registered_functions', [])

        # Group functions by service
        by_service = {}
        for func in registered_functions:
            if isinstance(func, dict):
                service = func.get('service', 'default')
                action_name = func.get('name', 'unknown')
            else:
                # Handle RegisteredFunction dataclass
                service = getattr(func, 'service', 'default')
                action_name = getattr(func, 'name', 'unknown')

            if service not in by_service:
                by_service[service] = []
            by_service[service].append(action_name)

        return {
            'type': 'available_actions',
            'timestamp': time.time(),
            'datetime': datetime.now().isoformat(),
            'data': {
                'by_service': by_service,
                'total_actions': len(registered_functions)
            }
        }


class ToolExecutionFormatter(BaseFeedFormatter):
    """Formatter for tool execution events."""

    def format(self, event: BaseTelemetryEvent) -> Dict[str, Any]:
        """Format tool execution event for feed."""
        return {
            'type': 'tool_execution',
            'timestamp': time.time(),
            'datetime': datetime.now().isoformat(),
            'step': event.properties.get('step', 0),
            'data': {
                'tool_name': event.properties.get('tool_name', 'Unknown'),
                'action_name': event.properties.get('action_name', 'Unknown'),
                'success': event.properties.get('success', False),
                'duration_seconds': event.properties.get('duration_seconds', 0),
                'error': event.properties.get('error'),
                'result_size': event.properties.get('result_size'),
                'result_truncated': event.properties.get('result_truncated', False),
                'result_preview': event.properties.get('result_preview'),
                'parameters': event.properties.get('parameters', {})
            }
        }


class ErrorFormatter(BaseFeedFormatter):
    """Formatter for error events."""

    def format(self, event: BaseTelemetryEvent) -> Dict[str, Any]:
        """Format error event for feed."""
        return {
            'type': 'error',
            'timestamp': time.time(),
            'datetime': datetime.now().isoformat(),
            'step': event.properties.get('step', 0),
            'data': {
                'error_type': event.properties.get('error_type', 'Unknown'),
                'error_message': event.properties.get('error_message', ''),
                'error_stack': event.properties.get('error_stack'),
                'recoverable': event.properties.get('recoverable', True),
                'context': event.properties.get('context', {})
            }
        }


class SessionCompletionFormatter(BaseFeedFormatter):
    """Formatter for session completion events."""

    def format(self, event: BaseTelemetryEvent) -> Dict[str, Any]:
        """Format session completion event for feed."""
        return {
            'type': 'session_completion',
            'timestamp': time.time(),
            'datetime': datetime.now().isoformat(),
            'data': {
                'success': event.properties.get('success', False),
                'total_steps': event.properties.get('total_steps', 0),
                'duration_seconds': event.properties.get('duration_seconds', 0),
                'error_message': event.properties.get('error_message'),
                'metrics': event.properties.get('metrics', {})
            }
        }


class GenericEventFormatter(BaseFeedFormatter):
    """Formatter for unrecognized/generic events.

    Provides a simple envelope with raw properties so UI can display unknown events.
    """

    def format(self, event: BaseTelemetryEvent) -> Dict[str, Any]:
        """Format generic event for feed."""
        return {
            'type': event.name,
            'timestamp': time.time(),
            'datetime': datetime.now().isoformat(),
            'data': dict(event.properties) if hasattr(event, 'properties') else {}
        }


class UserMessageFormatter(BaseFeedFormatter):
    """Formatter for user message feed events.

    Formats user messages for display in the chat UI.
    CRITICAL: Output type MUST be 'user_message' to match chat.js handleUserMessage().
    """

    def format(self, event: BaseTelemetryEvent) -> Dict[str, Any]:
        """Format user message event for chat display."""
        return {
            'type': 'user_message',
            'timestamp': time.time(),
            'datetime': datetime.now().isoformat(),
            'data': {
                'text': event.properties.get('text', ''),
                'message': event.properties.get('text', ''),  # Alias for compatibility
                'kind': event.properties.get('kind', 'comment'),
                'state': event.properties.get('state', 'queued'),
                'metadata': event.properties.get('metadata', {})
            }
        }


class QueueStatusFormatter(BaseFeedFormatter):
    """Formatter for queue status events.

    Formats queue status updates for the chat UI queue indicator.
    """

    def format(self, event: BaseTelemetryEvent) -> Dict[str, Any]:
        """Format queue status event for UI updates."""
        return {
            'type': 'queue_status',
            'timestamp': time.time(),
            'datetime': datetime.now().isoformat(),
            'data': {
                'queued_count': event.properties.get('queued_count', 0),
                'processing': event.properties.get('processing', False),
                'oldest_message_age_seconds': event.properties.get('oldest_message_age_seconds')
            }
        }


class SessionPausedFormatter(BaseFeedFormatter):
    """Formatter for session paused events."""

    def format(self, event: BaseTelemetryEvent) -> Dict[str, Any]:
        """Format session paused event for feed."""
        return {
            'type': 'session_paused',
            'timestamp': time.time(),
            'datetime': datetime.now().isoformat(),
            'data': {
                'session_id': event.properties.get('session_id', ''),
                'reason': event.properties.get('reason', 'user_interrupt'),
                'paused_by': event.properties.get('paused_by', 'user'),
                'status': 'paused'
            }
        }


class SessionResumedFormatter(BaseFeedFormatter):
    """Formatter for session resumed events."""

    def format(self, event: BaseTelemetryEvent) -> Dict[str, Any]:
        """Format session resumed event for feed."""
        return {
            'type': 'session_resumed',
            'timestamp': time.time(),
            'datetime': datetime.now().isoformat(),
            'data': {
                'session_id': event.properties.get('session_id', ''),
                'resumed_by': event.properties.get('resumed_by', 'user'),
                'status': 'running'
            }
        }


class SessionStatusFormatter(BaseFeedFormatter):
    """Formatter for generic session status events."""

    def format(self, event: BaseTelemetryEvent) -> Dict[str, Any]:
        """Format session status event for feed."""
        return {
            'type': 'status',
            'timestamp': time.time(),
            'datetime': datetime.now().isoformat(),
            'data': {
                'session_id': event.properties.get('session_id', ''),
                'status': event.properties.get('status', 'unknown'),
                'previous_status': event.properties.get('previous_status'),
                'reason': event.properties.get('reason')
            }
        }


class IterationCompleteFormatter(BaseFeedFormatter):
    """Formatter for iteration complete events.

    Provides a clean boundary marker for the UI to render iteration summaries
    with file chips and status indicators.
    """

    def format(self, event: BaseTelemetryEvent) -> Dict[str, Any]:
        """Format iteration complete event for feed."""
        props = event.properties

        return {
            'type': 'iteration_complete',
            'timestamp': time.time(),
            'datetime': datetime.now().isoformat(),
            'data': {
                'iteration': props.get('iteration', 0),
                'step': props.get('step', 0),
                'iteration_type': props.get('iteration_type', 'mixed'),
                'iteration_status': props.get('iteration_status', 'completed'),
                'reasoning_summary': props.get('reasoning_summary', ''),
                'actions': props.get('actions_executed', []),
                'action_results': props.get('action_results', []),
                'files_created': props.get('files_created', []),
                'files_modified': props.get('files_modified', []),
                'files_read': props.get('files_read', []),
                'files_deleted': props.get('files_deleted', []),
                'success': props.get('success', True),
                'error': props.get('error'),
                'is_done': props.get('is_done', False),
                'duration_seconds': props.get('duration_seconds', 0.0)
            }
        }


class FeedFormatterRegistry:
    """Registry mapping event names to formatters."""

    def __init__(self):
        self._formatters: Dict[str, BaseFeedFormatter] = {
            'agent_step': AgentStepFormatter(),
            'llm_request': LLMRequestFormatter(),
            'session_start': SessionStartFormatter(),
            'session_completion': SessionCompletionFormatter(),
            'multi_agent_relationship': MultiAgentRelationshipFormatter(),
            'multi_agent_relationship_detailed': MultiAgentRelationshipFormatter(),
            'session_relationship': MultiAgentRelationshipFormatter(),
            'controller_registered_functions': ControllerRegisteredFunctionsFormatter(),
            'tool_execution': ToolExecutionFormatter(),
            'error': ErrorFormatter(),
            # User message and queue status formatters for chat UI
            'user_message': UserMessageFormatter(),
            'queue_status': QueueStatusFormatter(),
            # Session state change formatters
            'session_paused': SessionPausedFormatter(),
            'session_resumed': SessionResumedFormatter(),
            'status': SessionStatusFormatter(),
            # Iteration complete formatter for enhanced UI
            'iteration_complete': IterationCompleteFormatter(),
        }
        self._generic_formatter = GenericEventFormatter()

    def get_formatter(self, event_name: str) -> BaseFeedFormatter:
        """Get formatter for event name.

        Args:
            event_name: The name of the event type

        Returns:
            Formatter for the event, or generic formatter if not registered
        """
        return self._formatters.get(event_name, self._generic_formatter)

    def register_formatter(self, event_name: str, formatter: BaseFeedFormatter) -> None:
        """Register a custom formatter.

        Args:
            event_name: The event name to register formatter for
            formatter: The formatter instance
        """
        self._formatters[event_name] = formatter


# Global registry instance
_registry = FeedFormatterRegistry()


def get_formatter_registry() -> FeedFormatterRegistry:
    """Get the global formatter registry."""
    return _registry
