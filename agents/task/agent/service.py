
from __future__ import annotations

import asyncio
import base64
import http
import importlib
import json
import logging
import os
import re
import subprocess
import traceback
import time
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Type, TypeVar, Union, Tuple
from collections import deque  # ADDED: Import deque for bounded collections
from agents.task.agent.tool_call_tracker import ToolCallTracker  # Robust tool call ID tracking

from dotenv import load_dotenv

# Import centralized constants
from agents.task.constants import (
    IMG_TOKENS,
    LoopDetectionConfig,
    DEFAULT_USER_ID,
    MemoryConfig,
    MAX_MCP_PER_STEP
)

# Import POLYROB exceptions
from core.exceptions import (
    AgentError,
    ValidationError as ROBValidationError,
    LLMResponseError,
    ToolError
)

# Import billing exception for fail-fast handling
from core.exceptions import InsufficientCreditsError

# Import centralized utilities to avoid repeated inline imports
from agents.task.utils_json import normalize_action_schema
from core.env import bool_env as _bool_env
# Model limits now come from modules.llm.model_registry

# Native message types
from modules.llm.messages import (
	AIMessage,
	BaseMessage,
	HumanMessage,
	SystemMessage,
	ToolMessage,
	MessageOrigin,
	make_control_message,
)
from modules.llm.adapters import BaseChatModel
from core.exceptions import (
    RateLimitError,
    LLMError,
    LLMRateLimitError,
    LLMAuthenticationError,
    LLMConnectionError,
    LLMContextLengthError,
    LLMResponseError,
    LLMPermanentError,
    LLMProviderExhaustedError
)
# PIL Image imported locally in save_screenshot() method where needed
from pydantic import BaseModel, ConfigDict, ValidationError

from tools.browser.views import BrowserStateHistory, BrowserState
from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.core.llm_runner import LLMRunnerMixin
from agents.task.agent.core.memory_writer import MemoryWriterMixin
from agents.task.agent.core.memory_prefetch import MemoryPrefetchMixin
from agents.task.agent.core.background_review import BackgroundReviewMixin
from agents.task.agent.core.step import StepMixin
from agents.task.agent.core.history_io import HistoryIOMixin
from agents.task.agent.core.logging_io import LoggingIOMixin
from agents.task.agent.core.safety_lifecycle import SafetyLifecycleMixin
from agents.task.agent.core.user_ingress import UserIngressMixin
from agents.task.agent.core.llm_provisioning import LLMProvisioningMixin
from agents.task.agent.core.model_swap import ModelSwapMixin
from agents.task.agent.core.model_introspection import ModelIntrospectionMixin
from agents.task.agent.core.loop_detection import LoopDetectionMixin
from agents.task.agent.core.turn_input import TurnInputMixin
from agents.task.agent.core.resources import ResourceMixin
from agents.task.agent.core.session_metadata import SessionMetadataMixin
from agents.task.agent.core.error_recovery import ErrorRecoveryMixin
from agents.task.agent.core.step_telemetry import StepTelemetryMixin
from agents.task.agent.core.output_validation import OutputValidationMixin
from agents.task.agent.core.result_processing import ResultProcessingMixin
from agents.task.agent.core.next_action_internal import NextActionInternalMixin
from agents.task.agent.core.step_execution import StepExecutionMixin
from agents.task.agent.core.run_loop import RunLoopMixin
from agents.task.agent.core.construction import AgentConstructionMixin
from agents.task.agent.prompts import SystemPrompt, AgentMessagePrompt
from agents.task.agent.views import (
    ActionModel,
    AgentBrain,
    AgentError,
    AgentHistory,
    AgentHistoryList,
    AgentOutput,
    AgentStepInfo,
    ActionResult,
)
from tools.browser.context import BrowserContext
from tools.dom.views import DOMElementNode, SelectorMap
from agents.task.telemetry.views import (
    HumanApprovalRequestedEvent,
    HumanApprovalDecisionEvent,
    TodoStatusEvent,
    AgentRunTelemetryEvent,
    AgentEndTelemetryEvent,
    ProviderFailureEvent,
    ProviderFallbackSuccessEvent,
)
# ProductTelemetry no longer directly used - accessed via TelemetryManager
from agents.task.utils import time_execution_async, detect_llm_provider, extract_token_usage
# Safely import Google API exceptions
try:
    from google.api_core.exceptions import ResourceExhausted
