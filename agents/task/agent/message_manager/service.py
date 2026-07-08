from __future__ import annotations

import contextlib
import copy
import json
import logging
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Type, Tuple, Union

from agents.task.agent.views import ActionResult, AgentBrain, AgentOutput, AgentStepInfo

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

from tools.browser.views import BrowserState

# Import centralized LLM functionality - Single source of truth
try:
	# Use model_registry as the canonical source for model configs
	from modules.llm.model_registry import get_model_config
	from modules.llm import count_tokens, count_messages_tokens
	LLM_MODULE_AVAILABLE = True
except ImportError:
	LLM_MODULE_AVAILABLE = False
	# Create a logger here since we need it for the warning
	logger = logging.getLogger(__name__)
	logger.warning("LLM module not available, using fallback token counting")

# Import centralized constants
from agents.task.constants import IMG_TOKENS
# Model limits now come from modules.llm.model_registry

from agents.task.agent.message_manager.views import MessageHistory, MessageMetadata, ManagedMessage
from agents.task.agent.message_manager.tool_call_builder import (
    ToolCallBuilder,
    detect_and_remove_duplicate_tool_calls,
    repair_and_normalize,
    validate_tool_message_pairs
)
# MessageTrimmer removed - trimming disabled
from agents.task.agent.message_manager.config import MessageManagerConfig

from agents.task.agent.messages.token_counter import TokenCounterMixin
from agents.task.agent.messages.compactor import CompactorMixin
from agents.task.agent.messages.persistence import PersistenceMixin
from agents.task.agent.messages.filters import FiltersMixin
from agents.task.agent.messages.guidance import GuidanceMixin
from agents.task.agent.messages.builders import MessageBuildersMixin
from agents.task.agent.messages.retrieval import MessageRetrievalMixin

logger = logging.getLogger(__name__)

# Token counting is now centralized in modules/llm
# No local fallbacks needed

