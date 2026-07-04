from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Dict, List, Optional, Union
import time
from datetime import datetime
from agents.task.path import pm


@dataclass
class BaseTelemetryEvent(ABC):
	@property
	@abstractmethod
	def name(self) -> str:
		pass

	@property
	def properties(self) -> Dict[str, Any]:
		try:
			# Import and check if the object is a dataclass before using asdict
			if is_dataclass(self):
				return {k: v for k, v in asdict(self).items() if k != 'name'}
			# Fallback for non-dataclass instances
			return {k: v for k, v in self.__dict__.items() if k != 'name' and not k.startswith('_')}
		except (TypeError, ValueError, ImportError):
			# Fallback for any errors or if is_dataclass is not available
			return {k: v for k, v in self.__dict__.items() if k != 'name' and not k.startswith('_')}

	def get_session_id(self) -> Optional[str]:
		"""Get the session ID associated with this event.
		
		Default implementation checks common attributes that might contain the session ID.
		Override this method in subclasses to provide more specific session ID extraction.
		
		Returns:
			The session ID or None if not found
		"""
		# Check for explicit session_id attribute
		if hasattr(self, 'session_id') and getattr(self, 'session_id'):
			# Session ID already cleaned by session creator
			return getattr(self, 'session_id')

		# Check for parent_session_id attribute (used in relationship events)
		if hasattr(self, 'parent_session_id') and getattr(self, 'parent_session_id'):
			# Session ID already cleaned by session creator
			return getattr(self, 'parent_session_id')
			
		# Try to extract from agent_id if available
		if hasattr(self, 'agent_id') and getattr(self, 'agent_id'):
			from agents.task.utils import extract_session_from_agent_id
			agent_id = getattr(self, 'agent_id')
			return extract_session_from_agent_id(agent_id)

		return None


@dataclass
class RegisteredFunction:
	name: str
	params: dict[str, Any]
	service: str = "default"  # Track which service the function belongs to


@dataclass
class ControllerRegisteredFunctionsTelemetryEvent(BaseTelemetryEvent):
	registered_functions: list[RegisteredFunction]
	name: str = 'controller_registered_functions'


@dataclass
class AgentStepTelemetryEvent(BaseTelemetryEvent):
	agent_id: str
	step: int
	step_error: list[str]
	consecutive_failures: int
	actions: list[dict]
	service_actions: Optional[list[str]] = None  # Track actions by service type
	action_details: Optional[Dict[str, Any]] = None  # Additional action details
	agent_name: Optional[str] = None  # Add agent name
	agent_type: Optional[str] = None  # Add agent type
	task_progress: Optional[str] = None  # Add task progress
	current_task: Optional[str] = None  # Add current task
	reasoning: Optional[str] = None  # Add reasoning field
	context_data: Optional[Dict[str, Any]] = None  # Additional context data

	# Iteration metadata for enhanced UI rendering
	iteration: Optional[int] = None  # Logical iteration number (1-indexed)
	iteration_type: str = "mixed"  # thinking, browser, filesystem, mcp, mixed, done
	iteration_status: str = "active"  # active, completed, partial, failed, thinking, cancelled, timeout, done

	# File tracking per iteration
	files_created: Optional[List[str]] = None
	files_modified: Optional[List[str]] = None
	files_read: Optional[List[str]] = None
	files_deleted: Optional[List[str]] = None

	# Error/warning info
	error_message: Optional[str] = None
	loop_warning: Optional[str] = None
	is_done: bool = False

	# Synthesis indicator
	reasoning_synthesized: bool = False

	name: str = 'agent_step'

	def __post_init__(self):
		"""Initialize optional fields and extract agent name from agent_id.

		Note: Service detection and action categorization moved to formatters.py
		where it belongs (presentation layer, not data layer).
		"""
		if self.service_actions is None:
			self.service_actions = []

		if self.action_details is None:
			self.action_details = {}

		# Extract agent name from agent_id if not explicitly provided
		if (not self.agent_name or self.agent_name == 'Unknown') and self.agent_id and '_' in self.agent_id:
			prefix = self.agent_id.split('_')[0]
			# Capitalize first letter for display
			self.agent_name = prefix.capitalize()