except ImportError:
    # Create a dummy exception class if google-api-core is not installed
    class ResourceExhausted(Exception):
        pass

# Import from our own logging config
from agents.task.logging_config import get_task_logger

# Import centralized path management
from agents.task.path import pm

load_dotenv()

# Generic logger for the module itself (not instances)
logger = get_task_logger('agent')

T = TypeVar('T', bound=BaseModel)



def _normalize_llm_content(content: Any) -> str:
	"""
	Normalize LLM response content to string format.
	
	Some providers (like Gemini) return content as a list of content blocks,
	while others (OpenAI, Anthropic) return it as a string.
	
	Args:
		content: LLM response content (str, list, or other)
		
	Returns:
		Normalized string content
	"""
	if content is None:
		return ""
	
	if isinstance(content, str):
		return content
	
	if isinstance(content, list):
		# Extract text from content blocks
		content_parts = []
		for block in content:
			if isinstance(block, str):
				content_parts.append(block)
			elif hasattr(block, 'text'):
				content_parts.append(block.text)
			elif isinstance(block, dict) and 'text' in block:
				content_parts.append(block['text'])
			else:
				content_parts.append(str(block))
		return "".join(content_parts)
	
	# Fallback to string conversion
	return str(content)


# Known tool/service names that namespace their actions
_KNOWN_TOOLS = {
	'polymarket', 'mcp', 'twitter', 'email', 'perplexity',
	'filesystem', 'browser', 'collabland', 'alchemy', 'task'
}


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
	for tool in _KNOWN_TOOLS:
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
	elif 'mcp' in action_name_lower:
		return 'mcp'

	# Strategy 3: Try to extract prefix from first underscore
	if '_' in action_name_lower:
		potential_tool = action_name_lower.split('_')[0]
		if len(potential_tool) >= 3 and potential_tool not in {'get', 'set', 'add', 'del', 'run'}:
			return potential_tool

	return 'default'


@dataclass
class AgentDeps:
	"""Injected collaborators an Agent needs (objects/callables, not config values).

	Split out of the old 31-param Agent.__init__ (PR9 item #2). Construct via
	Agent(config, deps), or use Agent.from_params(**kwargs) for the legacy kwarg
	interface.
	"""
	llm: "BaseChatModel"
	orchestrator: Any  # Required: provides session_id/user_id + infrastructure
	page_extraction_llm: Optional["BaseChatModel"] = None
	system_prompt_class: Type[SystemPrompt] = SystemPrompt
	register_new_step_callback: Optional[Callable[['BrowserState', 'AgentOutput', int], None]] = None
	register_done_callback: Optional[Callable[['AgentHistoryList'], None]] = None
	# UP-05: an explicit Controller to bind instead of orchestrator.controller — used
	# to give a delegated sub-agent a least-privilege child Controller. None => the
	# shared orchestrator controller (default, pre-UP-05 behaviour).
	controller: Optional[Any] = None


@dataclass
class AgentConfig:
	"""Scalar/behavioural configuration for an Agent (everything that isn't a
	collaborator). Defaults match the historical Agent.__init__ defaults exactly."""
	task: str
	use_vision: bool = True
	save_conversation_path: Optional[str] = None
	save_conversation_path_encoding: Optional[str] = 'utf-8'
	max_failures: int = 5
	retry_delay: int = 10
	max_input_tokens: Optional[int] = None  # model-specific limit if not provided
	# CO-F1: judge-backed output validation of the agent's final answer. Default OFF
	# (env `VALIDATE_OUTPUT`, core.env SSOT falsey-set); an explicit AgentConfig
	# kwarg still wins over the env default (this is a plain field, not a
	# default_factory, so callers that pass validate_output=... are unaffected).
	validate_output: bool = field(default_factory=lambda: _bool_env("VALIDATE_OUTPUT", False))
	generate_gif: bool | str = True
	sensitive_data: Optional[Dict[str, str]] = None
	available_file_paths: Optional[list[str]] = None
	include_attributes: Optional[list[str]] = None
	max_error_length: int = 400
	max_actions_per_step: int = 10
	initial_actions: Optional[List[Dict[str, Dict[str, Any]]]] = None
	tool_calling_method: Optional[str] = 'auto'
	agent_name: str = "agent"
	session_config: Optional[Dict[str, Any]] = None
	profile_id: Optional[str] = None
	profile_overrides: Optional[Dict[str, Any]] = None
	use_native_tools: bool = True
	step_timeout_seconds: Optional[int] = 600
	stall_timeout_seconds: Optional[int] = 600
	max_step_timeout: int = 900
	is_sub_agent: bool = False
	parent_session_id: Optional[str] = None
	role: str = "orchestrator"  # "orchestrator" may delegate; sub-agents get "leaf"
	persona_block: Optional[str] = None  # S1: chat-mode persona text → SystemPrompt <identity>


