"""Step execution loop mixin (code-motion from service.py)."""

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
# Model limits now come from modules.llm.model_registry

# Native message types
from modules.llm.messages import (
	AIMessage,
	BaseMessage,
	HumanMessage,
	SystemMessage,
	ToolMessage,
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


def _detect_service_for_action(action_name: str) -> str:
	"""Lazy delegate to the module-level helper in service.py.

	Defined here so the moved ``_step_impl`` body (which references
	``_detect_service_for_action`` as a global) resolves at runtime without a
	circular import at module load time.
	"""
	from agents.task.agent.service import _detect_service_for_action as _impl
	return _impl(action_name)


def _is_fatal_step_error(error_str: str, billing_failover_enabled: bool) -> bool:
	"""Classify a step-loop exception as fatal (halt immediately) vs recoverable.

	`error_str` must be lowercased. When `billing_failover_enabled` is True, billing/
	quota errors are NOT fatal here — they flow to `_handle_step_error` which attempts a
	provider swap before halting (HIGH-1). With the flag off, classification is identical
	to the historical inline check (all billing/quota terms fatal).
	"""
	is_billing = 'insufficient_quota' in error_str or 'billing' in error_str
	# CO-F5: bare '429'/'rate limit' are NOT fatal — they're retryable. Routing them
	# here halted the session before `_handle_step_error` could handle them. In practice
	# a plain-Exception '429'/'rate limit' string is caught by the generic `is_llm_error`
	# block (error_recovery.py ~:203-296: matches 'rate limit'/'429', then circuit
	# breaker → provider fallback → backoff), which now owns all retryable rate-limit
	# handling.
	return (
		('quota' in error_str and 'exceeded' in error_str) or
		'authentication' in error_str or
		'api key' in error_str or
		(is_billing and not billing_failover_enabled)
	)


class StepMixin:
	"""Mixin extracted verbatim from agents/task/agent/service.py (pure code-motion)."""

	@time_execution_async('--step')
	async def step(self, step_info: Optional[AgentStepInfo] = None) -> None:
		"""Execute one step of the task with runtime safety guards"""
		# ALWAYS use timeout protection - default to 600 seconds if not configured
		timeout_seconds = self.step_timeout_seconds or 600
		try:
			# Track step start time for stall detection
			self.state.last_step_start_time = time.time()

			# Check AgentState loop detection BEFORE step
			if self.state.is_stuck_in_loop():
				self.logger.warning("🚨 AgentState detected stuck loop")
				self._trigger_loop_intervention("AgentState loop detection triggered (multi-signal)")
			elif self.state.is_showing_loop_symptoms():
				# Early warning - log but don't intervene
				self.logger.info(
					f"⚠️ Loop symptoms detected: similar_actions={self.state.consecutive_similar_actions}, "
					f"steps_since_finding={self.state.n_steps - self.state.last_finding_step}"
				)

			# H-MEM loop signal - log only, no intervention
			if hasattr(self, 'task_context_manager') and self.task_context_manager:
				hmem_signal = self.task_context_manager.check_loop_signal(self.session_id)
				if hmem_signal:
					self.logger.info(f"📊 H-MEM: {hmem_signal}")
					self.task_context_manager.reset_loop_signal(self.session_id)

			async with asyncio.timeout(timeout_seconds):
				await self._step_impl(step_info)

			# Reset stall detection on successful step
			self.state.last_activity_time = time.time()

		except asyncio.TimeoutError:
			self.logger.error(f"Step timed out after {timeout_seconds} seconds", exc_info=True)
			self.state.consecutive_failures = self.max_failures

			# Try to save progress before failing
			try:
				await self._save_progress_on_timeout()
			except Exception as e:
				self.logger.warning(f"Failed to save progress on timeout: {e}")

			raise

	async def _prepare_step(self, step_info: Optional[AgentStepInfo] = None):
		"""Phase 1: pre-LLM setup — refresh context, build the state message, return (state, input_messages)."""
		# Check cancellation before starting step execution
		if self._cancelled:
			self.logger.warning(f"❌ Step execution cancelled before start (step {self.state.n_steps})")
			raise asyncio.CancelledError("Agent cancelled")

		# Update step counter if step_info is provided
		if step_info is not None and hasattr(step_info, 'step_number'):
			# Use the provided step number + 1 (since step_number is 0-indexed)
			self.state.n_steps = step_info.step_number + 1

		# Use step number only in log to reduce verbosity
		self.logger.info(f'📍 Step {self.state.n_steps}')

		# FIX: Invalidate H-MEM cache at start of each step to ensure fresh context
		# This prevents duplicate hierarchical searches within a step
		self.message_manager.invalidate_hmem_cache()

		# FIX (Context Optimization Phase 2): Log context usage breakdown
		# Log every 10 steps or first 3 steps for early visibility
		if self.state.n_steps % 10 == 0 or self.state.n_steps <= 3:
			self._log_context_breakdown()

		# Periodic memory monitoring (less aggressive)
		if self.state.n_steps % 10 == 0:  # Check less frequently
			try:
				import psutil
				import gc
				# NOTE: `os` is module-level (top of file); do NOT re-import it locally —
				# a function-local `import os` would shadow it for this whole method and
				# turn any earlier os.* use into an UnboundLocalError.

				process = psutil.Process(os.getpid())
				memory_mb = process.memory_info().rss / 1024 / 1024

				# NOTE: Removed truncation of _last_result - it was destroying important context
				# Previous results are needed for task continuity

				# Check memory usage
				memory_bytes = self.message_manager.get_memory_usage_estimate()
				memory_mb = memory_bytes / (1024 * 1024)
				if memory_mb > 50:  # If message history is using > 50MB
					self.logger.warning(f"Message history using {memory_mb:.1f}MB - consider checkpointing")

				# Periodic memory cleanup
				self.cleanup_memory(force=False)

				# Log if high memory usage
				if memory_mb > 1000:  # 1GB
					self.logger.info(f"Memory usage: {memory_mb:.0f}MB at step {self.state.n_steps}")


			except Exception as e:
				self.logger.debug(f"Memory cleanup error: {e}")

		# Context compaction check - EVERY STEP with tiered thresholds
		# FIX (Jan 2026): Changed from every 10 steps to every step
		# MCP responses can add 60K+ tokens in a single step, causing overflow before check
		try:
			usage_pct = self.message_manager.get_context_usage_percent()

			if usage_pct >= 95:
				# CRITICAL: Emergency prune immediately - about to overflow.
				# Non-LLM, always runs (the overflow safety net) - no cooldown.
				self.logger.warning(f"🚨 Critical context: {usage_pct:.1f}% - emergency prune")
				self.message_manager.emergency_context_prune()
			elif usage_pct >= 85:
				# HIGH: LLM compaction to intelligently summarize.
				# Flow-efficiency D3-a: llm_compact_history is an EXTRA LLM call. Apply a
				# step cooldown so it does not re-fire every step while usage lingers in the
				# 85-95% band (a single large MCP result can re-cross 85% each step). If it
				# climbs to >=95%, the emergency prune above handles overflow without an LLM call.
				from agents.task.constants import COMPACTION_COOLDOWN_STEPS
				last_compaction = getattr(self, '_last_llm_compaction_step', None)
				steps_since = (
					self.state.n_steps - last_compaction
					if last_compaction is not None else COMPACTION_COOLDOWN_STEPS
				)
				if steps_since >= COMPACTION_COOLDOWN_STEPS:
					self.logger.info(f"📦 High context: {usage_pct:.1f}% - LLM compaction")
					# P2-15: only start the cooldown when compaction ACTUALLY ran. A
					# transient abort (rate-limit/connection blip), a <15-message history,
					# or an anti-thrash no-op returns False and is designed to retry next
					# step — stamping the cooldown regardless delayed that retry by
					# COMPACTION_COOLDOWN_STEPS while usage sat in the 85-95% band.
					if await self.message_manager.llm_compact_history():
						self._last_llm_compaction_step = self.state.n_steps
				else:
					self.logger.info(
						f"📦 High context: {usage_pct:.1f}% - compacted {steps_since} step(s) ago, "
						f"skipping LLM compaction (cooldown {COMPACTION_COOLDOWN_STEPS})"
					)
			elif usage_pct >= 70:
				# WARNING: Log for visibility every 5 steps at this level
				if self.state.n_steps % 5 == 0:
					self.logger.info(f"⚠️ Context at {usage_pct:.1f}% - monitoring")
		except Exception as e:
			self.logger.debug(f"Context compaction error: {e}")

		state = None
		browser_context = None  # bound before the try so the return is always safe

		try:
			# Get fresh browser context from orchestrator
			browser_context = await self.get_browser_context()

			# Only get browser state if we have a browser context
			if browser_context:
				try:
					# FIX (Dec 2025): Only capture screenshots when vision is enabled
					# This saves ~100-200ms per step for non-vision tasks
					# Pass use_vision flag to control screenshot capture
					state = await browser_context.get_state(capture_screenshot=self.use_vision)

					# Apply page content truncation early for token safety
					if hasattr(state, 'page_content') and state.page_content:
						from agents.task.robust_parse_config import RobustParseConfig
						state.page_content = RobustParseConfig.truncate_page_content(state.page_content)
				except Exception as e:
					self.logger.warning(f"Error getting browser state: {str(e)}")
					# Don't fail - just continue without browser state
					state = None
			else:
				# No browser context - use a minimal state
				self.logger.debug("No browser context available - running without browser state")
				# Create a minimal state object for non-browser tasks
				from agents.task.agent.views import BrowserState
				from tools.dom.views import DOMElementNode, SelectorMap
				# BrowserState requires element_tree and selector_map from DOMState
				# Create a minimal DOMElementNode instance
				minimal_element = DOMElementNode(
					tag_name='html',
					xpath='/',
					attributes={},
					children=[],
					is_visible=True,
					parent=None
				)
				state = BrowserState(
					url="",
					title="No Browser",
					tabs=[],
					element_tree=minimal_element,
					selector_map={}
				)
		except Exception as e:
			self.logger.error(f"Unexpected error in state setup: {str(e)}", exc_info=True)
			# Create minimal state as fallback
			from agents.task.agent.views import BrowserState
			from tools.dom.views import DOMElementNode, SelectorMap
			# Create a minimal DOMElementNode for no-browser state
			empty_element = DOMElementNode(
				tag_name="html",
				xpath="/html",
				attributes={},
				children=[]
			)
			state = BrowserState(
				url="",
				title="No Browser",
				tabs=[],
				element_tree=empty_element,
				selector_map=SelectorMap()
			)

		self._check_if_stopped()

		# FIXED: Force recalibration after large actions
		from agents.task.robust_parse_config import RobustParseConfig
		should_recalibrate = False

		# Check if we need to recalibrate after large actions
		if (RobustParseConfig.FORCE_RECALIBRATE_AFTER_LARGE_ACTIONS and
			self._last_result and any(
				result.extracted_content and len(result.extracted_content) > RobustParseConfig.LARGE_ACTION_THRESHOLD
				for result in self._last_result
			)):
			should_recalibrate = True
			self.logger.debug("Forcing recalibration after large action result")

		# Recalibrate token counts on first step, every 5 steps, or after large actions
		if self.state.n_steps == 1 or self.state.n_steps % 5 == 0 or should_recalibrate:
			self.message_manager.recalibrate_token_counts()

		# Pass previous brain state for memory continuity
		previous_brain = None
		if hasattr(self, '_last_model_output') and self._last_model_output and hasattr(self._last_model_output, 'current_state'):
			previous_brain = self._last_model_output.current_state

		# FIX (Jan 2026): Conditionally include browser state to prevent context bleeding
		# The AgentMessagePrompt will also check _has_meaningful_browser_state()
		# so even if we pass True here, empty states won't bleed through
		include_browser = self._has_active_browser_usage()

		# B-T2: prefetch & inject recalled memory (first step, then every
		# MEMORY_PREFETCH_CADENCE steps when set). Fail-open; inert unless an
		# external memory provider is registered.
		await self._maybe_prefetch_memory()

		# Task 5: session-start "recent activity" digest — first step only, chat/
		# owner sessions only (never goal/cron/sub-agent). Fail-open; inert unless
		# AutonomyConfig.episodic_digest_inject() is on.
		await self._maybe_inject_episodic_digest()

		# Task 6: idle-reset continuity bridge — first step only, chat/owner sessions
		# only (never goal/cron/sub-agent). Fail-open; inert unless
		# AutonomyConfig.continuity_bridge_enabled() is on and a prior chat episode
		# with a summary exists for this session's thread_key.
		await self._maybe_inject_continuity_bridge()

		# §7.5: autonomous continuity bridge — first step only, AUTONOMOUS sessions
		# only (goal/cron; never chat/sub-agent). Carries recent activity into the
		# tick so it stops re-deriving "nothing new". Fail-open; inert unless
		# AutonomyConfig.autonomous_continuity_bridge() is on.
		await self._maybe_inject_autonomous_continuity()

		# CO-F7: mark the once-per-SESSION bootstrap as done immediately after
		# both injectors above have had their one shot on the session's first
		# executed step. This is deliberately set here (not at the end of
		# run()) so that a turn-1 run() that breaks BEFORE step() ever runs
		# (cancellation, resumed-from-done/stopped, too-many-failures) leaves
		# the flag False — a later real turn still gets to inject once. A
		# _continue_session=True turn resets n_steps too, so its own first step
		# also lands here; the injectors' own `_session_bootstrap_done` guard
		# (see memory_prefetch.py) makes this idempotent — they've already
		# fired-or-declined by the time this line runs, so re-setting an
		# already-True flag is a no-op.
		if self.state.n_steps == 1:
			self._session_bootstrap_done = True

		self.message_manager.add_state_message(
			state,
			self._last_result,
			step_info,
			self.use_vision,
			previous_brain,
			include_browser_state=include_browser
		)

		# Planning artifacts (plan.json, todo.md) are maintained directly by the agent

		# User messages are drained in the run loop (after each step and after completion)
		# This prevents unnecessary LLM calls just to check for new messages
		# See run() method for message draining logic

		input_messages = self.message_manager.get_messages()

		# DIAGNOSTIC: Log control flags before check to help debug unexpected stops
		if self.state.stopped:
			self.logger.info(f'⚠️  Control flags before get_next_action: stopped={self.state.stopped}')

		self._check_if_stopped()

		return state, input_messages, browser_context

	async def _record_step(self, state, model_output, input_messages) -> None:
		"""Recording tail of a step: persist last model output, run loop/repetition
		detection, fire the new-step callback, save the conversation, and refresh
		the state message. Extracted verbatim from _step_impl (behavior-preserving).
		"""
		# Store model output for next step
		self._last_model_output = model_output

		# FIX #4: Track action in AgentState for loop detection
		if model_output and hasattr(model_output, 'current_state') and model_output.current_state:
			action_summary_for_tracking = model_output.current_state.memory[:200] if model_output.current_state.memory else "No memory"
			self.state.track_action(action_summary_for_tracking)

		# Action repetition detection
		if model_output and hasattr(model_output, 'action') and model_output.action:
			# Create a simplified representation of the action for comparison
			import hashlib
			# --- UPGRADE: use order-independent semantic hash ----------------
			action_dump = []
			for a in model_output.action:
				# Dump each action with sorted keys so param order does not matter
				action_dump.append(a.model_dump(mode="json", exclude_unset=True, by_alias=True))
			action_hash = hashlib.md5(json.dumps(action_dump, sort_keys=True).encode()).hexdigest()
			# ---------------------------------------------------------------

			# Check for repeated actions with improved detection
			if self._previous_actions and action_hash == self._previous_actions[-1]:
				self._action_repetition_counter += 1
				# Log warnings after 3+ repetitions (more aggressive)
				if self._action_repetition_counter >= 3:
					self.logger.warning(f"Same action repeated {self._action_repetition_counter} times")

				# FIX #1: Respect the configured threshold instead of hardcoding 8
				# The config value is typically 2-3 for faster loop detection
				max_reps = self._max_allowed_repetitions  # Removed the max(..., 8) override
				if self._action_repetition_counter >= max_reps:
					self._trigger_loop_intervention(f"Action repeated {self._action_repetition_counter} times")
			else:
				# Different action, reset counter
				self._action_repetition_counter = 0

			# Update action history (deque automatically maintains max size)
			self._previous_actions.append(action_hash)

			# FIX #2: Enhanced pattern detection with IMMEDIATE intervention
			if len(self._previous_actions) >= 4:
				# Convert deque to list for slice operations
				prev_actions_list = list(self._previous_actions)

				# Check for A-B-A-B pattern - IMMEDIATE intervention
				if (prev_actions_list[-1] == prev_actions_list[-3] and
					prev_actions_list[-2] == prev_actions_list[-4]):
					self.logger.warning("🔄 Detected alternating action pattern (A-B-A-B) - INTERVENING NOW")
					self._trigger_loop_intervention("Alternating A-B-A-B pattern detected")

				# Check for A-B-C-A-B-C pattern - IMMEDIATE intervention
				elif (len(self._previous_actions) >= 6 and
					  prev_actions_list[-3:] == prev_actions_list[-6:-3]):
					self.logger.warning("🔄 Detected repeating 3-action pattern (A-B-C-A-B-C) - INTERVENING NOW")
					self._trigger_loop_intervention("Repeating 3-action pattern detected")

		if self.register_new_step_callback:
			if callable(self.register_new_step_callback):
				self.register_new_step_callback(state, model_output, self.state.n_steps)
			else:
				self.logger.warning("register_new_step_callback is set but not callable")

		# Offload blocking file I/O off the event loop so concurrent sessions
		# aren't stalled by this session's conversation write.
		await asyncio.to_thread(self._save_conversation, input_messages, model_output)
		self.message_manager.remove_last_state_message()  # we dont want the whole state in the chat history

		self._check_if_stopped()

		# T-16: Model output was already added earlier, right after get_next_action
		# This ensures consistent flow for both native and non-native tool calls
		# tool_call_id is already set from the earlier add_model_output call

	async def _call_llm(self, input_messages):
		"""Phase 2: call the LLM and normalize its tool calls -> (model_output, tool_calls_to_pass)."""
		# P2-14: get_next_action consumes one-shot ephemerals (correspondent reply /
		# RECALL) into a pending buffer. Drop them once the LLM responds; on a transient
		# failure, re-queue them so the next step re-includes them instead of losing them.
		try:
			model_output = await self.get_next_action(input_messages)
		except Exception:
			try:
				self.message_manager.restore_ephemeral_on_failure()
			except Exception:
				pass
			raise
		try:
			self.message_manager.commit_ephemeral_consumption()
		except Exception:
			pass

		# Handle tool_call_id based on native vs non-native mode
		tool_calls_to_pass = None
		if self.message_manager.use_native_tools:
			# Get tool calls stored by get_next_action
			if hasattr(self, '_current_normalized_tool_calls') and self._current_normalized_tool_calls:
				tool_calls_to_pass = self._current_normalized_tool_calls
				self.logger.debug(f"Using {len(tool_calls_to_pass)} native tool calls from LLM response")
			else:
				self.logger.debug("Native tools enabled but no tool calls in LLM response")
		else:
			# For non-native models, create synthetic tool calls from actions
			if model_output and hasattr(model_output, 'action') and model_output.action:
				# Create synthetic tool calls for non-native models
				synthetic_calls = []
				for idx, action in enumerate(model_output.action):
					# Extract action name and params
					if hasattr(action, 'model_dump'):
						action_dict = action.model_dump(exclude_unset=True, by_alias=True)
						if action_dict:
							action_name = list(action_dict.keys())[0]
							action_params = action_dict[action_name] if isinstance(action_dict[action_name], dict) else {}
						else:
							continue
					elif isinstance(action, dict) and action:
						action_name = list(action.keys())[0]
						action_params = action[action_name] if isinstance(action[action_name], dict) else {}
					else:
						continue

					# Create synthetic tool call
					synthetic_call = {
						'id': f"synthetic_{self.state.n_steps}_{idx}",
						'name': action_name,
						'args': action_params
					}
					synthetic_calls.append(synthetic_call)

				if synthetic_calls:
					# Normalize them using ToolCallBuilder
					from agents.task.agent.message_manager.tool_call_builder import ToolCallBuilder
					tool_calls_to_pass = [
						ToolCallBuilder.normalize_and_correct(tc, provider="synthetic")
						for tc in synthetic_calls
					]

					if tool_calls_to_pass:
						self.logger.debug(f"Created {len(tool_calls_to_pass)} synthetic tool calls for non-native model")
						# Store them for tracking
						if self.tool_call_tracker:
							for call in tool_calls_to_pass:
								self.tool_call_tracker.register_call(
									call_id=call.get('id') if isinstance(call, dict) else call.id,
									tool_name=call.get('name') if isinstance(call, dict) else call.name,
									args=call.get('args', {}) if isinstance(call, dict) else getattr(call, 'args', {})
								)

			# Tool call IDs are now tracked by tool_call_tracker - no temporary storage needed

		# ✅ PHASE 2 (Nov 5, 2025): Atomic message addition AFTER execution
		# DON'T add model output yet - wait until after successful execution
		# AIMessage + ToolMessages will be added atomically via add_tool_call_pair_atomic()
		self.logger.debug(f"Prepared {len(tool_calls_to_pass) if tool_calls_to_pass else 0} tool calls for execution")
		# tool_call_id = self.message_manager.add_model_output(model_output, tool_calls=tool_calls_to_pass)  # OLD: Optimistic commit

		# Clean up temporary storage
		if hasattr(self, '_current_normalized_tool_calls'):
			delattr(self, '_current_normalized_tool_calls')

		return model_output, tool_calls_to_pass

	async def _finalize_step(self, model_output, state, result, step_info=None):
		"""Finally-phase bookkeeping: complete the tracker step, emit step telemetry, persist memory, auto-save state. No early return."""
		# Ensure tool call tracker step is completed (idempotent - safe to call multiple times)
		# Do this BEFORE telemetry to ensure cleanup happens even if telemetry fails
		# NOTE: complete_step() is already idempotent - it only acts if _current_step_calls is not empty
		# The normal flow (lines 1744, 1749) calls this after adding tool responses,
		# but we call it here too to handle error cases where normal flow didn't complete
		if self.tool_call_tracker:
			try:
				self.tool_call_tracker.complete_step()
				self.logger.debug("Tool call tracker step completed in finally block")
			except Exception as tracker_error:
				self.logger.warning(f"Error completing tool call tracker step: {tracker_error}")

		actions = await self._emit_step_telemetry(model_output, state, result, step_info)

		# Add to hierarchical memory (Phase 2)
		if model_output:
			await self._save_step_to_memory(
				step_number=self.state.n_steps,
				brain_state=model_output.current_state.model_dump(),
				actions=actions if actions else [],
				results=result if result else [],
				step_info=step_info
			)

		# PERSISTENCE: Auto-save agent state and message history periodically
		if self.state.n_steps % 5 == 0:
			try:
				from agents.task.path import pm

				# Save agent state
				state_file = pm().create_file_path(
					session_id=self.session_id,
					subdir_name="data",
					filename="agent_state.json",
					user_id=self.user_id
				)
				if self.state.save_to_file(state_file):
					self.logger.debug(f"💾 Auto-saved agent state at step {self.state.n_steps}")

				# Save message history (skip for sub-agents - they don't need persistence)
				if hasattr(self, 'message_manager') and self.message_manager and not self._is_sub_agent:
					self.message_manager.save_to_disk(
						session_id=self.session_id,
						user_id=self.user_id
					)
					self.logger.debug(f"💾 Auto-saved message history at step {self.state.n_steps}")
			except Exception as save_error:
				self.logger.error(f"Failed to auto-save state at step {self.state.n_steps}: {save_error}")

	async def _step_impl(self, step_info: Optional[AgentStepInfo] = None) -> None:
		"""Actual step implementation without timeout wrapper"""
		# Phase 1: prepare. Returns the browser_context it acquired so we don't
		# re-acquire it here (that would double-emit telemetry / context tracking).
		state, input_messages, browser_context = await self._prepare_step(step_info)

		model_output = None
		tool_call_id = None  # Initialize here for wider scope
		result: list[ActionResult] = []

		try:
			model_output, tool_calls_to_pass = await self._call_llm(input_messages)

			# Validate model output; bail out early if invalid (guidance already injected)
			if not self._validate_and_intervene(model_output):
				# CO-F3: clean up the state message added for this step, same as the
				# exception handlers below do, so a planning-turn / empty-action step
				# doesn't leak its state message into permanent history.
				self.message_manager.remove_last_state_message()
				return

			result = await self._execute_actions(model_output, tool_calls_to_pass, state, step_info, browser_context)
			result = await self._process_action_results(result, model_output, tool_calls_to_pass)
			if result is None:
				# CO-F3: corrupted tool-message pairing aborted the step; clean up the
				# state message so it doesn't leak into permanent history either.
				self.message_manager.remove_last_state_message()
				return
			await self._record_step(state, model_output, input_messages)

		except InterruptedError as e:
			# CRITICAL FIX (Dec 2025): Handle stop BEFORE generic Exception handler
			# InterruptedError is a subclass of Exception, so this MUST come first!
			interruption_reason = str(e) if str(e) else "Agent stopped"
			self.logger.info(f'⏹️  {interruption_reason} at step {self.state.n_steps}')

			# Also log state flags for debugging
			self.logger.info(f'State flags: stopped={self.state.stopped}')

			# Clean up state message
			self.message_manager.remove_last_state_message()

			# Clean up tracker
			if self.tool_call_tracker:
				self.tool_call_tracker.complete_step()
				self.logger.debug("Cleared tool call tracker after agent stopped")

			# Set graceful stop result - not an error, just user interruption
			self._last_result = [
				ActionResult(
					extracted_content='Agent stopped by user. Send a message to continue.',
					include_in_memory=False  # Don't pollute memory with stop messages
				)
			]
			return

		except Exception as e:
			# Clean up state message for any exception
			self.message_manager.remove_last_state_message()

			# CRITICAL: Detect fatal errors that should stop execution immediately
			error_str = str(e).lower()
			# HIGH-1: when billing failover is enabled, let billing/quota errors reach
			# _handle_step_error (which tries a provider swap) instead of halting here.
			from agents.task.agent.core.error_recovery import _billing_failover_enabled
			_billing_failover = _billing_failover_enabled()
			is_fatal_error = _is_fatal_step_error(error_str, _billing_failover)

			if is_fatal_error:
				# Log fatal error and mark session as failed
				self.logger.error(f"❌ FATAL ERROR - Core component failure, halting session: {e}")
				self.state.consecutive_failures = self.max_failures  # Force max failures
				self.state.stopped = True  # Mark session as stopped

				# Create error result for user visibility
				self._last_result = [ActionResult(
					error=f"FATAL ERROR: {str(e)[:400]}. The session has been halted due to a critical error. Please check your API configuration and billing status.",
					include_in_memory=True
				)]

				# Clean up tracker
				if self.tool_call_tracker:
					self.tool_call_tracker.complete_step()

				# DO NOT re-raise - just return to let run() loop detect stopped state
				# This prevents infinite retry loops
				return

			# Non-fatal error - normal error handling
			result = await self._handle_step_error(e)
			self._last_result = result
			# Clean up tracker (no messages added since step failed)
			if self.tool_call_tracker:
				self.tool_call_tracker.complete_step()
				self.logger.debug("Cleared tool call tracker after step error")

		finally:
			await self._finalize_step(model_output, state, result, step_info)

			# NEVER `return` inside this finally. A bare `return` here swallows any
			# in-flight exception propagating through it — an asyncio.CancelledError
			# from task.cancel()/the step timeout, or an exception re-raised by
			# _handle_step_error — silently defeating cancellation, the step timeout,
			# and error propagation. Use a positive guard instead of an early return.
			# (Chat-mode step summary removed — handled by telemetry instead.)
			if result and state:
				await self._make_history_item(model_output, state, result)