@dataclass
class AgentRunTelemetryEvent(BaseTelemetryEvent):
	agent_id: str
	use_vision: bool
	task: str
	model_name: str
	chat_model_library: str
	version: str
	source: str
	name: str = 'agent_run'


@dataclass
class AgentEndTelemetryEvent(BaseTelemetryEvent):
	agent_id: str
	steps: int
	max_steps_reached: bool
	success: bool
	errors: list[str]
	name: str = 'agent_end'


@dataclass
class LLMRequestTelemetryEvent(BaseTelemetryEvent):
	"""
	Telemetry event for LLM requests and usage.
	
	This is the canonical event class for tracking LLM requests and usage.
	It replaces both the old LLMRequestTelemetryEvent and LLMUsageTelemetryEvent
	classes to eliminate redundancy. Downstream collectors should be configured
	to handle events with the 'llm_request' name.
	"""
	component: str
	purpose: str
	model_name: str
	duration_seconds: float
	success: bool
	token_count: Optional[int] = None
	session_id: Optional[str] = None
	error: Optional[str] = None  # Add error field for error reporting
	prompt_tokens: Optional[int] = None  # For detailed token usage
	completion_tokens: Optional[int] = None  # For detailed token usage
	provider: Optional[str] = None  # Provider name (openai, anthropic, etc.)
	parameters: Optional[Dict[str, Any]] = None  # Additional parameters
	cost_estimate: Optional[float] = None  # Estimated cost of the request
	agent_id: Optional[str] = None  # Optional agent ID
	# Unique identifier for this LLM request so that downstream analytics can
	# safely deuplicate entries that originate from multiple telemetry
	# sources (e.g. llm_usage JSON + feed event).  When not provided by the
	# caller a UUID will be generated automatically inside
	# ProductTelemetry.capture_llm_usage().
	request_id: Optional[str] = None
	name: str = 'llm_request'
	
	def __post_init__(self):
		"""Initialize provider after dataclass initialization.
		
		This resolves the dataclass-vs-property collision by ensuring
		the provider field is set based on the model_name.
		"""
		# FIXED: Initialize provider field if not set
		if self.provider is None and self.model_name:
			# Use centralized provider detection directly
			from agents.task.utils import detect_llm_provider
			provider = detect_llm_provider(None, self.model_name)
			# Map 'generic' to 'unknown' for consistency with existing code
			self.provider = 'unknown' if provider == 'generic' else provider

		# Initialize parameters if needed
		if self.parameters is None:
			self.parameters = {}


@dataclass
class InterfaceTelemetryEvent(BaseTelemetryEvent):
	"""Telemetry event for UI/interface related events"""
	event_type: str  # e.g., 'view_loaded', 'session_created', 'screenshot_saved'
	session_id: str
	data: Optional[dict] = None
	name: str = 'interface_event'


# DEPRECATED: PlannerOutputTelemetryEvent - planning is now integrated into agent



@dataclass
class SessionRelationshipTelemetryEvent(BaseTelemetryEvent):
	"""Telemetry event for tracking parent-child agent relationships in multi-agent sessions"""
	parent_session_id: str
	agent_ids: List[str]
	agent_types: Dict[str, str]  # Maps agent_id -> agent_type
	agent_sequence: List[str]    # Order of execution
	orchestrator_type: str = "default"
	name: str = 'multi_agent_relationship'  # Changed from 'session_relationship' to match expected name
	
	def get_session_id(self) -> str:
		"""Get the session ID for this event"""
		return self.parent_session_id