class MessageManager(TokenCounterMixin, CompactorMixin, PersistenceMixin, FiltersMixin, GuidanceMixin, MessageBuildersMixin, MessageRetrievalMixin):
	__slots__ = (
		'llm', 'system_prompt_class', 'history', 'task', 'action_descriptions',
		'IMG_TOKENS', 'include_attributes',
		'max_error_length', 'max_actions_per_step',
		'sensitive_data', 'session_id', 'add_task_message',
		'max_input_tokens', 'safe_input_tokens', 'completion_reserve', 'system_prompt',
		'logger', 'use_native_tools',
		'tool_call_tracker',  # Tool call tracker for ID management
		'task_context_manager',  # Phase 2: Hierarchical memory system
		'compaction_manager',  # FIX (Jan 2026): CompactionManager for context-aware compaction
		'mcp_servers',  # MCP server info for dynamic prompt generation
		'_ephemeral_messages',  # One-shot messages to include on next LLM call
		'_model_name',  # Cached model name
		'_provider_name',  # Cached provider name
		'_history_checkpoint',  # Checkpoint for message history rollback
		'_checkpoint_token_count',  # Token count at checkpoint time
		'_system_message',  # SystemMessage stored separately to prevent deque eviction
		'_system_message_tokens',  # Token count for system message
		'_initial_task_message',  # Initial task message stored separately to prevent deque eviction
		'_initial_task_tokens',  # Token count for initial task message
		'_skill_message',  # PR13: skills injected as a pinned foundation user message (not in system prompt)
		'_skill_message_tokens',  # Token cost of the pinned skill message
		'_self_context_message',     # polyrob: frozen SOUL/identity doc pinned in the foundation
		'_self_context_tokens',     # Token cost of the pinned self-context message
		'_project_context_message', # C9: auto-loaded CLAUDE.md/AGENTS.md frozen foundation message
		'_project_context_tokens',  # Token cost of the pinned project-context message
		'_runtime_identity_message', # model/provider the agent actually runs (swap-refreshable)
		'_runtime_identity_tokens',  # Token cost of the pinned runtime-identity message
		'_history_lock',  # SECURITY FIX: Lock to protect deque operations from race conditions
		'aux_llm',  # A5: optional cheap auxiliary model used only for llm_compact_history
		'_compaction_savings',  # B4: last-2 compaction savings ratios for anti-thrash
		'_compaction_count',  # C2: monotonic index for pre-compaction checkpoint files
		'_compaction_checkpoint_dir',  # C2: explicit checkpoint dir override (else pm() history dir)
	)

	def __init__(
		self,
		llm: BaseChatModel,
		task: str,
		action_descriptions: str,
		system_prompt_class: Type,  # Type of SystemPrompt
		max_input_tokens: Optional[int] = None,  # None = auto-calculate from model
		image_tokens: int = IMG_TOKENS,
		include_attributes: list[str] = [],
		max_error_length: int = 400,
		max_actions_per_step: int = 10,
		sensitive_data: Optional[Dict[str, str]] = None,
		session_id: Optional[str] = None,
		include_examples: bool = True,
		add_task_message: bool = True,
		system_message: Optional[SystemMessage] = None,  # NEW: Accept prebuilt system message
		use_native_tools: bool = True,  # Default to True to match Agent and config
		tool_call_tracker=None,  # Tool call tracker for ID management
		task_context_manager=None,  # Phase 2: Hierarchical memory system
		mcp_servers: Optional[Dict[str, List[str]]] = None,  # MCP server info for dynamic prompts
		persona_block: Optional[str] = None,  # S1: chat-mode persona for SystemPrompt <identity>
		tool_ids: Optional[List[str]] = None,  # Session's loaded tool_ids for config-aware prompt gating
		include_vision: bool = True,  # T1-06: session's use_vision → gates the vision prompt section
	):
		# S1 (chat consolidation): persona text forwarded to the SystemPrompt class
		# when this manager builds the system message itself (no prebuilt profile
		# message). None/"" => byte-identical legacy prompt.
		self._persona_block = persona_block
		# Set up logger with session ID
		from agents.task.logging_config import get_task_logger
		self.logger = get_task_logger("messages", session_id)

		self.llm = llm
		self.system_prompt_class = system_prompt_class

		# Cache model detection on init (single source of truth)
		self._model_name = self._detect_model_name()
		# Use consolidated provider detection from utils
		from agents.task.utils import detect_llm_provider
		self._provider_name = detect_llm_provider(None, self._model_name)

		# Calculate token limits from model (SINGLE SOURCE OF TRUTH)
		self.max_input_tokens, self.safe_input_tokens, self.completion_reserve = \
			self._calculate_token_limits(llm, max_input_tokens)

		# Calculate adaptive message limit based on context window
		# ADAPTIVE SCALING (Nov 4, 2025): Scale message history to model capacity
		# Modern models have 128k-2M token contexts - use them effectively!
		import os
		max_messages_override = os.getenv('TASK_MAX_MESSAGES')
		if max_messages_override:
			# P3: a non-numeric TASK_MAX_MESSAGES used to crash MessageManager.__init__
			# (and thus session creation). Ignore an invalid value with a warning.
			try:
				adaptive_max_messages = int(max_messages_override)
				self.logger.info(f"Using environment override for max messages: {adaptive_max_messages}")
			except ValueError:
				self.logger.warning(
					f"Ignoring non-numeric TASK_MAX_MESSAGES={max_messages_override!r}; "
					"using adaptive default")
				max_messages_override = None
		if not max_messages_override:
			# PHASE 1 FIX: Adaptive scaling based on model context window
			# Average message size: ~160 tokens (system/user/assistant/tool messages)
			# Target: Use 10-20% of context window for message history
			from modules.llm.model_registry import get_model_config
			model_config = get_model_config(self._model_name)
			
			if model_config and model_config.context_window:
				context_window = model_config.context_window
				
				# Scale based on model capacity
				if context_window >= 1_000_000:
					# Large context models (1M+): Use 10% of window for messages
					# 1M * 0.10 / 160 tokens/msg ≈ 625 messages
					message_budget_pct = 0.10
					self.logger.info(f"Detected large context model ({context_window:,} tokens)")
				elif context_window >= 200_000:
					# Medium-large context models (200K-1M): Use 12% 
					message_budget_pct = 0.12
					self.logger.info(f"Detected medium-large context model ({context_window:,} tokens)")
				elif context_window >= 100_000:
					# Medium context models (100K-200K): Use 15%
					message_budget_pct = 0.15
					self.logger.info(f"Detected medium context model ({context_window:,} tokens)")
				else:
					# Small context models (<100K): Use 30%
					message_budget_pct = 0.30
					self.logger.info(f"Detected small context model ({context_window:,} tokens)")
				
				# Calculate max messages from budget
				avg_tokens_per_message = 160
				message_token_budget = int(context_window * message_budget_pct)
				adaptive_max_messages = int(message_token_budget / avg_tokens_per_message)
				
				# Apply reasonable bounds
				adaptive_max_messages = max(50, min(adaptive_max_messages, 2000))
				
				self.logger.info(
					f"Adaptive message history: {adaptive_max_messages} messages "
					f"(~{message_token_budget:,} tokens, {message_budget_pct:.1%} of {context_window:,} window)"
				)
			else:
				# Fallback for unknown models
				adaptive_max_messages = 100
				self.logger.warning(
					f"Could not determine context window for {self._model_name}, "
					f"using fallback: {adaptive_max_messages} messages"
				)
			
			# Message History vs H-MEM (complementary systems):
			#   - Message History: "Did I already read file X? What was the exact error?"
			#   - H-MEM: "What phase am I in? What concepts have I learned?"

		self.history = MessageHistory(max_messages=adaptive_max_messages)
		self.logger.info(
			f"MessageManager initialized with LIMITED history (max_messages={adaptive_max_messages}). "
			f"Long-term memory: TaskContextManager (hierarchical). "
			f"Token limits: max={self.max_input_tokens}, safe={self.safe_input_tokens}"
		)
		self.task = task
		self.action_descriptions = action_descriptions
		self.IMG_TOKENS = image_tokens
		self.include_attributes = include_attributes
		self.max_error_length = max_error_length
		self.max_actions_per_step = max_actions_per_step
		self.sensitive_data = sensitive_data
		# HISTORY_SECRET_SCRUB (default ON): the pattern backstop for UNregistered
		# secrets lives inside _filter_sensitive_data, but every caller gated it
		# behind `if self.sensitive_data:` — which is empty by default, so the
		# default-ON backstop never ran and sk-/AKIA/Bearer/PEM leaking through a
		# tool result persisted to message_history.json + compaction checkpoints in
		# the clear. Cache the flag once so the write/ephemeral paths can invoke the
		# filter even with no allowlist registered.
		from core.env import bool_env as _bool_env
		self._history_secret_scrub = _bool_env("HISTORY_SECRET_SCRUB", True)
		self.session_id = session_id
		self.add_task_message = add_task_message

		# Store tool call tracker for ID management
		self.tool_call_tracker = tool_call_tracker

		# Store task context manager for hierarchical memory (Phase 2)
		self.task_context_manager = task_context_manager

		# FIX (Jan 2026): Initialize CompactionManager for context-aware compaction
		# This is now the single source of truth for compaction thresholds and logic
		from modules.memory.task.compaction_manager import CompactionManager
		self.compaction_manager = CompactionManager.for_model(self._model_name)

		# Store MCP server info for dynamic prompt generation
		self.mcp_servers = mcp_servers or {}

		# Session's loaded tool_ids — lets SystemPrompt gate config-aware sections
		# (e.g. <anysite>, <browser-tools>) on the tools actually loaded this session,
		# not a global flag. None is preserved (NOT collapsed to []): it means the
		# caller never declared a tool set, and SystemPrompt keeps legacy sections on.
		self.tool_ids = list(tool_ids) if tool_ids is not None else None

		# Store native tools flag for message conversion
		self.use_native_tools = use_native_tools

		# Initialize ephemeral message buffer (one-shot, not counted toward history)
		self._ephemeral_messages: List[BaseMessage] = []

		# PR13: skills are injected as a pinned foundation user message (set later),
		# not embedded in the system prompt (keeps the system prompt stable/cacheable).
		self._skill_message: Optional[BaseMessage] = None
		# Token cost of the pinned skill message, kept in the foundation totals so
		# compaction/overflow math sees skills (which left the system prompt in PR13).
		self._skill_message_tokens: int = 0

		# polyrob Phase C: SOUL/IDENTITY self-context is pinned as a frozen foundation
		# user message (set later), NOT embedded in the system prompt — same rationale
		# as skills (keeps the system prompt stable/cacheable). Empty => inert.
		self._self_context_message: Optional[BaseMessage] = None
		self._self_context_tokens: int = 0

		# C9: auto-loaded project context (CLAUDE.md/AGENTS.md/.cursorrules) is pinned
		# as a frozen foundation message (set later), CLI-only, default-OFF on server.
		# Placed AFTER self-context and BEFORE the initial task (same pattern as
		# self_context but project-scoped rather than identity-scoped). Empty => inert.
		self._project_context_message: Optional[BaseMessage] = None
		self._project_context_tokens: int = 0

		# Model-identity SSOT: the model/provider the agent is ACTUALLY running on,
		# pinned as a frozen foundation message so the agent can state it truthfully
		# instead of grepping config/.env (which leaked secrets). Set at session
		# start from the built LLM and refreshed on a live /model swap. Empty => inert.
		self._runtime_identity_message: Optional[BaseMessage] = None
		self._runtime_identity_tokens: int = 0

		# Initialize checkpoint attributes for message history persistence
		self._history_checkpoint: List[ManagedMessage] = []
		self._checkpoint_token_count: int = 0

		# SECURITY FIX: Initialize lock to protect deque operations from race conditions
		# This prevents data corruption when list/deque conversions happen during concurrent access
		self._history_lock = threading.RLock()  # RLock allows reentrant locking

		# Initialize system message - CRITICAL FIX: Store separately from history to prevent deque eviction
		# The system message is NOT added to self.history.messages because the deque has a max length
		# and will automatically drop old messages, potentially losing the system message.
		# Instead, we store it separately and prepend it when get_messages() is called.
		if system_message is not None:
			# Use the provided system message directly
			self.logger.debug("Using provided system message from profile")
			self._system_message = system_message
			self.system_prompt = system_message
		else:
			# Create system message from prompt class
			# SystemPrompt accepts action_descriptions, max_actions_per_step, use_native_tools, model_name, provider, and mcp_servers
			# Pass model_name and provider so SystemPrompt can inject model-specific instructions (e.g., for Grok)
			# Pass mcp_servers so SystemPrompt can generate dynamic examples using actual server/tool names
			prompt_instance = self.system_prompt_class(
				self.action_descriptions,
				max_actions_per_step=self.max_actions_per_step,
				use_native_tools=use_native_tools,
				model_name=self._model_name,
				provider=self._provider_name,
				mcp_servers=self.mcp_servers,
				persona_block=self._persona_block,
				tool_ids=self.tool_ids,
				include_vision=include_vision,
			)

			# SystemPrompt has get_system_message() method that returns a SystemMessage
			system_message = prompt_instance.get_system_message()
			self._system_message = system_message
			self.system_prompt = system_message
		
		# Calculate and store system message token count separately
		self._system_message_tokens = self._count_message_tokens(self._system_message)
		self.logger.debug(f"System message stored separately ({self._system_message_tokens} tokens) - will not be evicted by deque")
		
		# CRITICAL FIX: Store initial task separately (like SystemMessage) to prevent deque eviction
		# This ensures the original task is ALWAYS visible to the agent, even after 100+ steps
		# Without this, after 20 messages, the agent loses context of what it's supposed to do
		if self.add_task_message:
			# Check if this is a new session (empty history - system message stored separately)
			history_size = len(self.history.messages)

			if history_size == 0:
				# New session - store initial task SEPARATELY (not in deque)
				self._initial_task_message = HumanMessage(content=self.task)
				self._initial_task_tokens = self._count_message_tokens(self._initial_task_message)
				self.logger.info(f"Initial task message stored separately ({self._initial_task_tokens} tokens) - will not be evicted by deque")
			else:
				# Continuing session - task already established
				# Set to None so it's not included in get_messages()
				self._initial_task_message = None
				self._initial_task_tokens = 0
				self.logger.info(
					f"Skipping initial task message for continuous execution "
					f"(history already has {history_size} messages)"
				)
		else:
			# Task message disabled
			self._initial_task_message = None
			self._initial_task_tokens = 0
		
		# Log token usage at initialization (include all separate storage tokens)
		model_name = self.model_name
		total_tokens = self.history.total_tokens + self._system_message_tokens + self._initial_task_tokens
		self.logger.info(
			f"Initialized MessageManager with {total_tokens} tokens for model {model_name} "
			f"(system: {self._system_message_tokens}, task: {self._initial_task_tokens}, history: {self.history.total_tokens})"
		)
		self.logger.debug(f"Token limit: {self.max_input_tokens}, remaining: {self.max_input_tokens - total_tokens}")

	def _detect_model_name(self) -> str:
		"""Detect and cache model name from LLM instance (single source of truth)."""
		# Try to get specific model name from various attributes
		for attr in ['model_name', 'model', '_model_name', 'deployment_name']:
			if hasattr(self.llm, attr):
				model = getattr(self.llm, attr, None)
				if model:
					return model

		# Fallback to provider or class name
		return self._provider_name if hasattr(self, '_provider_name') and self._provider_name != "unknown" else self.llm.__class__.__name__

	# REMOVED: _detect_provider_name - now using consolidated detect_llm_provider from utils.py

	@property
	def model_name(self) -> str:
		"""Public accessor for cached model name (single source of truth)."""
		return self._model_name

	@model_name.setter
	def model_name(self, value: str) -> None:
		"""Update model name and re-detect provider (for LLM fallback scenarios)."""
		self._model_name = value
		# Re-detect provider based on new model name
		from agents.task.utils import detect_llm_provider
		self._provider_name = detect_llm_provider(None, value)
		self.logger.info(f"Model name updated to: {value} (provider: {self._provider_name})")

	@property
	def provider_name(self) -> str:
		"""Public accessor for cached provider name (single source of truth)."""
		return self._provider_name

	@provider_name.setter
	def provider_name(self, value: str) -> None:
		"""Set the cached provider name authoritatively (for live model swaps).

		The ``model_name`` setter re-detects the provider FROM the model name
		(``detect_llm_provider``); this setter lets ``swap_model`` override that
		with the registry-canonical provider label after the model is updated."""
		self._provider_name = value

	def recalibrate_for_model(self, model_name: str) -> None:
		"""Recompute token budgets + compaction manager for a newly-swapped model.

		Init-time values (``max_input_tokens``/``safe_input_tokens``/
		``completion_reserve`` and the ``CompactionManager``) are sized for the
		ORIGINAL model. After a live ``swap_model`` they must be re-derived for the
		new model's context window. Reuses ``_calculate_token_limits`` (the init-time
		SSOT formula) so the math isn't duplicated, and rebuilds the compaction
		manager the same way ``__init__`` does. Unlike ``recalibrate_token_counts``
		(shrink-only, for a smaller fallback), this always adopts the new model's
		budget in either direction.
		"""
		self._model_name = model_name
		# Re-derive from the new model (None override => auto-calc from the registry,
		# still honouring the TASK_MAX_INPUT_TOKENS env cap inside the helper).
		self.max_input_tokens, self.safe_input_tokens, self.completion_reserve = \
			self._calculate_token_limits(self.llm, None)
		from modules.memory.task.compaction_manager import CompactionManager
		self.compaction_manager = CompactionManager.for_model(self._model_name)
		self.logger.info(
			f"Recalibrated for swapped model {model_name}: "
			f"max_input={self.max_input_tokens}, safe_input={self.safe_input_tokens}, "
			f"completion_reserve={self.completion_reserve}"
		)


	@classmethod
	def from_config(
		cls,
		llm: BaseChatModel,
		task: str,
		action_descriptions: str,
		system_prompt_class: Type,  # Type of SystemPrompt
		config: MessageManagerConfig,
		logger: Optional[logging.Logger] = None
	) -> 'MessageManager':
		"""Create MessageManager from config object.

		SINGULAR RESPONSIBILITY: Config object contains ALL settings.
		No fallbacks, no deprecated parameters, no duplication.

		Args:
			llm: The language model
			task: Task description
			action_descriptions: Available actions
			system_prompt_class: System prompt class
			config: Configuration object with all settings (REQUIRED)
			logger: Optional logger instance for DI

		Returns:
			Configured MessageManager instance
		"""
		if config is None:
			raise ValueError("config is required - no defaults, explicit configuration only")

		instance = cls(
			llm=llm,
			task=task,
			action_descriptions=action_descriptions,
			system_prompt_class=system_prompt_class,
			max_input_tokens=config.max_input_tokens,
			max_actions_per_step=config.max_actions_per_step,
			**config.to_dict()
		)

		# Override logger if provided (DI pattern)
		if logger is not None:
			instance.logger = logger

		return instance


	def _add_message_with_tokens(self, message: BaseMessage, position: Optional[int] = None, _internal: bool = False) -> None:
		"""Add message with token count metadata and auto-trim if needed.

		MessageManager keeps ONLY recent conversation for immediate context.
		Long-term memory is handled by TaskContextManager.

		Args:
			message: The message to add
			position: Optional position to insert at
			_internal: If True, bypass single-writer enforcement for AI/Tool messages (used internally)
		"""

		# filter out sensitive data from the message. Run when an allowlist is
		# registered OR the HISTORY_SECRET_SCRUB backstop is on (default) — the
		# backstop must scrub unregistered secrets even with an empty allowlist.
		if self.sensitive_data or self._history_secret_scrub:
			message = self._filter_sensitive_data(message)

		token_count = self._count_message_tokens(message)
		from datetime import datetime
		metadata = MessageMetadata(
			input_tokens=token_count,
			timestamp=datetime.now().isoformat(),
		)

		# SECURITY FIX: Protect all deque operations with lock to prevent race conditions
		# The list/deque conversion and trimming must be atomic
		with self._history_lock:
			# Add message WITHOUT updating total_tokens (will be done via increment)
			managed_msg = ManagedMessage(message=message, metadata=metadata)
			maxlen = self.history.max_messages
			if position is None:
				# A deque(maxlen).append() auto-evicts the leftmost message when full
				# WITHOUT decrementing total_tokens, and the trim loop below can't
				# compensate (len can never EXCEED maxlen). Subtract the soon-to-be
				# evicted message's tokens first so total_tokens doesn't drift upward.
				msgs = self.history.messages
				if maxlen and msgs and len(msgs) >= maxlen:
					ev = msgs[0]
					if getattr(ev, 'metadata', None):
						self.history.total_tokens = max(
							0, self.history.total_tokens - ev.metadata.input_tokens)
				msgs.append(managed_msg)
			else:
				# FIXED: deque with maxlen doesn't support insert() when full
				# Convert to list, insert, then back to deque
				messages_list = list(self.history.messages)
				messages_list.insert(position, managed_msg)
				# deque(maxlen) construction silently drops the leftmost overflow;
				# decrement those tokens too so total_tokens stays consistent.
				if maxlen and len(messages_list) > maxlen:
					for ev in messages_list[: len(messages_list) - maxlen]:
						if getattr(ev, 'metadata', None):
							self.history.total_tokens = max(
								0, self.history.total_tokens - ev.metadata.input_tokens)
				self.history.messages = deque(messages_list, maxlen=maxlen)

			# Auto-trim if exceeding limit
			while len(self.history.messages) > self.history.max_messages:
				# Keep system message (index 0) and task message (index 1, if present)
				# Start trimming from index 2 onwards to preserve context structure
				# System: index 0 (always kept)
				# Task: index 1 (kept if it's the initial HumanMessage with task)
				# Hierarchical context: injected dynamically, not in history
				# Recent messages: index 2+ (these get trimmed)

				# Find the oldest trimmable message (skip system and initial task)
				remove_index = 2 if len(self.history.messages) > 2 else 1

				# Safety check: don't trim if we only have system + task messages
				if remove_index >= len(self.history.messages):
					self.logger.warning("Cannot trim - would remove essential messages")
					break

				removed_msg = self.history.messages.pop(remove_index)

				# Decrement token count for removed message
				if hasattr(removed_msg, 'metadata') and removed_msg.metadata:
					removed_tokens = removed_msg.metadata.input_tokens
					self.history.total_tokens = max(0, self.history.total_tokens - removed_tokens)

				self.logger.debug(f"Trimmed old message from recent history: {type(removed_msg.message).__name__}")

		# Use incremental token counting for new message
		self._increment_token_count(token_count)


	def invalidate_hmem_cache(self) -> None:
		"""Invalidate the H-MEM context cache.
		
		FIX (Dec 12, 2025): Now calls TaskContextManager.invalidate_context_cache()
		to force a fresh hierarchical search on next context injection.
		
		Note: The cache now auto-invalidates per step, so this is only needed
		when you want immediate cache invalidation (e.g., after major findings).
		"""
		if self.task_context_manager and self.session_id:
			try:
				self.task_context_manager.invalidate_context_cache(self.session_id)
			except Exception as e:
				self.logger.debug(f"H-MEM cache invalidation failed: {e}")


	def add_message(self, message: BaseMessage, position: Optional[int] = None, _internal: bool = False) -> None:
		"""Add a message to history - public wrapper for _add_message_with_tokens

		IMPORTANT: This method enforces single-writer constraints for AI and Tool messages.
		Only MessageManager methods (add_model_output, add_tool_response) should add these types.

		Args:
			message: The message to add
			position: Optional position to insert at
			_internal: Private flag used by internal methods to bypass enforcement
		"""
		# ENFORCEMENT: Single Writer for AI/Tool Messages
		# Prevent external callers from directly adding AI or Tool messages
		# AIMessage, ToolMessage imported at module level from modules.llm.messages
		if not _internal:
			if isinstance(message, (AIMessage, ToolMessage)):
				from core.exceptions import BotError

				error_msg = (
					f"Direct addition of {type(message).__name__} not allowed. "
					f"Use add_model_output() for AI messages or add_tool_response() for Tool messages."
				)
				self.logger.error(f"[SINGLE_WRITER] {error_msg}")

				class MessageFlowError(BotError):
					"""Error in message flow control"""
					pass

				raise MessageFlowError(error_msg)

		self._add_message_with_tokens(message, position)

	def add_human_message(self, content: str, position: Optional[int] = None) -> None:
		"""Create a HumanMessage with the given content and add it to history"""
		message = HumanMessage(content=content)
		self._add_message_with_tokens(message, position)

	def append_user_turn(self, text: str) -> None:
		"""Append a plain user message at the END of history (a conversational turn).

		Contrast with inject_user_guidance, which inserts at position 1 with
		task-directive framing for the HITL resume path. This is an ordinary
		trailing turn, routed through _add_message_with_tokens so it participates
		in token accounting and overflow trimming like any other user message.
		"""
		self._add_message_with_tokens(HumanMessage(content=text))

	def set_skill_message(self, content: Optional[str]) -> None:
		"""PR13: pin skills as a foundation user message instead of the system prompt.

		Stored as a SKILL-origin control message and injected in the foundation
		(after the initial task) by get_messages(). Keeps the system prompt stable
		for prompt caching and keeps injected skills distinguishable from user input.
		Pass falsy content to clear.
		"""
		if content and content.strip():
			self._skill_message = make_control_message(content, MessageOrigin.SKILL)
			self._skill_message_tokens = self._count_message_tokens(self._skill_message)
		else:
			self._skill_message = None
			self._skill_message_tokens = 0

	def set_self_context_message(self, content: Optional[str]) -> None:
		"""polyrob Phase C: pin the SOUL/IDENTITY self-context as a frozen foundation
		user message instead of the system prompt.

		Stored as a SELF_CONTEXT-origin control message and injected in the
		foundation (before skills) by get_messages()/get_messages_for_llm(). Keeps the
		system prompt stable for prompt caching and reads as an authoritative
		self-definition block distinct from user input. Set once at session start
		(frozen); pass falsy content to clear (the inert default).
		"""
		if content and content.strip():
			self._self_context_message = make_control_message(content, MessageOrigin.SELF_CONTEXT)
			self._self_context_tokens = self._count_message_tokens(self._self_context_message)
		else:
			self._self_context_message = None
			self._self_context_tokens = 0

	def set_project_context_message(self, content: Optional[str]) -> None:
		"""C9: pin the auto-loaded project context (CLAUDE.md/AGENTS.md/.cursorrules) as a
		frozen foundation message (CLI-only, default-OFF on server).

		Stored as a PROJECT_CONTEXT-origin control message and injected in the
		foundation AFTER self-context and BEFORE the initial task by
		get_messages()/get_messages_for_llm(). Keeps the system prompt stable for
		prompt caching. Set once at session start (frozen); pass falsy content to
		clear (the inert default on server / when no context file found).
		"""
		if content and content.strip():
			self._project_context_message = make_control_message(content, MessageOrigin.PROJECT_CONTEXT)
			self._project_context_tokens = self._count_message_tokens(self._project_context_message)
		else:
			self._project_context_message = None
			self._project_context_tokens = 0

	def set_runtime_identity(self, model: Optional[str], provider: Optional[str]) -> None:
		"""Pin the model/provider the agent is ACTUALLY running on as a foundation message.

		So the agent can answer "what model are you" truthfully instead of reading
		config/.env (which leaked secrets). Set at session start from the built LLM
		and refreshed on a live /model swap. Falsy model => clear (inert). Kept in the
		foundation (not the system prompt) so the prompt stays cacheable; on a swap the
		refreshed line is one short block, so the cache cost is negligible.
		"""
		if model and str(model).strip():
			prov = str(provider).strip() if provider else "unknown"
			content = (
				f"You are currently running on model \"{model}\" via provider \"{prov}\". "
				"State this if asked which model/provider you are — do NOT read env/config "
				"files to answer; this line is authoritative."
			)
			self._runtime_identity_message = make_control_message(content, MessageOrigin.RUNTIME_IDENTITY)
			self._runtime_identity_tokens = self._count_message_tokens(self._runtime_identity_message)
		else:
			self._runtime_identity_message = None
			self._runtime_identity_tokens = 0

	def add_file_paths(self, file_paths: list[str]) -> None:
		"""Add file paths message with properly escaped paths.
		
		Args:
			file_paths: List of file paths to add
		"""
		# Sanitize file paths to prevent injection
		def sanitize_paths(paths: list[str]) -> list[str]:
			if not paths:
				return []
			return [p.replace('"""', '\\"\\"\\"').replace('\\', '\\\\') for p in paths]
			
		safe_paths = sanitize_paths(file_paths)
		content = f'Here are file paths you can use: {safe_paths}'
		msg = HumanMessage(content=content)
		self._add_message_with_tokens(msg)

	def add_new_task(self, new_task: str) -> None:
		"""Add a new task message with properly escaped task text.
		
		Args:
			new_task: The new task description
		"""
		# Reuse the same escaping function from task_instructions
		def escape_task_text(text: str) -> str:
			if not text:
				return ""
			# Replace potential control sequences
			escaped = (text.replace('"""', '\"\"\"')  # Escape triple quotes
					  .replace('\\', '\\\\')        # Escape backslashes
					  .replace('{', '{{')           # Escape curly braces for f-strings
					  .replace('}', '}}'))
			return escaped
		
		# Apply escaping directly instead of extracting from another message
		safe_task = escape_task_text(new_task)
		content = f'Your new ultimate task is: """{safe_task}""". Take the previous context into account and finish your new ultimate task. '
		msg = HumanMessage(content=content)
		self._add_message_with_tokens(msg)

	def add_plan(self, plan: Optional[str], position: Optional[int] = None) -> None:
		"""Add a plan message as AIMessage.

		This is an internal method that bypasses enforcement.
		"""
		if plan:
			msg = AIMessage(content=plan)
			self._add_message_with_tokens(msg, position, _internal=True)

