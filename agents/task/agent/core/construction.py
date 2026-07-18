
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

def _resolve_generate_gif(config_value, gif_enabled: bool):
    """Gate GIF creation behind the ENABLE_GIF_CREATION operator flag (live-test F-GIF).

    The flag (default false) is the master switch. When OFF, suppress GIF creation
    entirely — a headless/autonomous server has no UI consumer for them. When ON,
    honor the config value (a bool, or a str output path).
    """
    return config_value if gif_enabled else False


def _bump_eager_skill_usage(matched_skills, user_id, logger) -> None:
    """SK-F3: record a provenance load for every eager-injected skill.

    With SKILL_PROGRESSIVE_DISCLOSURE off, skills are embedded full-body directly
    into the system message and the agent never calls ``load_skill`` — the only
    other place usage is bumped (``action_registration.py``'s ``load_skill``
    closure). Without this, the curator (W5) sees zero loads for an authored
    skill that IS actively guiding the agent every turn and archives it as
    unused. Pure module-level function so it's unit-testable without
    constructing a full ``Agent``. Fail-open — a metrics write must never break
    skill injection.
    """
    if not user_id:
        return
    try:
        from modules.skills.skill_usage import get_skill_usage_store
        _usage_store = get_skill_usage_store()
        for _s in matched_skills:
            _usage_store.bump_load(_s.skill_id, user_id)
    except Exception as _bump_err:
        logger.debug(f"eager skill usage bump skipped (non-fatal): {_bump_err}")


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
from agents.task.agent.core.step import StepMixin
from agents.task.agent.core.history_io import HistoryIOMixin
from agents.task.agent.core.logging_io import LoggingIOMixin
from agents.task.agent.core.safety_lifecycle import SafetyLifecycleMixin
from agents.task.agent.core.user_ingress import UserIngressMixin
from agents.task.agent.core.llm_provisioning import LLMProvisioningMixin
from agents.task.agent.core.model_introspection import ModelIntrospectionMixin
from agents.task.agent.core.loop_detection import LoopDetectionMixin
from agents.task.agent.core.resources import ResourceMixin
from agents.task.agent.core.session_metadata import SessionMetadataMixin
from agents.task.agent.core.error_recovery import ErrorRecoveryMixin
from agents.task.agent.core.step_telemetry import StepTelemetryMixin
from agents.task.agent.core.output_validation import OutputValidationMixin
from agents.task.agent.core.result_processing import ResultProcessingMixin
from agents.task.agent.core.next_action_internal import NextActionInternalMixin
from agents.task.agent.core.step_execution import StepExecutionMixin
from agents.task.agent.core.run_loop import RunLoopMixin
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


def resolve_profile_overrides(updated_config, *, tool_calling_method,
                              max_actions_per_step, max_input_tokens, max_failures):
    """Resolve profile overrides onto the four agent scalars they may set.

    Replaces the old ``for key in [...]: locals()[key] = updated_config[key]`` loop,
    which was a Python no-op (assigning to ``locals()`` does not rebind locals), so
    these profile overrides were silently dropped. A present key in ``updated_config``
    wins; otherwise the current value is kept. Returns the four values in fixed order.
    """
    return (
        updated_config.get("tool_calling_method", tool_calling_method),
        updated_config.get("max_actions_per_step", max_actions_per_step),
        updated_config.get("max_input_tokens", max_input_tokens),
        updated_config.get("max_failures", max_failures),
    )