class MultiAgentRelationshipEvent(BaseTelemetryEvent):
	"""Detailed telemetry event for multi-agent relationships."""
	
	def __init__(self, agent_ids: List[str], agent_types: Dict[str, str], execution_sequence: List[str], 
				  agent_details: List[Dict] = None, start_times: Dict[str, float] = None, 
				  end_times: Dict[str, float] = None, durations: Dict[str, float] = None,
				  agent_models: Dict[str, str] = None, session_id: Optional[str] = None):
		self.agent_ids = agent_ids
		self.agent_types = agent_types
		self.execution_sequence = execution_sequence
		self.agent_details = agent_details or []
		self.start_times = start_times or {}
		self.end_times = end_times or {}
		self.durations = durations or {}
		self.agent_models = agent_models or {}
		self.session_id = session_id  # Explicit session context when available
		
		# Ensure model information is added to agent_details if provided separately
		if self.agent_models and self.agent_details:
			for agent_detail in self.agent_details:
				agent_id = agent_detail.get('id') or agent_detail.get('agent_id')
				if agent_id and agent_id in self.agent_models:
					if not agent_detail.get('model'):
						agent_detail['model'] = self.agent_models[agent_id]

	@property
	def name(self) -> str:
		"""Get the name of this event. Implements the abstract property from BaseTelemetryEvent."""
		return 'multi_agent_relationship_detailed'
		
	@property
	def properties(self) -> Dict[str, Any]:
		"""Get the properties for the event.
		
		Returns:
			Dict with event properties
		"""
		props = {
			'agent_ids': self.agent_ids,
			'agent_types': self.agent_types,
			'execution_sequence': self.execution_sequence,
			'agent_details': self.agent_details,
			'agent_models': self.agent_models,  # Explicitly include agent_models
			'orchestrator_type': 'default'
		}
		
		# Add timing data if available
		if self.start_times:
			props['start_times'] = self.start_times
		if self.end_times:
			props['end_times'] = self.end_times
		if self.durations:
			props['durations'] = self.durations
			
		return props

	def get_session_id(self) -> Optional[str]:
		"""Return the session identifier for the event so it can be routed to the correct feed."""
		if self.session_id:
			return self.session_id
		# Fallback: derive from first agent id
		if self.agent_ids:
			first = self.agent_ids[0]
			if '_' in first:
				return first.split('_', 1)[1]
		return None


class SessionStartTelemetryEvent(BaseTelemetryEvent):
	"""Event for session start."""
	
	def __init__(self, session_id: str, task: str, model_name: str, *args, **kwargs):
		"""Initialize a session start event with task information.
		
		Args:
			session_id: The session ID
			task: The description of the task being executed
			model_name: The name of the model being used
		"""
		# Remove super() call since BaseTelemetryEvent has no __init__
		self.session_id = session_id
		self.task = task 
		self.model_name = model_name
		self._name = 'session_start'
		
		# Store additional attributes
		for key, value in kwargs.items():
			setattr(self, key, value)
		
	@property
	def name(self) -> str:
		"""Get the name of this event. Implements the abstract property from BaseTelemetryEvent."""
		return self._name
		
	@property
	def properties(self) -> Dict[str, Any]:
		"""Get the properties of this event."""
		# Build a comprehension of public attributes
		props = {
			'session_id': self.session_id,
			'task': self.task,
			'model_name': self.model_name
		}
		
		# Include other instance attributes that don't start with underscore
		for key, value in self.__dict__.items():
			if not key.startswith('_') and key not in props:
				props[key] = value
				
		return props
	
	def get_session_id(self) -> str:
		"""Get the session ID associated with this event."""
		return self.session_id


@dataclass
class SessionCompletionTelemetryEvent(BaseTelemetryEvent):
	"""Event for session completion."""
	session_id: str
	success: bool
	total_steps: int
	duration_seconds: float
	error_message: Optional[str] = None
	metrics: Optional[Dict[str, Any]] = None
	name: str = 'session_completion'
	
	def get_session_id(self) -> str:
		"""Get the session ID for this event"""
		return self.session_id


@dataclass
class ScreenshotSavedTelemetryEvent(BaseTelemetryEvent):
	"""Event for when a screenshot is saved to disk."""
	agent_id: str
	step: int
	screenshot_path: str
	name: str = 'screenshot_saved'


