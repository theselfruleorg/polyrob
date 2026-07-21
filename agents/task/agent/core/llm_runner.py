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
			self.logger.error("Model output has empty action list - this violates the agent contract")
			self.logger.error("The LLM must ALWAYS provide at least one action. Empty actions are never valid.")
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

	@time_execution_async('--get_next_action')
	async def get_next_action(self, input_messages: list[BaseMessage]) -> AgentOutput:
		"""Get next action from the model with enhanced retry logic and token safety.

		This method uses a language model to determine the next action based on the current state.
		"""
		# Check cancellation before calling LLM
		if self._cancelled:
			self.logger.warning(f"❌ LLM call cancelled before execution")
			raise asyncio.CancelledError("Agent cancelled")

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
				self.logger.warning(
					f"⚠️ VISION DISABLED: Model {model_name} does NOT support vision. "
					f"Stripping {image_count} image(s) from messages."
				)

				# Strip images from messages - replace multimodal content with text only
				for idx in multimodal_positions:
					msg = input_messages[idx]
					if isinstance(msg.content, list):
						# Extract text parts only
						text_parts = []
						for part in msg.content:
							if isinstance(part, dict):
								if part.get('type') == 'text':
									text_parts.append(part.get('text', ''))
								elif part.get('type') == 'image_url':
									text_parts.append('[Screenshot available - vision disabled]')
							elif isinstance(part, str):
								text_parts.append(part)
						# Replace message content with text-only version
						input_messages[idx] = HumanMessage(content='\n'.join(text_parts))
						self.logger.debug(f"Stripped images from message at position {idx}")

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
		
		# CRITICAL FIX: Add timeout wrapper to prevent infinite hangs
		# Also handle LLM-specific errors with automatic provider fallback
		try:
			result = await asyncio.wait_for(
				self._get_next_action_internal(input_messages),
				timeout=timeout
			)
			return result
		
		except (LLMRateLimitError, LLMAuthenticationError, LLMConnectionError) as llm_error:
			error_type = type(llm_error).__name__
			current_provider = self._get_provider_from_model(self.model_name)
			
			self.logger.warning(
				f"🔄 LLM provider error ({error_type}) from {current_provider}/{self.model_name}: "
				f"{str(llm_error)[:200]}"
			)
			
			# Record the failure
			self._emit_provider_failure_telemetry(
				failed_provider=current_provider,
				failed_model=self.model_name,
				error_type=error_type,
				error_message=str(llm_error)[:500],
				attempt_number=1
			)
			
			# Try to get a fallback LLM. P1 finalization: exclude the providers that
			# already FAILED this run too (not just the current one) — the sibling
			# generic-LLMError branch below does this, and without it this path could
			# fall back straight to a provider we already know is dead.
			self.logger.info(f"Attempting provider fallback after {error_type}...")
			fallback_llm = await self._get_fallback_llm(
				exclude_providers=[current_provider] + self.state.llm_providers_failed,
				original_model=self.model_name
			)
			
			if fallback_llm:
				# Store original LLM for potential restoration
				original_llm = self.llm
				original_model_name = self.model_name
				
				try:
					# Temporarily switch to fallback
					self.llm = fallback_llm
					fallback_model = getattr(fallback_llm, 'model_name', 'fallback')
					self.model_name = fallback_model
					
					self.logger.info(f"✅ Switched to fallback provider: {fallback_model}")
					
					# Retry with fallback LLM
					fallback_start = time.time()
					result = await asyncio.wait_for(
						self._get_next_action_internal(input_messages),
						timeout=timeout
					)
					
					fallback_duration = time.time() - fallback_start
					
					# Record successful fallback
					fallback_provider = self._get_provider_from_model(fallback_model)
					self._emit_fallback_success_telemetry(
						original_provider=current_provider,
						original_model=original_model_name,
						fallback_provider=fallback_provider,
						fallback_model=fallback_model,
						original_error_type=error_type,
						total_attempts=2,
						total_time=fallback_duration
					)
					
					self.logger.info(
						f"🎉 Fallback successful: {original_model_name} → {fallback_model} "
						f"(took {fallback_duration:.1f}s)"
					)
					
					return result
					
				except Exception as fallback_error:
					# Fallback also failed - restore original and raise typed error
					self.llm = original_llm
					self.model_name = original_model_name
					
					self.logger.error(
						f"❌ Fallback also failed: {type(fallback_error).__name__}: {str(fallback_error)[:200]}"
					)
					
					# Track both failed providers
					fallback_provider = self._get_provider_from_model(fallback_model)
					self.state.track_llm_error(type(fallback_error).__name__, fallback_provider)
					
					# Raise typed exception for proper handling
					raise LLMProviderExhaustedError(
						f"Primary LLM ({original_model_name}) failed with {error_type}: {str(llm_error)[:200]}. "
						f"Fallback also failed: {str(fallback_error)[:200]}",
						providers_tried=list(self.state.llm_providers_failed)
					)
			else:
				# No fallback available - raise typed exception
				self.logger.error(
					f"❌ No fallback provider available after {error_type} from {current_provider}"
				)
				raise LLMProviderExhaustedError(
					f"No fallback available after {error_type} from {current_provider}: "
					f"{str(llm_error)[:200]}",
					providers_tried=[current_provider]
				) from llm_error
		
		except LLMContextLengthError as context_error:
			# Context too long - this is handled separately (truncation, not provider fallback)
			self.logger.error(f"Context length exceeded: {context_error}")
			# Let existing timeout/recovery logic handle this if possible
			raise

		except LLMError as generic_llm_error:
			# UPGRADED (Dec 2025): Generic LLM error - also try fallback
			error_type = type(generic_llm_error).__name__
			current_provider = self._get_provider_from_model(self.model_name)

			self.logger.warning(f"🔄 Generic LLM error ({error_type}) - attempting fallback...")

			# Track error
			self.state.track_llm_error(error_type, current_provider)

			# Try fallback
			fallback_llm = await self._get_fallback_llm(
				exclude_providers=[current_provider] + self.state.llm_providers_failed,
				original_model=self.model_name
			)

			if fallback_llm:
				original_llm = self.llm
				original_model = self.model_name

				try:
					self.llm = fallback_llm
					self.model_name = getattr(fallback_llm, 'model_name', 'fallback')
					self.logger.info(f"✅ Switched to fallback: {self.model_name}")

					result = await asyncio.wait_for(
						self._get_next_action_internal(input_messages),
						timeout=timeout
					)

					self.state.reset_llm_errors()
					return result

				except Exception as fallback_error:
					self.llm = original_llm
					self.model_name = original_model
					self.logger.error(f"❌ Fallback also failed: {fallback_error}")
					# Track the failed fallback provider
					fallback_provider = self._get_provider_from_model(getattr(fallback_llm, 'model_name', 'unknown'))
					self.state.track_llm_error(type(fallback_error).__name__, fallback_provider)
					raise LLMProviderExhaustedError(
						f"Primary LLM failed: {generic_llm_error}. Fallback failed: {fallback_error}",
						providers_tried=list(self.state.llm_providers_failed)
					)
			else:
				self.logger.error(f"❌ No fallback available for {error_type}")
				raise LLMProviderExhaustedError(
					f"No fallback available after {error_type}: "
					f"{str(generic_llm_error)[:200]}",
					providers_tried=list(self.state.llm_providers_failed)
				) from generic_llm_error
		
		except asyncio.TimeoutError:
			# Clear LLM call tracking on timeout
			self._llm_call_in_progress = False
			self._llm_call_start_time = None
			
			self.logger.error(f"LLM call timed out after {timeout:.0f} seconds - implementing recovery strategy", exc_info=True)

			# Clear any pending tool calls from tracker
			if hasattr(self, 'tool_call_tracker') and self.tool_call_tracker:
				self.tool_call_tracker.complete_step()
				self.logger.debug("Cleared tool call tracker after timeout")

			# Try to reduce context size and retry once
			if hasattr(self, 'message_manager') and len(input_messages) > 5:
				self.logger.info("Attempting recovery with reduced context")
				try:
					# Keep system message and most recent tool call pairs to maintain context
					reduced_messages = []

					# Always keep system message
					for msg in input_messages:
						if isinstance(msg, SystemMessage):
							reduced_messages.append(msg)
							break

					# Keep last 2 complete tool call pairs if they exist
					tool_pairs = []
					for i in range(len(input_messages) - 1, -1, -1):
						if isinstance(input_messages[i], ToolMessage) and i > 0:
							if isinstance(input_messages[i-1], AIMessage):
								tool_pairs.insert(0, (input_messages[i-1], input_messages[i]))
								if len(tool_pairs) >= 2:
									break

					# Add the tool pairs
					for ai_msg, tool_msg in tool_pairs:
						reduced_messages.append(ai_msg)
						reduced_messages.append(tool_msg)

					# Add last user message if not already included
					for msg in reversed(input_messages):
						if hasattr(msg, 'type') and msg.type == 'human':
							if msg not in reduced_messages:
								reduced_messages.append(msg)
							break

					# Ensure we have at least some messages
					if len(reduced_messages) < 3:
						reduced_messages = input_messages[-5:]  # Fallback to last 5 messages

					reduced_timeout = min(180.0, timeout * 0.75)  # Give recovery 75% of original timeout (min 180s)
					
					result = await asyncio.wait_for(
						self._get_next_action_internal(reduced_messages),
						timeout=reduced_timeout
					)
					self.logger.info("Recovery successful with reduced context")
					return result
				except asyncio.TimeoutError:
					self.logger.warning("Recovery attempt also timed out")
				except Exception as e:
					self.logger.warning(f"Recovery attempt failed: {e}")

			# IMPROVED RECOVERY: Try to continue instead of ending task
			# AgentOutput and AgentBrain already imported globally

			# Create safe fallback brain state
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
						"text": f"⚠️ The AI took too long to respond (timeout after {timeout:.0f}s). "
						        f"This can happen with complex tasks. Please send another message to retry or simplify your request."
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

