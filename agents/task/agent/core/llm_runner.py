"""LLM execution / next-action / error-recovery mixin (code-motion from service.py)."""

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
from core.exceptions import InsufficientCreditsError, LLMRefusalError

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
)
# PIL Image imported locally in save_screenshot() method where needed
from pydantic import BaseModel, ConfigDict, ValidationError

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



class LLMRunnerMixin:
	"""Mixin extracted verbatim from agents/task/agent/service.py (pure code-motion)."""

	def _validate_model_output(self, model_output) -> bool:
		"""Validate that model output contains actionable steps.

		Args:
			model_output: The model's output to validate

		Returns:
			True if output is valid and actionable, False otherwise
		"""
		if not model_output:
			self.logger.warning("Model output is None")
			return False

		if not hasattr(model_output, 'action'):
			self.logger.warning("Model output missing 'action' field")
			return False

		if not model_output.action:
			# The CALLER decides what an empty list means: the first one is the
			# bounded planning turn ALLOWED_REASONING_TURNS grants, and it logs
			# the warning either way. Shouting "violates the contract" at ERROR
			# here painted two red lines on the REPL for every allowed
			# reasoning turn (2026-09-17).
			self.logger.debug("Model output has empty action list")
			return False

		# Check each action is valid
		valid_actions = []
		# FIX: Ensure action is not None before iterating
		actions_to_check = model_output.action if model_output.action is not None else []
		for i, action in enumerate(actions_to_check):
			if action is None:
				continue

			# FIX: Accept both dict and ActionModel instances
			action_name = None
			if hasattr(action, 'model_dump'):
				# It's an ActionModel - extract the action name
				action_dict = action.model_dump(exclude_unset=True, by_alias=True)
				# DEBUG: Log what we got from model_dump
				if not action_dict:
					self.logger.warning(f"Action {i}: model_dump(exclude_unset=True, by_alias=True) returned empty: {action_dict}")
					self.logger.warning(f"Action {i}: type={type(action)}, class={action.__class__.__name__}")
					# Try without by_alias to debug
					action_dict_no_alias = action.model_dump(exclude_unset=True, by_alias=False)
					self.logger.warning(f"Action {i}: model_dump WITHOUT by_alias: {action_dict_no_alias}")
				if action_dict:
					action_name = list(action_dict.keys())[0] if action_dict else None
			elif isinstance(action, dict) and action:
				# It's a dict - extract the action name
				action_name = list(action.keys())[0] if action else None

			# CRITICAL FIX: Actually make actions actionable!
			if action_name:
				# FIX: Be permissive - if the model generated it, it's probably valid
				# The controller will handle unknown actions appropriately
				valid_actions.append(action_name)
				self.logger.debug(f"Accepting action: {action_name}")

				# Optional: Log a warning if we don't recognize it
				# Use Controller's high-level API instead of directly accessing registry
				if not self.controller.has_action(action_name):
					self.logger.debug(f"Note: Action '{action_name}' not in controller registry (will try anyway)")

		# NOTE (flow-efficiency D1-b): a `done` action carries its own name into
		# `valid_actions` above, so there is no premature-done gate here. A trivial
		# "say hello and finish" task may therefore complete in a single step.
		# (A former `n_steps < 2` rejection lived here but was unreachable dead code:
		# `has_done` could never be true while `valid_actions` was empty.)

		if not valid_actions:
			self.logger.warning("No valid actionable steps found in model output")
			self.logger.debug(f"Actions were: {actions_to_check}")
			return False

		# FIX: Log accepted actions for debugging
		self.logger.info(f"Validated {len(valid_actions)} actions: {valid_actions}")
		return True

	def _ensure_action_model_current(self) -> None:
		"""Rebuild self.ActionModel/self.AgentOutput if the controller's registered
		action set has changed since they were last built.

		self.ActionModel/self.AgentOutput are built ONCE at construction
		(construction.py) and bind validation of every subsequent model_output.action
		to that fixed field set. A mid-session load_tool() call (progressive tool
		disclosure) registers new actions into the controller's live registry but
		never refreshed these cached types — Pydantic's default extra='ignore'
		then silently drops any dynamically-loaded tool's action name from
		AgentOutput validation, since the stale class has no such field. Confirmed
		live 2026-08-18: a session that ran `load_tool` for
		code_execution mid-session got a systematically empty model_dump() for
		every code_execution_run_code call for the rest of the run, misreported
		as "empty action response" and feeding the thinking-loop/failure-counter
		escalation. Cheap to check every step; only rebuilds when the action set
		actually changed.
		"""
		try:
			current_names = frozenset(self.controller.get_action_names())
		except Exception:
			return
		if current_names == getattr(self, '_action_model_names', None):
			return
		self.ActionModel = self.controller.create_action_model()
		self.AgentOutput = AgentOutput.type_with_custom_actions(self.ActionModel)
		self._action_model_names = current_names

	def _timeout_recovery_output(self, timeout: float) -> AgentOutput:
		"""The AgentOutput a step ends with after the LLM timed out twice.

		INTERACTIVE session: a ``send_message`` telling the user to retry — the
		session stays alive and the human decides. AUTONOMOUS session (cron /
		goal / planner run): NO message. There is no user to "send another
		message"; the loop itself re-asks the model on the next step, and the
		only reader of that text is the owner's Telegram via the cron delivery
		rail (prod 2026-09-21 07:05Z, 09-20: "⚠️ The AI took too long to
		respond … Please send another message" delivered as an owner DM). An
		empty action list is the same shape the last-resort branch already
		returns; the brain state names the timeout so the next step has it.
		"""
		# AgentOutput and AgentBrain already imported globally
		try:
			from agents.task.session_class import is_autonomous_session
			autonomous = bool(is_autonomous_session(getattr(self, "session_id", None)))
		except Exception:
			autonomous = False  # fail-open to the interactive shape
		if autonomous:
			self.logger.info(
				"Recovery: autonomous session — no user notice; the next step retries"
			)
			return AgentOutput(
				current_state=AgentBrain(
					page_summary="LLM timeout occurred - continuing on the next step",
					memory=(f"Step {self.state.n_steps}: LLM timed out after {timeout:.0f}s "
					        f"(twice). No message sent — this is an autonomous run; retry the "
					        f"same step with a shorter, more focused request."),
					evaluation_previous_goal="Timeout - LLM response took too long",
					next_goal="Retry the timed-out step with a smaller request",
					reasoning="Autonomous run: a timeout is retried by the loop, not reported to a user.",
				),
				action=[],
			)

		recovery_brain = AgentBrain(
			page_summary="LLM timeout occurred - will notify user and await guidance",
			memory=f"Step {self.state.n_steps}: LLM timed out after multiple retries. Will inform user and wait for guidance.",
			evaluation_previous_goal="Timeout - LLM response took too long",
			next_goal="Notify user about timeout and await guidance to continue",
			reasoning="LLM timeout occurred. Instead of ending task, notify user so they can decide how to proceed."
		)

		# Try to use send_message action to notify user (preferred over done)
		# This keeps the session alive and allows the user to retry
		recovery_actions = []
		try:
			ActionModel = self.controller.create_action_model()
			available_actions = self.controller.get_action_names()

			# Check if send_message is available (preferred - keeps session alive)
			if 'send_message' in available_actions:
				recovery_actions = [ActionModel(send_message={
					# The notice does NOT pause the session: the next step retries at once.
					# Prod 2026-10-04 17:26 the old "send another message to retry" text
					# preceded a 60 USDC send 8 s later — obeying it would have paid twice.
					"text": f"⚠️ The model was slow (timeout after {timeout:.0f}s). I'm retrying "
					        f"your last request now and will confirm when it's done — don't resend it."
				})]
				self.logger.info("Recovery: notified user via send_message (session continues)")
				# DON'T mark as done - session stays alive
			else:
				# Fallback to done if send_message not available
				recovery_actions = [ActionModel(done={
					"text": f"LLM timeout at step {self.state.n_steps} after {timeout:.0f}s. "
					        f"Please start a new request - complex tasks may need to be broken down."
				})]
				self.logger.info("Recovery: created done action after timeout (send_message not available)")

		except Exception as e:
			self.logger.error(f"Could not create recovery action: {e}")
			# Last resort: empty but with proper brain state
			recovery_actions = []

		return AgentOutput(
			current_state=recovery_brain,
			action=recovery_actions
		)

	async def _fail_over_or_raise(self, llm_error: LLMError, input_messages: list[BaseMessage],
	                              timeout: float) -> AgentOutput:
		"""The agent's half of the failure policy. A transient error raises: the step handler
		retries the same model with backoff, then walks the ladder. An error the same model
		cannot outlast (auth, permanent, credit) walks the ladder now. A refusal or a content
		filter never walks it: another provider must not answer what this model declined."""
		from core.exceptions import LLMRefusalError
		if isinstance(llm_error, LLMRefusalError):
			raise llm_error
		error_type = type(llm_error).__name__
		current_provider = self._current_llm_provider(self.model_name)
		self.logger.warning(
			f"🔄 LLM provider error ({error_type}) from {current_provider}/{self.model_name}: "
			f"{str(llm_error)[:200]}"
		)
		self._emit_provider_failure_telemetry(
			failed_provider=current_provider,
			failed_model=self.model_name,
			error_type=error_type,
			error_message=str(llm_error)[:500],
			attempt_number=1
		)
		from core.error_classifier import credit_death_in_chain
		credit_death = credit_death_in_chain(llm_error) is not None
		if not (credit_death or isinstance(llm_error, (LLMPermanentError, LLMAuthenticationError))):
			raise llm_error

		# A credit-dead provider that recovers through this fallback never reaches the
		# step handler's trip site, so every later call would try the dead provider first.
		await self._trip_sentinel_if_credit_death(llm_error)

		fallback_llm = await self._get_fallback_llm(
			exclude_providers=[current_provider] + list(self.state.llm_providers_failed),
			original_model=self.model_name
		)
		if not fallback_llm:
			self.logger.warning(f"No fallback provider may serve after {error_type} from {current_provider}")
			raise llm_error

		original_model = self.model_name
		fallback_provider = await self.adopt_fallback_llm(fallback_llm, error_type)
		fallback_model = self.model_name
		fallback_start = time.time()
		try:
			result = await asyncio.wait_for(
				self._get_next_action_internal(input_messages),
				timeout=timeout
			)
		except Exception as fallback_error:
			self.logger.error(
				f"❌ Fallback also failed: {type(fallback_error).__name__}: {str(fallback_error)[:200]}"
			)
			await self.restore_first_llm()
			self.state.track_llm_error(type(fallback_error).__name__, fallback_provider)
			# The first error decides: the step handler halts on a permanent one.
			raise llm_error from None

		self.state.reset_llm_errors()
		self._emit_fallback_success_telemetry(
			original_provider=current_provider,
			original_model=original_model,
			fallback_provider=fallback_provider,
			fallback_model=fallback_model,
			original_error_type=error_type,
			total_attempts=2,
			total_time=time.time() - fallback_start
		)
		self.logger.info(f"🎉 Fallback successful: {original_model} → {fallback_model}")
		return result

	@staticmethod
	def _text_only(msg):
		"""A human row's images as a text marker, for a model that cannot see them."""
		if not (isinstance(msg, HumanMessage) and isinstance(msg.content, list)):
			return msg
		if not any(isinstance(p, dict) and p.get('type') == 'image_url' for p in msg.content):
			return msg
		parts = []
		for part in msg.content:
			if isinstance(part, dict) and part.get('type') == 'text':
				parts.append(part.get('text', ''))
			elif isinstance(part, dict) and part.get('type') == 'image_url':
				parts.append('[Screenshot available - vision disabled]')
			elif isinstance(part, str):
				parts.append(part)
		return HumanMessage(content='\n'.join(parts))

	def _call_messages(self, messages, extra_messages=None):
		"""The rows ONE call sends: the manager's rows with a non-vision model's images as
		text, then this call's own rows (the truncation note), which nothing re-queues."""
		model = getattr(self.llm, 'model_name', None) or getattr(self, 'model_name', None)
		try:
			blind = isinstance(model, str) and bool(model) and not self._check_vision_support(model)
		except Exception:
			blind = False
		rows = [self._text_only(m) for m in messages] if blind else list(messages)
		return rows + list(extra_messages or [])

	@time_execution_async('--get_next_action')
	async def get_next_action(self, input_messages: list[BaseMessage]) -> AgentOutput:
		"""Get the next action, inside the owner's reasoning scope (094): a session
		pinned with ``/model … reasoning=<level>`` sends that level on its MAIN calls
		only — compaction, reflection and the judge keep the operator env."""
		from agents.task.model_pin_state import session_reasoning as _level_for
		from modules.llm.call_scope import session_reasoning
		with session_reasoning(_level_for(getattr(self, "session_id", None))):
			return await self._get_next_action_unscoped(input_messages)

	async def _get_next_action_unscoped(self, input_messages: list[BaseMessage]) -> AgentOutput:
		"""Get next action from the model with enhanced retry logic and token safety.

		This method uses a language model to determine the next action based on the current state.
		"""
		# 2026-08-18: refresh stale ActionModel/AgentOutput bindings before every
		# LLM call — see _ensure_action_model_current's docstring.
		self._ensure_action_model_current()

		# Check cancellation before calling LLM
		if self._cancelled:
			self.logger.warning(f"❌ LLM call cancelled before execution")
			raise asyncio.CancelledError("Agent cancelled")

		await self.return_to_first_llm_if_due()

		has_multimodal_content = any(
			isinstance(msg, HumanMessage) and isinstance(msg.content, list)
			for msg in input_messages
		)

		if has_multimodal_content:
			# Count images across all messages
			image_count = 0
			multimodal_positions = []
			for idx, msg in enumerate(input_messages):
				if isinstance(msg, HumanMessage) and isinstance(msg.content, list):
					images_in_msg = sum(
						1 for part in msg.content
						if isinstance(part, dict) and part.get('type') == 'image_url'
					)
					if images_in_msg > 0:
						image_count += images_in_msg
						multimodal_positions.append(idx)

			# Get model name for logging
			model_name = getattr(self.llm, 'model_name', 'unknown')

			# Check if model supports vision
			supports_vision = self._check_vision_support(model_name)

			if not supports_vision:
				# UPDATED (Dec 2025): Strip images from messages instead of halting
				# This allows non-vision models to continue with text-only content
				# The strip itself is _call_messages, on the request each call really sends.
				self.logger.warning(
					f"⚠️ VISION DISABLED: Model {model_name} does NOT support vision. "
					f"Stripping {image_count} image(s) from messages."
				)

				# Also disable vision for this session to prevent future screenshots
				self.use_vision = False
				image_count = 0  # Reset since we stripped them
			else:
				self.logger.info(
					f"📷 VISION ENABLED: Sending {image_count} image(s) in {len(multimodal_positions)} message(s) "
					f"to vision-capable model '{model_name}' (positions: {multimodal_positions})"
				)

			# Store for post-response verification
			self._expected_image_count = image_count
		else:
			self._expected_image_count = 0
			self.logger.debug("No multimodal content in this LLM call")

		# ============================================================================
		# END VISION VERIFICATION
		# Calculate dynamic timeout using MessageManager
		tool_count = len(self.controller.get_action_names()) if self.controller else 0
		tool_count = len(self.controller.get_action_names()) if self.controller else 0
		timeout = self.message_manager.calculate_llm_timeout(
			tool_count=tool_count,
			use_vision=self.use_vision
		)
		
		# 057 WS-B: this task's OUTPUT budget. Set every step for every session class,
		# so a task spawned from a cron run does not keep that run's cap. A cap only
		# when AUTONOMOUS_MAX_OUTPUT_TOKENS is set AND this is a cron/goal session.
		from agents.task.session_class import apply_session_output_budget
		apply_session_output_budget(self.session_id)

		# CRITICAL FIX: Add timeout wrapper to prevent infinite hangs
		# Typed LLM errors go through the failure policy (_fail_over_or_raise)
		try:
			result = await asyncio.wait_for(
				self._get_next_action_internal(input_messages),
				timeout=timeout
			)
			# 057 WS-B: the timeout ladder measures a STREAK — a completed call
			# resets it.
			from agents.task.agent.core.timeout_reroute import note_success
			note_success(self)
			# 057 WS-B: a cut-off answer is a FACT (finish_reason=length), not
			# malformed JSON — re-run the step once with the reason named,
			# instead of repairing a truncated answer by guessing.
			from agents.task.agent.core.output_truncation import handle_truncated_output
			return await handle_truncated_output(self, input_messages, timeout, result)
		
		except LLMContextLengthError as context_error:
			# A smaller prompt helps, a different provider does not: _handle_step_error prunes.
			self.logger.error(f"Context length exceeded: {context_error}")
			raise

		except LLMError as llm_error:
			return await self._fail_over_or_raise(llm_error, input_messages, timeout)

		except asyncio.TimeoutError:
			self.logger.error(f"LLM call timed out after {timeout:.0f} seconds - implementing recovery strategy", exc_info=True)

			# 057 WS-B: consecutive timeouts are a ROUTING fact. A timeout never
			# triggered provider fallback, so a stalled upstream was retried on
			# the SAME upstream. 2 in a row => order this session's upstreams by
			# latency; 3 in a row => stop retrying and end the step honestly (the
			# worst retrying step is timeout + 180 s ≈ 570 s against a 600 s cron
			# cap, which is how a cap gets hit). Inert unless LLM_TIMEOUT_REROUTE.
			from agents.task.agent.core.timeout_reroute import (
				maybe_reroute, note_timeout, should_stop_retrying)
			_timeout_streak = note_timeout(self)
			maybe_reroute(self, _timeout_streak)

			# Clear any pending tool calls from tracker
			if hasattr(self, 'tool_call_tracker') and self.tool_call_tracker:
				self.tool_call_tracker.complete_step()
				self.logger.debug("Cleared tool call tracker after timeout")

			# One retry with the SAME context: the request is rebuilt from the message
			# manager, so a "reduced context" here would be a claim, not a request.
			if should_stop_retrying(_timeout_streak):
				self.logger.error(
					"%d consecutive LLM timeouts — ending this step with no further "
					"retry. A third timeout in a row is a statement about the route, "
					"not about this prompt.", _timeout_streak)
			else:
				# 75% of the budget, capped at 180 s: the worst step is timeout + 180 s,
				# under the 600 s cron cap.
				retry_timeout = min(180.0, timeout * 0.75)
				self.logger.info(f"Retrying the call once with the same context ({retry_timeout:.0f}s)")
				try:
					return await asyncio.wait_for(
						self._get_next_action_internal(input_messages), timeout=retry_timeout)
				except asyncio.TimeoutError:
					self.logger.warning("The retry after a timeout also timed out")
				except LLMRefusalError:
					raise  # a refusal is not a slow model: it halts and names its category
				except Exception as e:
					self.logger.warning(f"The retry after a timeout failed: {e}")

			# No model call answered the one-shot messages of this step (a correspondent
			# reply, a recall note): queue them again, or the recovery step commits them.
			try:
				self.message_manager.restore_ephemeral_on_failure()
			except Exception:
				self.logger.debug("could not re-queue the one-shot messages", exc_info=True)
			# IMPROVED RECOVERY: continue instead of ending the task. The shape
			# depends on WHO is on the other end — see _timeout_recovery_output.
			return self._timeout_recovery_output(timeout)