@dataclass
class AgentRegistrationEvent(BaseTelemetryEvent):
	"""Event for agent registration."""
	agent_id: str
	agent_name: str
	agent_type: str
	model_name: str
	task: str
	session_id: Optional[str] = None
	name: str = 'agent_registration'
	
	def __post_init__(self):
		"""Ensure model_name is properly set."""
		# Set a default model_name if it's empty
		if not self.model_name or self.model_name == "None":
			self.model_name = "Unknown"
	
	def get_session_id(self) -> Optional[str]:
		"""Get the session ID for this event"""
		if self.session_id:
			return self.session_id
		elif self.agent_id and '_' in self.agent_id:
			return self.agent_id.split('_', 1)[1]
		return None


@dataclass
class HumanApprovalRequestedEvent(BaseTelemetryEvent):
	"""Event for when human approval is requested for an action."""
	agent_id: str
	step: int
	reason: str
	required: bool = True
	payload: Optional[Dict[str, Any]] = None
	action_preview: Optional[List[str]] = None  # List of action names/types
	checkpoint_type: Optional[str] = None  # 'pre_action', 'destructive', 'final'
	timeout_seconds: Optional[int] = None
	name: str = 'human_approval_requested'
	
	def get_session_id(self) -> Optional[str]:
		"""Get the session ID for this event"""
		if hasattr(self, 'session_id') and getattr(self, 'session_id'):
			return getattr(self, 'session_id')
		elif self.agent_id and '_' in self.agent_id:
			return self.agent_id.split('_', 1)[1]
		return None


@dataclass
class HumanApprovalDecisionEvent(BaseTelemetryEvent):
	"""Event for when a human provides an approval decision."""
	agent_id: str
	step: int
	approved: bool
	note: Optional[str] = None
	decision_time_seconds: Optional[float] = None  # Time taken to make decision
	override_type: Optional[str] = None  # 'approve', 'reject', 'skip', 'edit'
	edited_params: Optional[Dict[str, Any]] = None  # If action params were edited
	name: str = 'human_approval_decision'
	
	def get_session_id(self) -> Optional[str]:
		"""Get the session ID for this event"""
		if hasattr(self, 'session_id') and getattr(self, 'session_id'):
			return getattr(self, 'session_id')
		elif self.agent_id and '_' in self.agent_id:
			return self.agent_id.split('_', 1)[1]
		return None


@dataclass
class UserGuidanceEvent(BaseTelemetryEvent):
	"""Event for when user provides guidance/message to agent."""
	agent_id: str
	kind: str  # 'message', 'todo_update', 'approval', 'correction'
	text: str
	injected_at: Optional[str] = None  # Where in agent flow it was injected
	processed: bool = False
	name: str = 'user_guidance'
	
	def get_session_id(self) -> Optional[str]:
		"""Get the session ID for this event"""
		if hasattr(self, 'session_id') and getattr(self, 'session_id'):
			return getattr(self, 'session_id')
		elif self.agent_id and '_' in self.agent_id:
			return self.agent_id.split('_', 1)[1]
		return None


@dataclass
class TodoStatusEvent(BaseTelemetryEvent):
	"""Event for TODO list status updates."""
	agent_id: str
	todos_total: int
	todos_completed: int
	todos_pending: int
	todo_items: Optional[List[Dict[str, Any]]] = None
	enforcement_triggered: bool = False  # If Done was blocked due to incomplete TODOs
	name: str = 'todo_status'
	
	def get_session_id(self) -> Optional[str]:
		"""Get the session ID for this event"""
		if hasattr(self, 'session_id') and getattr(self, 'session_id'):
			return getattr(self, 'session_id')
		elif self.agent_id and '_' in self.agent_id:
			return self.agent_id.split('_', 1)[1]
		return None


@dataclass
class AssistantMessageEvent(BaseTelemetryEvent):
	"""Event for chat-like assistant messages in HITL flow."""
	agent_id: str
	step: int
	message: str
	page_summary: Optional[str] = None
	next_goal: Optional[str] = None
	key_actions: Optional[List[str]] = None
	url: Optional[str] = None
	title: Optional[str] = None
	name: str = 'assistant_message'

	def get_session_id(self) -> Optional[str]:
		"""Get the session ID for this event"""
		if hasattr(self, 'session_id') and getattr(self, 'session_id'):
			return getattr(self, 'session_id')
		elif self.agent_id and '_' in self.agent_id:
			return self.agent_id.split('_', 1)[1]
		return None