class AgentConstructionMixin:
	"""Agent.__init__ (the ~660L constructor) split whole out of the Agent class so
	service.py drops under 700L (P9). Agent inherits __init__ from this mixin; from_params
	(in service.py) still builds AgentConfig/AgentDeps and calls cls(config, deps). Imports
	above are service.py's full set so every name the constructor references resolves."""

	def _load_project_context(self) -> None:
		"""Auto-load the frozen PROJECT_CONTEXT foundation message (C9, P7 finalization:
		extracted from __init__).
		  - Local CLI single-owner: loaded TRUSTED (steering), gated on
		    project_context_autoload() (default ON under POLYROB_LOCAL).
		  - Server: loaded ONLY under project_context_server_mode(), then injected
		    UNTRUSTED-WRAPPED (framed as DATA) since the repo may be one merely opened.
		Fully fail-open — any error loads nothing. Server byte-identical by default."""
		try:
			from agents.task.constants import AutonomyConfig, local_mode_enabled
			from agents.task.agent.core.project_context import build_project_context_message
			_local = local_mode_enabled()
			# Server tier searches the tenant's session workspace, NEVER the process
			# CWD (install dir); on any failure leave it None so server-mode loads nothing.
			_workspace_dir = None
			if not _local:
				try:
					from agents.task.path import pm
					_workspace_dir = str(pm().get_workspace_dir(
						self.orchestrator.session_id,
						getattr(self.orchestrator, "user_id", None),
					))
				except Exception:
					_workspace_dir = None
			_proj_ctx = build_project_context_message(
				local=_local,
				autoload=AutonomyConfig.project_context_autoload(),
				server_mode=AutonomyConfig.project_context_server_mode(),
				cwd=os.getcwd(),
				workspace_dir=_workspace_dir,
				cap_tokens=AutonomyConfig.project_context_max_tokens(),
			)
			self.message_manager.set_project_context_message(_proj_ctx)
		except Exception as e:
			self.logger.debug(f"Could not load project context (non-fatal): {e}")

	def _restore_tool_call_tracker(self, orchestrator) -> None:
		"""Restore saved tool-call-tracker state from a previous session (P7
		finalization: extracted from __init__). Best-effort — reads self.session_id /
		self.tool_call_tracker, fail-open on any error."""
		if not (self.session_id and orchestrator):
			return
		try:
			from agents.task.path import pm
			tool_calls_file = pm().create_file_path(
				session_id=self.session_id,
				subdir_name="data",
				filename="tool_calls.json",
				user_id=orchestrator.user_id if hasattr(orchestrator, 'user_id') else None
			)
			if tool_calls_file.exists():
				if self.tool_call_tracker.load_from_file(tool_calls_file):
					self.logger.info("📂 Restored tool call tracker state from previous session")
		except Exception as e:
			self.logger.debug(f"No tool call tracker state to restore: {e}")

	def _normalize_save_conversation_path(self, save_conversation_path) -> None:
		"""Normalize + store save_conversation_path via the path manager (P7
		finalization: extracted from __init__). Sets self.save_conversation_path to a
		pm()-resolved logs path, or None when unset."""
		if save_conversation_path:
			from agents.task.path import pm
			self.save_conversation_path = str(pm().create_file_path(
				self.orchestrator.session_id,
				"logs",
				os.path.basename(save_conversation_path) or "conversation",
				user_id=self.orchestrator.user_id,
			))
			self.logger.info(f"Using normalized conversation path: {self.save_conversation_path}")
		else:
			self.save_conversation_path = None

	def _setup_memory_profiling(self) -> None:
		"""Optional tracemalloc profiling behind PROFILE_MEM=1 (P7 finalization:
		extracted from __init__). Self-contained — reads only self.logger and
		registers an atexit dump; a no-op when the flag is off."""
		if os.getenv("PROFILE_MEM") != "1":
			return
		try:
			import tracemalloc
			import atexit
			import pprint

			tracemalloc.start()
			self.logger.info("Memory profiling enabled - tracemalloc started")

			@atexit.register
			def _dump_memory_stats():
				try:
					top = tracemalloc.take_snapshot().statistics("lineno")[:20]
					self.logger.info("Top 20 memory allocations:")
					pprint.pprint(top)
				except Exception as e:
					self.logger.warning(f"Error dumping memory stats: {e}")
		except ImportError:
			self.logger.warning("tracemalloc not available for memory profiling")
		except Exception as e:
			self.logger.warning(f"Failed to set up memory profiling: {e}")

	@staticmethod
	def _validate_construction_params(*, max_failures, retry_delay, max_input_tokens,
	                                  max_actions_per_step, max_error_length, task, orchestrator):
		"""Validate critical construction parameters (P7 finalization: extracted from
		__init__). Pure — raises ROBValidationError on invalid input, sets no state."""
		if not isinstance(max_failures, int) or max_failures < 0:
			raise ROBValidationError(f"max_failures must be a non-negative integer, got {max_failures}")
		if not isinstance(retry_delay, int) or retry_delay < 0:
			raise ROBValidationError(f"retry_delay must be a non-negative integer, got {retry_delay}")
		if max_input_tokens is not None and (not isinstance(max_input_tokens, int) or max_input_tokens <= 0):
			raise ROBValidationError(f"max_input_tokens must be a positive integer or None, got {max_input_tokens}")
		if not isinstance(max_actions_per_step, int) or max_actions_per_step <= 0:
			raise ROBValidationError(f"max_actions_per_step must be a positive integer, got {max_actions_per_step}")
		if not isinstance(max_error_length, int) or max_error_length <= 0:
			raise ROBValidationError(f"max_error_length must be a positive integer, got {max_error_length}")
		if not task or not isinstance(task, str):
			raise ROBValidationError("task must be a non-empty string")
		if not orchestrator:
			raise ROBValidationError("orchestrator is required - Agent cannot run standalone")

	def __init__(self, config: AgentConfig, deps: AgentDeps):
		"""Initialize Agent from a config + deps pair.

		The original 31-param constructor body is preserved verbatim below; the
		parameters are simply unpacked from config/deps into the same local names
		the body already uses (PR9 item #2 — typed construction surface).
		"""
		# --- unpack deps ---
		llm = deps.llm
		orchestrator = deps.orchestrator
		page_extraction_llm = deps.page_extraction_llm
		system_prompt_class = deps.system_prompt_class
		register_new_step_callback = deps.register_new_step_callback
		register_done_callback = deps.register_done_callback
		injected_controller = deps.controller  # UP-05: least-privilege child controller (or None)
		# --- unpack config ---
		task = config.task
		use_vision = config.use_vision
		save_conversation_path = config.save_conversation_path
		save_conversation_path_encoding = config.save_conversation_path_encoding
		max_failures = config.max_failures
		retry_delay = config.retry_delay
		max_input_tokens = config.max_input_tokens
		validate_output = config.validate_output
		# Operator gate: MemoryConfig.ENABLE_GIF_CREATION (env, default false) is the
		# master switch. AgentConfig.generate_gif defaults True, so without this gate a
		# headless server makes per-conversation GIFs nobody consumes (live-test waste).
		generate_gif = _resolve_generate_gif(config.generate_gif, MemoryConfig.ENABLE_GIF_CREATION)
		sensitive_data = config.sensitive_data
		available_file_paths = config.available_file_paths
		include_attributes = config.include_attributes
		max_error_length = config.max_error_length
		max_actions_per_step = config.max_actions_per_step
		initial_actions = config.initial_actions
		tool_calling_method = config.tool_calling_method
		agent_name = config.agent_name
		session_config = config.session_config
		profile_id = config.profile_id
		profile_overrides = config.profile_overrides
		use_native_tools = config.use_native_tools
		step_timeout_seconds = config.step_timeout_seconds
		stall_timeout_seconds = config.stall_timeout_seconds
		max_step_timeout = config.max_step_timeout
		is_sub_agent = config.is_sub_agent
		parent_session_id = config.parent_session_id
		role = config.role
		# INPUT VALIDATION (P7 finalization: extracted to _validate_construction_params).
		self._validate_construction_params(
			max_failures=max_failures, retry_delay=retry_delay,
			max_input_tokens=max_input_tokens, max_actions_per_step=max_actions_per_step,
			max_error_length=max_error_length, task=task, orchestrator=orchestrator,
		)

		# Store orchestrator reference (single source of truth for session_id and user_id)
		# DO NOT copy session_id or user_id - use properties instead
		self.orchestrator = orchestrator

		# Store the agent name
		self.agent_name = agent_name
		
		# Sub-agent isolation flags
		# Sub-agents skip H-MEM and message persistence to prevent context pollution
		self._is_sub_agent = is_sub_agent
		self._parent_session_id = parent_session_id
		# Delegation role: a "leaf" agent cannot call delegate_task (roadmap P1).
		# Sub-agents are spawned as "leaf"; the main agent defaults to "orchestrator".
		self._role = "leaf" if is_sub_agent else role

		# Initialize Tool Call Tracker for robust ID management
		# Note: Uses orchestrator.session_id directly
		self.tool_call_tracker = ToolCallTracker(
			session_id=self.orchestrator.session_id,
			logger=None  # Will use its own logger
		)

		# Stall detection interval (state.last_activity_time set later after state is created)
		self._stall_check_interval = 60  # Check every minute

		# Use orchestrator's infrastructure (single instances per session)
		self.session_manager = self.orchestrator.session_manager
		self.telemetry_manager = self.orchestrator.telemetry_manager

		# Initialize logger early so it's available for profile and session config processing
		from agents.task.logging_config import get_task_logger
		self.logger = get_task_logger(self.agent_name, self.orchestrator.session_id)

		# RESTORATION (P7: extracted to _restore_tool_call_tracker). Load saved
		# tool-call-tracker state from a previous session — after logger init.
		self._restore_tool_call_tracker(orchestrator)

		# Apply profile configuration if specified (after logger is initialized)
		if profile_id:
			from agents.task.agent.profile_manager import ProfileManager

			# Load and apply profile configuration
			updated_config = ProfileManager.load_and_apply(profile_id, locals(), profile_overrides)

			# Apply system message if profile provided one
			if '_profile_system_message' in updated_config:
				self._profile_system_message = updated_config['_profile_system_message']

			# Create LLM from profile config if provided and not already set
			if '_profile_llm_config' in updated_config and not llm:
				llm_config = updated_config['_profile_llm_config']
				llm = self._create_llm_from_config(llm_config)
				if llm:
					self.logger.info(f"Created LLM from profile config: {llm_config.get('provider')}/{llm_config.get('model')}")
				else:
					self.logger.warning(f"Could not create LLM from config: {llm_config}")

			# Apply other profile settings
			if '_enabled_actions' in updated_config:
				self._enabled_actions = updated_config['_enabled_actions']
			if '_profile_max_steps' in updated_config:
				self._profile_max_steps = updated_config['_profile_max_steps']

			# Apply profile settings explicitly. The old `locals()[key] = ...` loop was
			# a no-op (assigning to locals() does not rebind), silently dropping these
			# overrides — see resolve_profile_overrides.
			tool_calling_method, max_actions_per_step, max_input_tokens, max_failures = \
				resolve_profile_overrides(
					updated_config,
					tool_calling_method=tool_calling_method,
					max_actions_per_step=max_actions_per_step,
					max_input_tokens=max_input_tokens,
					max_failures=max_failures,
				)

		# Initialize tool call adapter for normalizing tool calls across providers
		# This is initialized later after controller is set up
		
		# Process session config if provided
		self.session_config = None
		if session_config:
			try:
				from agents.task.config import TaskSessionConfig

				# Create config from dict
				self.session_config = TaskSessionConfig.from_dict(session_config)

				# Apply config values (override individual parameters)

				# Apply limits - override parameters if session config provides them
				if self.session_config.limits.max_actions_per_step is not None:
					max_actions_per_step = self.session_config.limits.max_actions_per_step
				if self.session_config.limits.max_input_tokens is not None:
					max_input_tokens = self.session_config.limits.max_input_tokens
				if self.session_config.limits.max_failures is not None:
					max_failures = self.session_config.limits.max_failures

				# CRITICAL FIX: Apply LLM config from session_config
				# Session config should OVERRIDE the profile's LLM (session is more specific)
				# This was previously missing, causing user's model selection to be ignored!
				if self.session_config.llm:
					# Check if session config specifies a non-default model
					# Default is gpt-5/openai - if user specified something different, use it
					is_non_default_model = (
						self.session_config.llm.model != "gpt-5" or
						self.session_config.llm.provider != "openai"
					)

					if is_non_default_model or not llm:
						llm_config = {
							'model': self.session_config.llm.model,
							'provider': self.session_config.llm.provider,
							'temperature': self.session_config.llm.temperature,
							'use_vision': self.session_config.llm.use_vision
						}
						self.logger.info(f"Creating LLM from session config: {llm_config['provider']}/{llm_config['model']}")
						new_llm = self._create_llm_from_config(llm_config)
						if new_llm:
							llm = new_llm  # Override the profile LLM
							self.logger.info(f"✅ Created LLM from session config: {llm_config['provider']}/{llm_config['model']}")
						else:
							self.logger.error(f"❌ Failed to create LLM from session config: {llm_config}, keeping existing LLM")

				# Save config to session
				self._save_session_config()

				self.logger.info("Applied session config")

			except Exception as e:
				self.logger.warning(f"Failed to apply session config: {e}")
				# Continue with individual parameters as fallback
		
		
		# HRE removed - was experimental feature
		
		
		# Optional memory profiling (P7 finalization: extracted to _setup_memory_profiling).
		self._setup_memory_profiling()
		
		# Logger already initialized earlier, just log that we're ready
		# (This used to be where logger was initialized, but we moved it earlier
		# to be available for session_config processing)
		
		# Set the agent_id - combine agent_name and clean session_id
		# Format: agent_name_session_id (e.g., agent_1234-5678)
		self.agent_id = f"{self.agent_name}_{self.orchestrator.session_id}"

		# Debug log to track session ID usage
		self.logger.debug(f"Agent initialized with ID: {self.agent_id}")

		# Initialize HITL Manager (after agent_id is set)
		from agents.task.agent.hitl_manager import HITLManager
		self.hitl_manager = HITLManager(
			session_id=self.orchestrator.session_id,
			agent_id=self.agent_id,
			logger=self.logger,
			telemetry_manager=self.telemetry_manager
		)

		# NOTE: model_name will be available via property after MessageManager is created
		# DO NOT extract it here - MessageManager is the single source of truth

		# NOTE: Agent registration is handled by SessionOrchestrator.create_agent()
		# Agent only uses SessionManager for metadata updates via _update_session_metadata()
		# This prevents duplicate registration and ensures single source of truth

		# Basic configuration
		self.sensitive_data = sensitive_data
		self.task = task
		self.use_vision = use_vision
		self.llm = llm

		# Set chat_model_library for provider detection (needed by set_tool_calling_method)
		# This must be set BEFORE calling set_tool_calling_method() at line 427
		self.chat_model_library = self.llm.__class__.__name__

		# Store agent type for telemetry
		self.agent_type = self.__class__.__name__
		
		self.use_native_tools = use_native_tools
		# Store timeout configuration with validation
		self.step_timeout_seconds = step_timeout_seconds or 600
		self.stall_timeout_seconds = stall_timeout_seconds or 600
		self.max_step_timeout = max_step_timeout

		# Ensure step timeout doesn't exceed max
		if self.step_timeout_seconds > self.max_step_timeout:
			self.logger.warning(f"step_timeout_seconds {self.step_timeout_seconds} exceeds max {self.max_step_timeout}, using max")
			self.step_timeout_seconds = self.max_step_timeout

		# NOTE: Approval state now managed by HITLManager
		
		# Page extraction LLM setup
		if not page_extraction_llm:
			self.page_extraction_llm = llm
		else:
			self.page_extraction_llm = page_extraction_llm
		
		# File paths
		self.available_file_paths = available_file_paths
		
		# Normalize save_conversation_path (P7: extracted to _normalize_save_conversation_path).
		self._normalize_save_conversation_path(save_conversation_path)
		
		self.save_conversation_path_encoding = save_conversation_path_encoding
		
		# State tracking
		self._last_result = None
		self._cancelled = False  # Cancellation flag for stopping execution
		self._deferred_mcp_actions: List[Dict[str, Any]] = []  # MCP actions deferred from previous step
		self._max_mcp_deferrals = 2  # Cap deferrals to prevent infinite loops
		# Set default for include_attributes if not provided
		if include_attributes is None:
			include_attributes = [
				'title',
				'type',
				'name',
				'role',
				'tabindex',
				'aria-label',
				'placeholder',
				'value',
				'alt',
				'aria-expanded',
			]
		self.include_attributes = include_attributes
		self.max_error_length = max_error_length
		self.max_input_tokens = max_input_tokens  # Store max_input_tokens for MessageManager
		self.generate_gif = generate_gif



		# Controller: an injected (least-privilege child) controller takes precedence
		# (UP-05); otherwise the shared orchestrator controller. All downstream
		# derivations (ActionModel, action_descriptions, mcp_servers_info,
		# message_manager) follow from self.controller, so this is the only seam.
		self.controller = injected_controller or self.orchestrator.controller
		_ctl_src = "injected (least-privilege)" if injected_controller else "orchestrator"
		self.logger.debug(f"Using {_ctl_src} controller with {len(self.controller.list_tools())} tools")

		self.max_actions_per_step = max_actions_per_step

		# WS-A capability gate: while the session is tainted by untrusted correspondent
		# DATA, deny high-impact tools (money/comms/code-exec/delegation). Registered
		# fail-CLOSED, gated on the access model + a real orchestrator taint flag. The
		# owner clears the taint by sending a genuine turn.
		#
		# SECURITY (P1 finalization): registration is NOT wrapped in a swallow-all
		# except. When the access model is ON (an explicit opt-in), a registration
		# failure must SURFACE — a tainted session that came up WITHOUT this
		# fail-closed gate could run high-impact tools on untrusted correspondent
		# DATA. When the model is OFF the `if` is false and nothing is registered.
		from agents.task.surface_config import SurfaceConfig
		if SurfaceConfig.correspondent_access_enabled() and self.controller is not None \
				and hasattr(self.controller, "register_pre_tool_call_hook"):
			from agents.task.agent.core.correspondent_gate import (
				build_reply_allowed, build_tool_resolver, make_correspondent_gate_hook)
			_orch = self.orchestrator
			# Resolve each action's owning tool_id so the tool-id-level denylist
			# actually fires — the pre-hook only ever sees the bare action name
			# (run_code, goal_create, x402_fetch, dynamic MCP {server}_{tool}).
			# D1 (2026-07-13): taint sources + reply budget enable the scoped
			# reply-to-the-tainting-party exemption (CORRESPONDENT_REPLY_ENABLED,
			# default OFF -> the exemption never fires, gate unchanged).
			_gate = make_correspondent_gate_hook(
				lambda: bool(getattr(_orch, "_correspondent_tainted", False)),
				resolve_tool=build_tool_resolver(self.controller),
				get_taint_sources=lambda: set(
					getattr(_orch, "_correspondent_taint_sources", None) or set()),
				reply_allowed=build_reply_allowed(
					lambda: getattr(_orch, "container", None),
					lambda: getattr(_orch, "user_id", "") or ""))
			self.controller.register_pre_tool_call_hook(_gate, fail_mode="closed")

		# ToolCallAdapter removed - functionality integrated into MessageManager and Registry
		# (tool calls now flow through ToolCallBuilder + Registry).

		# Browser context obtained on-demand via property (see get_browser_context())

		self.system_prompt_class = system_prompt_class

		# Agent gets pre-configured ActionModel from Controller
		self.ActionModel = self.controller.create_action_model()
		self.AgentOutput = AgentOutput.type_with_custom_actions(self.ActionModel)

		self._set_version_and_source()
		
		# Token limits are now calculated by MessageManager (SINGLE SOURCE OF TRUTH)
		# Just pass max_input_tokens override if provided, otherwise MessageManager auto-calculates

		self.tool_calling_method = self.set_tool_calling_method(tool_calling_method)

		# NOTE: Provider detection will happen after MessageManager is created
		# For now, detect provider just to check native tools support
		# This is a temporary detection - model_name property will delegate to MessageManager
		_temp_model_name = self._extract_model_name(llm)
		provider = detect_llm_provider(None, _temp_model_name)

		# Preserve user intent - intersect user preference with provider capability.
		# This also re-assigns self.use_native_tools so the agent's own flag matches
		# what MessageManager is given (see _reconcile_native_tools).
		use_native_tools = self._reconcile_native_tools(provider)

		# Cache action descriptions for reuse (single call to controller).
		# T1-03: in native mode the full parameter schemas already ship to the
		# provider in the `tools` param, so the prompt gets a compact one-line
		# index instead of the raw schema dump (2.5-6k redundant tokens/session).
		# The JSON-fallback path keeps the full dump — there it IS the schema.
		if use_native_tools:
			self.action_descriptions = self.controller.get_prompt_action_index()
		else:
			self.action_descriptions = self.controller.get_prompt_description()

		# Get MCP server info for dynamic prompt generation (no hardcoded examples)
		self.mcp_servers_info = self.controller.get_mcp_servers_info()

		# Hierarchical Memory System - MANDATORY for main agents, SKIPPED for sub-agents
		# Sub-agents don't need H-MEM - they do focused tasks and return results
		self.task_context_manager = None
		self._created_task_context_manager = False

		if self._is_sub_agent:
			# SUB-AGENT ISOLATION: Use NullTaskContextManager to prevent context pollution
			# FIX (Jan 2026): Use NullObject pattern instead of None to prevent AttributeError
			# Sub-agents extract their own output which is returned to parent
			from modules.memory.task.null_context_manager import NullTaskContextManager
			self.task_context_manager = NullTaskContextManager()
			self.logger.info(f"🔒 Sub-agent mode: Using NullTaskContextManager (parent: {self._parent_session_id[:20] if self._parent_session_id else 'unknown'}...)")
		else:
			# Main agent: Initialize H-MEM as usual
			# Try to get from container first
			try:
				memory_manager = self.orchestrator.container.get_service('memory_manager')
				if memory_manager and hasattr(memory_manager, 'task_context_manager'):
					self.task_context_manager = memory_manager.task_context_manager
					self.logger.info("✅ Using TaskContextManager from container")
			except Exception as e:
				self.logger.debug(f"Could not get TaskContextManager from container: {e}")

			# If not available, create dedicated instance for this session
			if not self.task_context_manager:
				self.logger.warning(
					"No memory_manager in container — creating a dedicated "
					"TaskContextManager for this session (cross-agent memory will NOT "
					"be shared)"
				)
				from modules.memory.task.task_context_manager import TaskContextManager
				from core.config import BotConfig

				# Create instance
				config = self.orchestrator.container.config if self.orchestrator.container else BotConfig()
				self.task_context_manager = TaskContextManager(
					name=f"task_context_{self.session_id}",
					config=config
				)
				self._created_task_context_manager = True

		# Opt-in external cross-session memory backend (MEMORY_BACKEND=sqlite|local_vector).
		# Registered once via the existing one-external-provider registry; the live
		# prefetch/sync_turn wiring (step.py + _save_step_to_memory) already routes through
		# registry.active(). For local_vector we hand it the container's already-loaded local
		# embedding model (no second copy); it degrades to FTS5-only if absent.
		# NOTE: data_dir lives on the container's BotConfig — Agent has no self.config.
		try:
			from modules.memory.backend_factory import maybe_register_memory_backend
			_container = getattr(getattr(self, "orchestrator", None), "container", None)
			_cfg = getattr(_container, "config", None)
			from core.runtime_paths import data_dir_or_home
			data_dir = data_dir_or_home(getattr(_cfg, "data_dir", None))
			_embedder = None
			if _container is not None and getattr(_container, "has_service", None):
				try:
					if _container.has_service("embedding_model"):
						_embedder = _container.get_service("embedding_model")
				except Exception:
					_embedder = None
			_provider = maybe_register_memory_backend(data_dir=data_dir, embedding_model=_embedder)
			# The Controller's session_search/memory_search registration gate ran at
			# Controller.__init__ — BEFORE this backend registration — so on the FIRST
			# session of a process (e.g. `polyrob run`) it saw no external provider and
			# skipped the recall tools (W6). Re-run the gated registration now that the
			# backend exists. Idempotent: guarded on not-already-registered so a later
			# session (backend already up at Controller init) doesn't double-register.
			if (_provider is not None and getattr(_provider, "is_external", False)
					and self.controller is not None):
				try:
					_names = self.controller.registry.list_action_names()
					if "session_search" not in _names:
						self.controller._register_session_search_action()
					# ME-D1: same first-session gap for the other provider-gated tools —
					# their gates ran at Controller init, before the backend existed.
					if "recent_activity" not in _names:
						self.controller._register_recent_activity_action()
					if "memory" not in _names:
						self.controller._register_memory_tool_action()
				except Exception as e:
					self.logger.debug(f"memory tool re-registration skipped: {e}")
		except Exception as e:
			self.logger.debug(f"memory backend registration skipped: {e}")

		# P2-24: recompute the prompt action index AFTER the memory-backend
		# registration above (registers session_search/recent_activity/memory on the
		# FIRST session of a process). It was computed earlier, before those tools
		# existed, so the first session's system prompt omitted them while the per-
		# step schemas included them. action_descriptions is not consumed until the
		# MessageManager build below, so recomputing here is safe. Native only.
		try:
			if use_native_tools and self.controller is not None:
				self.action_descriptions = self.controller.get_prompt_action_index()
		except Exception as _aidx_err:
			self.logger.debug(f"action-index recompute skipped: {_aidx_err}")

		# Load skills for this session and embed into system message
		# Skills are per-session prompt extensions that provide context-aware guidance
		system_message = getattr(self, '_profile_system_message', None)
		skill_content = None
		
		try:
			from agents.task.agent.skill_manager import get_skill_manager
			skill_manager = get_skill_manager()
			
			# Get tool_ids from controller (what tools are loaded for this session)
			tool_ids = self.controller.list_tools() if self.controller else []
			
			# Get available actions for more precise matching
			available_actions = []
			if self.controller and hasattr(self.controller, 'registry'):
				available_actions = self.controller.registry.list_action_names()
			
			# Match skills based on session context (include user's custom skills)
			user_id = self.orchestrator.user_id if hasattr(self.orchestrator, 'user_id') else None
			from agents.task.templates import seeded_skills_for
			_seeded = seeded_skills_for(os.environ.get("POLYROB_PERSONA"))
			matched_skills = skill_manager.get_skills_for_session(
				tool_ids=tool_ids,
				task=self.task,
				available_actions=available_actions,
				user_id=user_id,
				seeded_skill_ids=_seeded or None,
			)

			# S9 fix (default OFF -> byte-identical): with progressive disclosure, optionally
			# expose ALL available skills in the catalog so the agent can discover + load_skill
			# any of them (not just trigger-matched ones). Matched skills stay first (priority).
			from agents.task.constants import (
				skill_progressive_disclosure as _prog_disc,
				skill_catalog_include_all as _catalog_include_all,
			)
			# Access-time read (not the import-bound constant) so the local-mode
			# default (ON under POLYROB_LOCAL) is honored for the terminal agent.
			if _prog_disc() and _catalog_include_all():
				try:
					_seen = {s.skill_id for s in matched_skills}
					# SK-F1: the builtin library is 23 rules and every authored skill
					# defaults to priority 6 (the old cap-20 cut band) — 50 is comfortably
					# above both so the catalog actually reflects the full library.
					_extra = [s for s in skill_manager.get_catalog_skills(user_id=user_id, max_skills=50, tool_ids=tool_ids)
							  if s.skill_id not in _seen]
					matched_skills = list(matched_skills) + _extra
				except Exception as _cat_err:
					self.logger.debug(f"catalog-include-all skipped (non-fatal): {_cat_err}")

			if matched_skills:
				# S-1: progressive disclosure. When enabled, inject only a compact
				# <skill-catalog> and let the agent pull full bodies via load_skill;
				# otherwise (default) embed full bodies as today. Either way the
				# matched skills (with pre-loaded bodies) are handed to the controller
				# so load_skill can serve them without re-reading disk.
				from agents.task.constants import skill_progressive_disclosure
				if skill_progressive_disclosure():
					skill_content = skill_manager.format_skill_catalog(matched_skills)
					if self.controller is not None:
						self.controller._session_skills = {s.skill_id: s for s in matched_skills}
					self.logger.info(
						f"✨ Skill catalog ({len(matched_skills)}) for session "
						f"[progressive disclosure]: {[s.skill_id for s in matched_skills]}"
					)
				else:
					skill_content = skill_manager.format_skills_for_prompt(matched_skills)
					self.logger.info(f"✨ Loaded {len(matched_skills)} skills for session: {[s.skill_id for s in matched_skills]}")
					# SK-F3: with progressive disclosure OFF, skills are eager-injected
					# full-body (no load_skill call), so usage was never recorded and the
					# curator archived authored skills as "never used." Bump provenance
					# here too, at the only choke point this path has.
					_bump_eager_skill_usage(matched_skills, user_id, self.logger)
				
				# Save skills info to session directory for webview
				try:
					from agents.task.path import pm
					import json
					# P2-25: precompute user-authored skill ids via the REAL method
					# (_load_user_rules returns (rules, dir); the old hasattr referenced
					# a nonexistent _load_user_skill_rules, so is_user_skill was always
					# the startswith/endswith heuristic).
					_user_skill_ids = set()
					try:
						if user_id and hasattr(skill_manager, '_load_user_rules'):
							_ur, _ = skill_manager._load_user_rules(user_id)
							_user_skill_ids = set((_ur or {}).keys())
					except Exception:
						_user_skill_ids = set()
					skills_data = [
						{
							"id": s.skill_id,
							"name": s.skill_id.replace("-", " ").title(),
							"priority": s.priority,
							"trigger_type": s.trigger_type,
							"is_user_skill": s.skill_id.startswith("forked-") or s.skill_id.endswith("-custom") or s.skill_id in _user_skill_ids
						}
						for s in matched_skills
					]
					skills_path = pm().create_file_path(
						session_id=self.orchestrator.session_id,
						subdir_name="",
						filename="skills.json",
						user_id=user_id
					)
					with open(skills_path, 'w') as f:
						json.dump(skills_data, f, indent=2)
					self.logger.debug(f"Saved skills info to {skills_path}")
				except Exception as save_err:
					self.logger.debug(f"Could not save skills info (non-fatal): {save_err}")
		except Exception as e:
			self.logger.debug(f"Could not load skills (non-fatal): {e}")
		
		# PR13: never embed skills into the system message - keep it stable/cacheable.
		# Skills are injected as a pinned user message via set_skill_message() below.
		# Here we just strip the placeholder from any profile system message.
		if system_message:
			content = system_message.content.replace("{SKILLS_PLACEHOLDER}", "")
			system_message = SystemMessage(content=content)

		# Store skill content for embedding later if SystemPrompt generates the message
		self._skill_content = skill_content

		# Initialize message manager with proper session ID and optional profile system message
		# MessageManager becomes the SINGLE SOURCE OF TRUTH for model_name and provider_name
		self.message_manager = MessageManager(
			llm=self.llm,
			task=self.task,
			# Always provide action descriptions for context, even with native tools
			# This helps the LLM understand what actions are available
			action_descriptions=self.action_descriptions,
			system_prompt_class=self.system_prompt_class,
			max_input_tokens=self.max_input_tokens,
			include_attributes=self.include_attributes,
			max_error_length=self.max_error_length,
			max_actions_per_step=self.max_actions_per_step,
			sensitive_data=self.sensitive_data,
			session_id=self.orchestrator.session_id,  # Pass clean session_id from orchestrator
			system_message=system_message,  # Pass profile system message with skills embedded
			use_native_tools=use_native_tools,  # Pass native tools flag
			tool_call_tracker=self.tool_call_tracker,  # Pass tool call tracker for ID management
			task_context_manager=self.task_context_manager,  # Pass hierarchical memory system
			mcp_servers=self.mcp_servers_info,  # Pass MCP server info for dynamic prompts
			persona_block=getattr(config, 'persona_block', None),  # S1: chat-mode persona
			# Session's loaded tools → config-aware prompt gating (e.g. <anysite> and
			# <browser-tools> only render when the tool is actually loaded this
			# session, not on a global flag).
			tool_ids=self.controller.list_tools() if self.controller else [],
			# T1-06: the vision prompt section follows the session's real use_vision
			# instead of claiming image abilities for use_vision=False sessions.
			include_vision=bool(self.use_vision),
		)

		# Agent-level provider label SSOT. The step-loop billing path reads
		# self.llm_provider (next_action_internal.py) and the B3 per-request swap
		# guard reads it for idempotence; without this it stayed unset on a
		# never-swapped agent (billing recorded 'unknown', and swap_model was the
		# only writer). Seed it from the MessageManager's detected provider so it
		# is consistent with the SSOT the model_name/provider_name properties mirror.
		self.llm_provider = self.message_manager.provider_name

		# PR13: strip any skills placeholder from the generated system message; skills
		# are NOT embedded in the system prompt anymore (kept stable for prompt caching).
		if not getattr(self, '_profile_system_message', None):
			current_content = self.message_manager._system_message.content
			if "{SKILLS_PLACEHOLDER}" in current_content:
				new_content = current_content.replace("{SKILLS_PLACEHOLDER}", "")
				self.message_manager._system_message = SystemMessage(content=new_content)
				self.message_manager._system_message_tokens = self.message_manager._count_message_tokens(
					self.message_manager._system_message
				)

		# PR13: pin skills as a foundation user message (origin=SKILL), not the system
		# prompt. get_messages_for_llm() injects this; the system prompt stays stable.
		self.message_manager.set_skill_message(skill_content)

		# polyrob Phase C: pin operator-authored SOUL/IDENTITY self-context as a frozen
		# foundation message, read ONCE at session start from <data_dir>/identity/. This
		# is operator-write-only (the agent never authors it in this cut). Empty/absent
		# docs => set_self_context_message(None) => inert / byte-identical. Like skills,
		# it lives in the foundation, NOT the system prompt, so the prompt stays cacheable.
		try:
			from core.instance import (load_self_context, load_self_doc,
										load_owner_doc, resolve_instance_id)
			_container = getattr(getattr(self, "orchestrator", None), "container", None)
			_cfg = getattr(_container, "config", None)
			from core.runtime_paths import data_dir_or_home
			_data_dir = data_dir_or_home(getattr(_cfg, "data_dir", None))
			# SOUL tier (operator-only, instance-global) + the evolving SELF tier
			# (agent-writable, per-(instance,user)). Both frozen at session start.
			# load_self_doc applies the load-side [BLOCKED] guard; empty => omitted.
			_soul = load_self_context(_data_dir)
			_uid = self.orchestrator.user_id if hasattr(self.orchestrator, "user_id") else None
			_self_doc = load_self_doc(_data_dir, _uid, resolve_instance_id())
			# Bounded owner-facts doc (agent-maintained, per-(instance,user)): durable
			# facts/preferences about the OWNER, injected after SOUL and before the
			# evolving SELF doc. Load-side [BLOCKED] guard applies; empty => omitted.
			_owner_doc = load_owner_doc(_data_dir, _uid, resolve_instance_id())
			if _owner_doc:
				_owner_doc = "## Owner facts\n\n" + _owner_doc
			# Owner-UX Phase 2: the owner-authored operating-contract doc
			# (contract.md, owner-review-gated via ContractWriter) + a deterministic
			# one-line style summary from typed prefs, both injected after owner
			# facts and before the evolving SELF doc. Gated on CONTRACT_DOC_ENABLED
			# (default ON); absent file + no style prefs set => "" => no change to
			# the join below (byte-identical to legacy).
			_contract_block = ""
			try:
				from agents.task.constants import AutonomyConfig as _ContractAC
				if _ContractAC.contract_doc_enabled():
					from core.instance import load_contract_doc
					_contract_doc = load_contract_doc(_data_dir, _uid, resolve_instance_id())
					if _contract_doc:
						_contract_doc = "## Operating contract\n\n" + _contract_doc
					from core.prefs import load_preferences, render_style_line
					_prefs = load_preferences(_data_dir, _uid, resolve_instance_id())
					_style_line = render_style_line(_prefs)
					_contract_block = "\n\n".join(p for p in (_contract_doc, _style_line) if p)
			except Exception:
				_contract_block = ""
			# WS-A + T1-13: the owner clause ("You act on behalf of OWNER X") renders
			# whenever a DISTINCT owner principal resolves; the correspondent-DATA frame
			# sentence stays gated on the three-tier access model being on. With no
			# distinct owner and the model off this is "" — byte-identical legacy.
			_awareness = ""
			try:
				from core.instance import owner_awareness_line
				_corr_on = False
				try:
					from agents.task.surface_config import SurfaceConfig
					_corr_on = SurfaceConfig.correspondent_access_enabled()
				except Exception:
					_corr_on = False
				_awareness = owner_awareness_line(include_correspondent_frame=_corr_on)
			except Exception:
				_awareness = ""
			_combined = "\n\n".join(
				p for p in (_awareness, _soul, _owner_doc, _contract_block, _self_doc) if p
			)
			self.message_manager.set_self_context_message(_combined)
		except Exception as e:
			self.logger.debug(f"Could not load self-context (non-fatal): {e}")

		# C9 + Phase 2: auto-load project context (P7: extracted to _load_project_context).
		self._load_project_context()

		# Model-identity SSOT: pin the model/provider the agent actually runs on, so
		# it can answer "what model are you" from an authoritative foundation line
		# instead of reading env/config files (which leaked secrets). Sourced from the
		# MessageManager model_name SSOT + the detected provider. Fail-open/inert.
		try:
			_ident_model = getattr(self.message_manager, "model_name", None) or getattr(self, "model_name", None)
			_ident_provider = (
				getattr(self, "llm_provider", None)
				or getattr(self.message_manager, "provider_name", None)
			)
			self.message_manager.set_runtime_identity(_ident_model, _ident_provider)
		except Exception as e:
			self.logger.debug(f"Could not pin runtime identity (non-fatal): {e}")

		# 014-C1: pin the <environment> block (where the agent lives — host,
		# workspace + persistence, posture axes, host executables). Local/autonomous
		# only; a plain multi-tenant server session gets None => inert. Fail-open.
		try:
			from agents.task.agent.core.env_context import build_environment_context
			_env_block = build_environment_context(
				self.orchestrator.session_id,
				getattr(self.orchestrator, "user_id", None),
				tool_ids=getattr(self, "tool_ids", None),
			)
			if _env_block:
				self.message_manager.set_environment_message(_env_block)
		except Exception as e:
			self.logger.debug(f"Could not pin environment context (non-fatal): {e}")

		# A5: optional cheap auxiliary model for compaction only. Inert unless
		# COMPACTION_MODEL is set; on any failure aux_llm stays None and
		# llm_compact_history transparently uses the main model (current behaviour).
		try:
			self.message_manager.aux_llm = self._provision_compaction_llm()
		except Exception as e:
			self.logger.debug(f"compaction aux model provisioning skipped: {e}")
			self.message_manager.aux_llm = None

		# Reflection LLM: its own aux slot (B5), which inherits compaction's
		# model+provider by default when unset (back-compat: reflection historically
		# reused _provision_compaction_llm wholesale) — see
		# constants.resolve_aux_chain(). Operators can now set AUX_MODEL_REFLECTION /
		# AUX_FALLBACK_REFLECTION independently. UP-09: default ON via
		# constants.reflection_llm_enabled_default() — the SAME helper the
		# TaskContextManager runtime guard reads, so the two can never diverge
		# (historical bug: TCM read BotConfig.get -> always False -> never fired).
		from agents.task.constants import reflection_llm_enabled_default
		if reflection_llm_enabled_default():
			try:
				self.task_context_manager.reflection_llm = self._provision_aux_llm("reflection")
			except Exception as e:
				self.logger.debug(f"reflection LLM provisioning skipped: {e}")

		# CO-F1: the optional cheap aux 'judge' model for output validation
		# (_validate_output) is now provisioned LAZILY — only on the first actual
		# _validate_output() call, and only when validate_output is on (mirrors the
		# BackgroundReviewMixin pattern: aux models are resolved at the point of use,
		# not unconditionally at construction). Provisioning it here unconditionally
		# wasted an aux-model resolution on every session even while judge-backed
		# validation was entirely dead (validate_output defaulted False and nothing
		# ever flipped it, and the run loop never validated the true final answer).
		# See OutputValidationMixin._validate_output for the lazy provision site.
		self._judge_llm = None

		# Log model name from MessageManager (single source of truth)
		self.logger.debug(f"Using model: {self.message_manager.model_name}")
		
		if self.available_file_paths:
			self.message_manager.add_file_paths(self.available_file_paths)
		# Step callback
		self.register_new_step_callback = register_new_step_callback
		self.register_done_callback = register_done_callback

		# Centralized agent state - MUST BE INITIALIZED EARLY
		from agents.task.agent.agent_state import AgentState
		self.state = AgentState(
			n_steps=1,
			max_steps=None,  # Set later if profile provides it
			max_failures=max_failures,
			consecutive_failures=0,
			total_actions_count=0,
			paused=False,
			stopped=False
		)

		# Update activity time now that state exists (used for stall detection)
		self.state.last_activity_time = time.time()
		self.state.last_step_start_time = time.time()

		# RESTORATION: Try to load saved agent state from previous session
		if self.session_id and orchestrator:
			try:
				from agents.task.path import pm
				state_file = pm().create_file_path(
					session_id=self.session_id,
					subdir_name="data",
					filename="agent_state.json",
					user_id=orchestrator.user_id if hasattr(orchestrator, 'user_id') else None
				)

				loaded_state = AgentState.load_from_file(state_file)
				if loaded_state:
					self.state = loaded_state
					self.logger.info(f"📂 Restored agent state from previous session: step {self.state.n_steps}")

					# Update max_failures if current config provides new value
					if max_failures and max_failures != self.state.max_failures:
						self.state.max_failures = max_failures
			except Exception as e:
				self.logger.debug(f"No agent state to restore (new session or error): {e}")

		# Initialize workspace context for file upload awareness
		from agents.task.workspace_context import get_workspace_context
		self.workspace_context = get_workspace_context()

		# Tracking variables
		self.history: AgentHistoryList = AgentHistoryList(history=[])

		self.max_failures = max_failures
		self.retry_delay = retry_delay
		self.validate_output = validate_output
		self.initial_actions = self._convert_initial_actions(initial_actions) if initial_actions else None
		if save_conversation_path:
			self.logger.info(f'Saving conversation to {save_conversation_path}')

		# Stall detection tracking
		self._last_action_count = 0
		self._stall_check_interval = 30.0  # Check every 30 seconds
		self._stall_check_task = None
		self._llm_call_in_progress = False  # Track when waiting for LLM response
		self._llm_call_start_time = None  # When the current LLM call started

		# Log registered commands
		# Use Controller's high-level API instead of directly accessing registry
		registered_commands = self.controller.get_action_names()
		self.logger.debug(f"Registered commands: {len(registered_commands)} commands - {', '.join(registered_commands)}")
		
		# Create a session data dictionary for later use
		self.session_data = {
			"agent_id": self.agent_id,
			"task": self.task,
			"model_name": self.model_name,
			"created_at": datetime.now().isoformat(),
			"steps": [],
			"llm_requests": []
		}

		# Native tools debug flag
		self._native_tools_debug = os.environ.get('NATIVE_TOOLS_DEBUG', '').lower() == 'true'
		if self._native_tools_debug:
			self.logger.setLevel(logging.DEBUG)
			self.logger.info("[NATIVE_TOOLS] Debug mode enabled")

		# Loop detection variables with improved thresholds
		self._previous_actions = deque(maxlen=LoopDetectionConfig.MEMORY_WINDOW_SIZE)  # Use consistent window size
		self._action_repetition_counter = 0
		self._max_allowed_repetitions = LoopDetectionConfig.MAX_ALLOWED_REPETITIONS
		self._last_browser_states = deque(maxlen=3)  # MODIFIED: Use bounded deque instead of list
		self._unchanged_state_count = 0
		self._state_change_threshold = LoopDetectionConfig.STATE_CHANGE_THRESHOLD

		# No need for these locks that can cause deadlocks

		# FIXED: Initialize memory management for bounded collections
		self._initialize_memory_management()

		# Get usage tracker if available (UNIFIED tracking - replaces usage_meter + telemetry)
		# This is the preferred method for token tracking
		if self.orchestrator and hasattr(self.orchestrator, 'usage_tracker'):
			self.usage_tracker = self.orchestrator.usage_tracker
			if self.usage_tracker:
				self.logger.info("✓ Unified usage tracker initialized (replaces meter + telemetry)")
			else:
				self.logger.debug("Unified usage tracker not available")
		else:
			self.usage_tracker = None

		# A3: give the compactor + TaskContextManager (reflection) the metering
		# context so aux LLM calls bill through the same record_llm_usage() path
		# as the main step. Placed AFTER self.usage_tracker is assigned above
		# (not next to the :874/:886 aux-model provisioning, which runs before
		# usage_tracker exists) so these reads are never a stale None.
		self.message_manager.usage_tracker = self.usage_tracker
		self.message_manager.metering_user_id = getattr(self, "user_id", None)
		self.message_manager.metering_agent_id = getattr(self, "agent_id", None)
		# Capture the MAIN event loop here (construction runs on it). Reflection
		# consolidation runs on a worker thread (add_step_memory is offloaded via
		# asyncio.to_thread), where run_coroutine_sync would spin a THROWAWAY loop;
		# the usage tracker's DB connection binds an asyncio.Lock to the main loop,
		# so metering on a throwaway loop raises "bound to a different event loop"
		# and the fail-open swallow means reflection was NEVER billed. Passing the
		# main loop lets ReflectionService schedule the meter back onto it via
		# run_coroutine_threadsafe (the main loop is free — only the worker blocks —
		# so no deadlock). Fail-open: None => legacy run_coroutine_sync path.
		try:
			_main_loop = asyncio.get_running_loop()
		except RuntimeError:
			_main_loop = None
		self.task_context_manager.reflection_meter_ctx = {
			"usage_tracker": self.usage_tracker,
			"user_id": getattr(self, "user_id", None),
			"session_id": getattr(self, "session_id", ""),
			"agent_id": getattr(self, "agent_id", "") or "",
			"loop": _main_loop,
		}

		# UsageMeter retired (C5) — LLMUsageTracker (self.usage_tracker, above) is
		# always available under the same precondition usage_meter needed
		# (db + balance_manager). Kept as None for back-compat attribute access.
		self.usage_meter = None

		# Save initial task to task.json for webview chat
		self._save_initial_task()

		# OPTIMIZATION: Setup tool output logging (Task 1 - Nov 14, 2025)
		self.tool_output_log_path = None
		if self.orchestrator and self.orchestrator.session_id and self.orchestrator.user_id:
			log_dir = (Path(self.orchestrator.session_path)
			           if hasattr(self.orchestrator, 'session_path')
			           else self.orchestrator._path_manager.get_subdir(
			               self.orchestrator.session_id, "logs", self.orchestrator.user_id))
			log_dir.mkdir(parents=True, exist_ok=True)
			self.tool_output_log_path = log_dir / "tool_outputs.jsonl"
			self.logger.debug(f"Tool output logging enabled: {self.tool_output_log_path}")