# Keys that belong to AgentDeps; everything else in a kwargs blob is AgentConfig.
_AGENT_DEP_KEYS = frozenset({
	'llm', 'orchestrator', 'page_extraction_llm', 'system_prompt_class',
	'register_new_step_callback', 'register_done_callback', 'controller',
})


class Agent(AgentConstructionMixin, RunLoopMixin, StepMixin, StepExecutionMixin, StepTelemetryMixin, ResultProcessingMixin, LLMRunnerMixin, NextActionInternalMixin, ErrorRecoveryMixin, OutputValidationMixin, MemoryWriterMixin, MemoryPrefetchMixin, BackgroundReviewMixin, HistoryIOMixin, LoggingIOMixin, SafetyLifecycleMixin, UserIngressMixin, TurnInputMixin, LLMProvisioningMixin, ModelSwapMixin, ModelIntrospectionMixin, LoopDetectionMixin, ResourceMixin, SessionMetadataMixin):
	@classmethod
	def from_params(cls, **kwargs) -> "Agent":
		"""Build an Agent from the legacy flat keyword arguments.

		Splits kwargs into AgentDeps (the collaborators in _AGENT_DEP_KEYS) and
		AgentConfig (everything else), then calls Agent(config, deps). This keeps
		every existing call site (orchestrator.create_agent, tests) working with a
		one-token change while Agent.__init__ itself takes just (config, deps).
		"""
		dep_kwargs = {k: kwargs.pop(k) for k in list(kwargs) if k in _AGENT_DEP_KEYS}
		config = AgentConfig(**kwargs)
		deps = AgentDeps(**dep_kwargs)
		return cls(config, deps)

	def _save_initial_task(self):
		"""Save the initial task to task.json for webview chat.

		Creates a task.json file in the session directory that contains:
		- task: The initial task description
		- timestamp: When the task was created
		- agent_name: Name of the agent handling the task

		This file is used by the webview chat tab to display the initial
		user message/task at the top of the conversation.
		"""
		try:
			from agents.task.path import pm

			session_id = pm().clean_session_id(self.session_id)
			session_dir = pm().get_data_dir(session_id, user_id=self.user_id)
			task_file = session_dir / "task.json"

			task_data = {
				"task": self.task,
				"timestamp": time.time(),
				"agent_name": self.agent_name
			}

			with open(task_file, 'w') as f:
				json.dump(task_data, f, indent=2)

			self.logger.info(f"✓ Saved task.json for session {session_id}")
		except Exception as e:
			self.logger.warning(f"Failed to save task.json: {e}")

	def _has_active_browser_usage(self) -> bool:
		"""Check if browser was ACTIVELY used in recent steps.
		
		FIX (Jan 2026): Simplified detection - only check recent action history.
		The AgentMessagePrompt._has_meaningful_browser_state() handles the 
		current state check. This method only checks if browser TOOLS were used.
		
		CRITICAL: Does NOT check:
		- browser_context existence (always exists for browser-capable agents)
		- task keywords (too aggressive, e.g. "parse URL from file")
		
		DOES check:
		- If recent actions include browser tool usage
		- If last browser state has a real URL (not empty/placeholder)
		
		Returns:
			True if browser was actively used recently, False otherwise
		"""
		# Check 1: Were browser tools used in recent actions?
		if hasattr(self, '_previous_actions') and self._previous_actions:
			browser_action_prefixes = ('browser_go_to', 'browser_click', 'browser_type', 
									   'browser_scroll', 'browser_screenshot', 
									   'browser_extract', 'browser_navigate')
			for action in self._previous_actions:
				action_name = str(action).lower() if action else ""
				if any(prefix in action_name for prefix in browser_action_prefixes):
					return True
		
		# Check 2: Does the last browser state have a real URL?
		if hasattr(self, '_last_browser_states') and self._last_browser_states:
			last_state = self._last_browser_states[-1] if self._last_browser_states else None
			if last_state and hasattr(last_state, 'url'):
				url = last_state.url
				# Real URL must be non-empty and not a placeholder
				if url and url.strip() and url != "about:blank":
					return True
		
		# Default: Browser not actively used
		return False

	@property
	def session_id(self) -> str:
		"""Get session_id from orchestrator (single source of truth)."""
		return self.orchestrator.session_id

	@property
	def effective_session_id(self) -> str:
		"""Get the effective session_id for this agent.
		
		For main agents: returns orchestrator.session_id
		For sub-agents: returns the virtual session_id (isolated context)
		
		Use this for action execution contexts and file paths.
		"""
		if self._is_sub_agent and hasattr(self, 'message_manager') and self.message_manager:
			# Sub-agents use their isolated session_id from message_manager
			return getattr(self.message_manager, 'session_id', self.orchestrator.session_id)
		return self.orchestrator.session_id

	@property
	def user_id(self) -> Optional[str]:
		"""Get user_id from orchestrator (single source of truth)."""
		return self.orchestrator.user_id

	@property
	def model_name(self) -> str:
		"""Get model_name from MessageManager (single source of truth)."""
		return self.message_manager.model_name

	@model_name.setter
	def model_name(self, value: str) -> None:
		"""Update model_name via MessageManager (for LLM fallback scenarios)."""
		self.message_manager.model_name = value

	@property
	def provider_name(self) -> str:
		"""Get provider_name from MessageManager (single source of truth)."""
		return self.message_manager.provider_name

	@provider_name.setter
	def provider_name(self, value: str) -> None:
		"""Update provider_name via MessageManager (for live model swaps).

		Mirrors the ``model_name`` setter: MessageManager is the SSOT and the
		Agent property is a thin mirror. Needed so ``swap_model`` can set the
		registry-canonical provider label authoritatively after a swap."""
		self.message_manager.provider_name = value

	@property
	def container(self) -> Optional[Any]:
		"""Get container from orchestrator (single source of truth).

		Returns:
			Container instance from orchestrator, or None if not available.

		Note:
			This property provides unified access to the container without
			duplicating storage. The orchestrator is the single owner.
		"""
		return self.orchestrator.container if self.orchestrator else None

	# LLM provisioning (_supports_streaming, _create_llm_from_config[_async],
	# set_token_limits, _get_model_max_completion_tokens, _reconcile_native_tools,
	# set_tool_calling_method) lives in core/llm_provisioning.py::LLMProvisioningMixin (P9).
	# REMOVED: _set_model_names() - deprecated; chat_model_library set in __init__;
	# model_name is a property delegating to MessageManager.

	def _save_session_config(self) -> None:
		"""Save the session configuration to the session directory for persistence and debugging."""
		if not self.session_config:
			return
		
		try:
			from agents.task.path import pm
			from pathlib import Path
			
			# Get session directory
			session_dir = pm().get_session_root(self.session_id)
			config_file = session_dir / "task_config.json"
			
			# Save configuration
			self.session_config.save(config_file)
			self.logger.info(f"Saved session config to {config_file}")
			
			# Also log a summary
			self.logger.info(f"Config summary:\n{self.session_config.summary()}")
			
		except Exception as e:
			self.logger.warning(f"Failed to save session config: {e}")

	def add_new_task(self, new_task: str) -> None:
		self.message_manager.add_new_task(new_task)

	async def _save_progress_on_timeout(self) -> None:
		"""Save progress when a step times out to enable recovery."""
		try:
			# Save current todo state using TaskTool
			if self.controller:
				task_tool = self.controller.get_tool('task')
				if task_tool and hasattr(task_tool, '_save_to_file'):
					try:
						task_tool._save_to_file(self.session_id)
						self.logger.info("Saved todo progress on timeout")
					except Exception as e:
						self.logger.warning(f"Failed to save todos on timeout: {e}")

			# Save message history checkpoint
			if hasattr(self, 'message_manager'):
				self.message_manager.checkpoint_history()
				self.logger.info("Checkpointed message history on timeout")

		except Exception as e:
			self.logger.warning(f"Failed to save progress on timeout: {e}")

	def _sanitize_text_for_log(self, text: str) -> str:
		"""Trim inline base64 images or very long strings for logging (P9: see log_sanitize)."""
		from agents.task.agent.log_sanitize import sanitize_text_for_log
		return sanitize_text_for_log(text)

	def _sanitize_structure_for_log(self, data):
		"""Recursively sanitise nested structures for logging (P9: see log_sanitize)."""
		from agents.task.agent.log_sanitize import sanitize_structure_for_log
		return sanitize_structure_for_log(data)