@dataclass
class UserMessageDuringExecutionEvent(BaseTelemetryEvent):
	"""Event for user messages sent during agent execution (continuous chat).

	This is for analytics/telemetry purposes. For UI display, use UserMessageFeedEvent.
	"""
	agent_id: str
	step: int
	message_text: str  # Truncated for privacy
	message_kind: str
	queue_depth: int
	execution_phase: str  # "running", "browser_action", "llm_call"
	name: str = 'user_message_during_execution'

	def get_session_id(self) -> Optional[str]:
		"""Get the session ID for this event"""
		if hasattr(self, 'session_id') and getattr(self, 'session_id'):
			return getattr(self, 'session_id')
		elif self.agent_id and '_' in self.agent_id:
			return self.agent_id.split('_', 1)[1]
		return None


@dataclass
class UserMessageFeedEvent(BaseTelemetryEvent):
	"""Event for user messages - rendered in chat UI.

	This event is specifically for the feed/chat rendering.
	Separate from UserMessageDuringExecutionEvent which is for analytics.

	CRITICAL: The 'name' field MUST be 'user_message' to match chat.js expectations.
	"""
	agent_id: str
	text: str
	kind: str = "comment"  # comment, guidance, correction
	step: int = 0
	metadata: Optional[Dict[str, Any]] = None
	state: str = "queued"  # queued, injected, processed
	name: str = 'user_message'  # CRITICAL: Must match chat.js handleUserMessage()

	def get_session_id(self) -> Optional[str]:
		"""Get the session ID for this event"""
		if hasattr(self, 'session_id') and getattr(self, 'session_id'):
			return getattr(self, 'session_id')
		elif self.agent_id and '_' in self.agent_id:
			return self.agent_id.split('_', 1)[1]
		return None


@dataclass
class QueueStatusEvent(BaseTelemetryEvent):
	"""Event for message queue status updates.

	Emitted when the message queue changes to update UI indicators.
	"""
	agent_id: str
	queued_count: int
	processing: bool = False
	oldest_message_age_seconds: Optional[float] = None
	name: str = 'queue_status'

	def get_session_id(self) -> Optional[str]:
		"""Get the session ID for this event"""
		if hasattr(self, 'session_id') and getattr(self, 'session_id'):
			return getattr(self, 'session_id')
		elif self.agent_id and '_' in self.agent_id:
			return self.agent_id.split('_', 1)[1]
		return None


@dataclass
class StreamingOutputEvent(BaseTelemetryEvent):
	"""Event for streaming LLM output in continuous chat."""
	agent_id: str
	step: int
	total_chunks: int
	total_chars: int
	provider: str
	duration_seconds: float
	callbacks_count: int
	callback_failures: int
	name: str = 'streaming_output'

	def get_session_id(self) -> Optional[str]:
		"""Get the session ID for this event"""
		if hasattr(self, 'session_id') and getattr(self, 'session_id'):
			return getattr(self, 'session_id')
		elif self.agent_id and '_' in self.agent_id:
			return self.agent_id.split('_', 1)[1]
		return None


@dataclass
class AgentQuestionEvent(BaseTelemetryEvent):
	"""Event for agent-initiated questions to user."""
	agent_id: str
	step: int
	question_prompt: str  # Truncated
	response_time_seconds: float
	timed_out: bool
	user_response: Optional[str]  # Truncated
	name: str = 'agent_question'

	def get_session_id(self) -> Optional[str]:
		"""Get the session ID for this event"""
		if hasattr(self, 'session_id') and getattr(self, 'session_id'):
			return getattr(self, 'session_id')
		elif self.agent_id and '_' in self.agent_id:
			return self.agent_id.split('_', 1)[1]
		return None


@dataclass
class PostCompletionContinuationEvent(BaseTelemetryEvent):
	"""Event for user continuing task after agent completion."""
	agent_id: str
	final_step: int
	continuation_message: str  # Truncated
	delay_seconds: float  # Time between done and message
	new_task_count: int  # Steps after continuation
	name: str = 'post_completion_continuation'

	def get_session_id(self) -> Optional[str]:
		"""Get the session ID for this event"""
		if hasattr(self, 'session_id') and getattr(self, 'session_id'):
			return getattr(self, 'session_id')
		elif self.agent_id and '_' in self.agent_id:
			return self.agent_id.split('_', 1)[1]
		return None


@dataclass
class ToolExecutionTelemetryEvent(BaseTelemetryEvent):
	"""Event for individual tool executions to track performance and results.
	
	This event captures granular details about each tool execution including:
	- Which tool/action was executed
	- Parameters passed
	- Execution timing
	- Success/failure status
	- Result size and truncation
	"""
	agent_id: str
	step: int
	tool_name: str  # e.g., 'filesystem', 'browser', 'task'
	action_name: str  # e.g., 'read_file', 'click', 'task_todo_list'
	parameters: Dict[str, Any]  # Action parameters (sanitized)
	duration_seconds: float  # Execution time
	success: bool  # Whether execution succeeded
	error: Optional[str] = None  # Error message if failed
	result_size: Optional[int] = None  # Size of result in characters
	result_truncated: bool = False  # Whether result was truncated
	result_preview: Optional[str] = None  # First 200 chars of result
	session_id: Optional[str] = None  # Explicit session ID
	name: str = 'tool_execution'
	
	def get_session_id(self) -> Optional[str]:
		"""Get the session ID for this event"""
		if self.session_id:
			return self.session_id
		elif self.agent_id and '_' in self.agent_id:
			return self.agent_id.split('_', 1)[1]
		return None


@dataclass
class ErrorTelemetryEvent(BaseTelemetryEvent):
	"""Event for tracking errors with context.
	
	Provides dedicated error tracking separate from step events to enable:
	- Error analytics and dashboards
	- Error rate monitoring
	- Root cause analysis
	- Recovery tracking
	"""
	agent_id: str
	step: int
	error_type: str  # 'tool_error', 'llm_error', 'validation_error', 'timeout', etc.
	error_message: str  # Error message
	error_stack: Optional[str] = None  # Stack trace (sanitized)
	recoverable: bool = True  # Whether error is recoverable
	context: Optional[Dict[str, Any]] = None  # Additional context
	session_id: Optional[str] = None  # Explicit session ID
	name: str = 'error'
	
	def get_session_id(self) -> Optional[str]:
		"""Get the session ID for this event"""
		if self.session_id:
			return self.session_id
		elif self.agent_id and '_' in self.agent_id:
			return self.agent_id.split('_', 1)[1]
		return None


@dataclass
class VisionContentInjectedEvent(BaseTelemetryEvent):
	"""Track when vision content is injected into agent context."""
	agent_id: str
	step: int
	image_count: int
	image_sources: List[str]  # ['msg_0', 'msg_1', ...]
	model_name: str
	vision_supported: bool
	injection_position: int
	survived_recalibration: bool
	
	name: str = "vision_content_injected"


@dataclass
class VisionResponseAnalyzedEvent(BaseTelemetryEvent):
	"""Track LLM response analysis for vision content."""
	agent_id: str
	step: int
	expected_image_count: int
	mentioned_visual_content: bool
	response_length: int
	model_name: str
	vision_keywords_found: List[str]  # Which keywords were detected
	
	name: str = "vision_response_analyzed"


@dataclass
class ProviderFailureEvent(BaseTelemetryEvent):
	"""Telemetry event for LLM provider failures.
	
	This event is emitted when an LLM provider fails and a fallback is attempted.
	Used for monitoring provider health, tracking failure patterns, and 
	understanding fallback behavior.
	
	Attributes:
		failed_provider: Provider that failed (e.g., 'openai', 'anthropic')
		failed_model: Model that was being used when failure occurred
		error_type: Type of error (e.g., 'LLMRateLimitError', 'LLMAuthenticationError')
		error_message: Truncated error message
		fallback_provider: Provider selected for fallback (None if no fallback available)
		fallback_model: Model selected for fallback
		fallback_success: Whether the fallback attempt succeeded
		attempt_number: Which attempt this was (1 = first try, 2+ = fallback attempts)
		session_id: Session where failure occurred
		agent_id: Agent that encountered the failure
		step: Step number when failure occurred
	"""
	failed_provider: str
	failed_model: str
	error_type: str
	error_message: str
	fallback_provider: Optional[str] = None
	fallback_model: Optional[str] = None
	fallback_success: Optional[bool] = None
	attempt_number: int = 1
	session_id: Optional[str] = None
	agent_id: Optional[str] = None
	step: Optional[int] = None
	timestamp: Optional[float] = None
	name: str = 'provider_failure'
	
	def __post_init__(self):
		"""Initialize timestamp if not provided."""
		if self.timestamp is None:
			self.timestamp = time.time()
	
	def get_session_id(self) -> Optional[str]:
		"""Get the session ID for this event"""
		if self.session_id:
			return self.session_id
		elif self.agent_id and '_' in self.agent_id:
			return self.agent_id.split('_', 1)[1]
		return None


@dataclass
class ProviderFallbackSuccessEvent(BaseTelemetryEvent):
	"""Telemetry event for successful provider fallback.

	Emitted when a fallback provider successfully handles a request
	after the primary provider failed.
	"""
	original_provider: str
	original_model: str
	fallback_provider: str
	fallback_model: str
	original_error_type: str
	total_attempts: int
	total_fallback_time_seconds: float
	session_id: Optional[str] = None
	agent_id: Optional[str] = None
	step: Optional[int] = None
	name: str = 'provider_fallback_success'

	def get_session_id(self) -> Optional[str]:
		"""Get the session ID for this event"""
		if self.session_id:
			return self.session_id
		elif self.agent_id and '_' in self.agent_id:
			return self.agent_id.split('_', 1)[1]
		return None


@dataclass
class SessionPausedEvent(BaseTelemetryEvent):
	"""Event emitted when a session is paused by user.

	Used for audit trail and UI synchronization.
	"""
	session_id: str
	reason: str = "user_interrupt"  # user_interrupt, timeout, error
	paused_by: str = "user"  # user, system, admin
	name: str = 'session_paused'

	def get_session_id(self) -> Optional[str]:
		return self.session_id


@dataclass
class SessionResumedEvent(BaseTelemetryEvent):
	"""Event emitted when a session is resumed by user.

	Used for audit trail and UI synchronization.
	"""
	session_id: str
	resumed_by: str = "user"  # user, system, admin
	name: str = 'session_resumed'

	def get_session_id(self) -> Optional[str]:
		return self.session_id


@dataclass
class SessionStatusEvent(BaseTelemetryEvent):
	"""Generic status event for session state changes.

	Used for UI synchronization via feed events.
	"""
	session_id: str
	status: str  # running, paused, completed, error
	previous_status: Optional[str] = None
	reason: Optional[str] = None
	name: str = 'status'

	def get_session_id(self) -> Optional[str]:
		return self.session_id


@dataclass
class IterationCompleteEvent(BaseTelemetryEvent):
	"""Event emitted when an agent iteration completes.

	This provides a clean boundary marker for the UI to know
	when one iteration ends and another begins. Contains full
	summary of what happened in the iteration.
	"""
	agent_id: str
	iteration: int
	step: int

	# Iteration classification
	iteration_type: str  # thinking, browser, filesystem, mcp, mixed, done
	iteration_status: str  # completed, partial, failed, timeout, done

	# Summary
	reasoning_summary: str
	actions_executed: List[Dict[str, Any]]
	action_results: List[Dict[str, Any]]  # Success/failure per action

	# Files touched (empty lists if none)
	files_created: List[str]
	files_modified: List[str]
	files_read: List[str]
	files_deleted: List[str]

	# Status
	success: bool
	error: Optional[str] = None
	is_done: bool = False

	# Timing
	duration_seconds: float = 0.0

	name: str = 'iteration_complete'

	def get_session_id(self) -> Optional[str]:
		if self.agent_id and '_' in self.agent_id:
			return self.agent_id.split('_', 1)[1]
		return None
