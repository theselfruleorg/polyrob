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
    MAX_MCP_PER_STEP,
    format_nag_exempt,
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

# G-1: rate limiter for the swallowed usage_tracker.record_llm_usage failure log
from agents.task.agent.core.metering_failure_log import metering_failure_limiter

# G-26 reachability fix (Task 5c): STABLE per-completion idempotency key for
# usage_tracker.record_llm_usage's request_id dedup.
from agents.task.agent.core.aux_metering import extract_stable_request_id





class NextActionInternalMixin:
	"""Core LLM-invocation method (_get_next_action_internal) split whole out of
	LLMRunnerMixin so llm_runner.py drops under 700L (P9). Agent composes it;
	get_next_action calls it via MRO. Imports above are llm_runner's full set,
	replicated so every name the method references resolves (post-MAX_MCP defense)."""

	async def _stream_plain_fallback(self, current_messages, timeout_seconds) -> AIMessage:
		"""Stream a plain (non-structured) LLM fallback call, accumulating content and
		usage metadata into one AIMessage. Shared by both structured-output fallback
		branches (H2: the two inline copies had drifted — one referenced an undefined
		``usage_metadata`` and raised NameError every time that branch streamed)."""
		full_content = ""
		fallback_usage_metadata = None
		# Fix pass 2 (money-correctness): astream() yields the single already-
		# stamped AIMessage from adapters.py._agenerate as its one chunk;
		# propagate its per-call `_polyrob_provider_response_id` onto the
		# rebuilt response so this fallback's billing gets the same stable,
		# race-proof dedup key as the primary path instead of falling back to
		# the shared, concurrency-racy `<client>.last_response` read.
		stamped_provider_response_id = None

		async def _run():
			nonlocal full_content, fallback_usage_metadata, stamped_provider_response_id
			async for chunk in self.llm.astream(current_messages):
				if hasattr(chunk, 'content') and chunk.content:
					full_content += chunk.content
					await self.hitl_manager.stream_output(chunk.content)
				if hasattr(chunk, 'usage_metadata') and chunk.usage_metadata:
					fallback_usage_metadata = chunk.usage_metadata
				chunk_stamped_id = getattr(chunk, '_polyrob_provider_response_id', None)
				if chunk_stamped_id:
					stamped_provider_response_id = chunk_stamped_id

		await asyncio.wait_for(_run(), timeout=timeout_seconds)
		result = AIMessage(content=full_content, usage_metadata=fallback_usage_metadata)
		if stamped_provider_response_id:
			result._polyrob_provider_response_id = stamped_provider_response_id
		return result

	async def _bill_llm_response(self, response, llm_duration, provider=None,
								 purpose="next_action"):
		"""Bill exactly one finalized LLM response through the single billing path.

		G1/G2 (telemetry audit 2026-07-04): billing used to be inline in the
		native-tool branch only, so the structured-output and plain fallback
		completions consumed tokens with NO billing record. This helper is called
		from EVERY response-producing path. Idempotent per response object (marks
		``_polyrob_billed``) so the same object is never charged twice; distinct
		responses (a real 2nd API call) are each billed. Returns the token_usage dict.
		"""
		if response is None:
			return None
		if getattr(response, "_polyrob_billed", False):
			return None
		try:
			setattr(response, "_polyrob_billed", True)
		except Exception:
			pass

		provider = provider or getattr(self, 'llm_provider', 'unknown')
		token_usage = extract_token_usage(response, provider)
		input_tokens = token_usage.get('prompt_tokens', 0) or 0
		output_tokens = token_usage.get('completion_tokens', 0) or 0
		cached_tokens = token_usage.get('cached_tokens', 0) or 0
		cache_creation_tokens = token_usage.get('cache_creation_tokens', 0) or 0
		# G-26 reachability fix: extract the provider's own response id (when
		# available) as the STABLE dedup key for this completion. Must run
		# synchronously here, with no await between the LLM call that produced
		# `response` and this point, so a concurrent call on a shared client
		# can't overwrite the source before we read it.
		request_id = extract_stable_request_id(getattr(self, 'llm', None), response, provider)

		if self.usage_tracker and self.user_id:
			try:
				usage_record = await self.usage_tracker.record_llm_usage(
					user_id=self.user_id,
					session_id=self.session_id,
					agent_id=self.agent_id,
					model=self.model_name,
					provider=provider,
					input_tokens=input_tokens,
					output_tokens=output_tokens,
					cached_tokens=cached_tokens,
					cache_creation_tokens=cache_creation_tokens,
					duration_seconds=llm_duration,
					component="agent",
					purpose=purpose,
					success=True,
					request_id=request_id
				)
				self.logger.info(
					f"✓ Step {getattr(getattr(self, 'state', None), 'n_steps', '?')}: "
					f"{input_tokens:,} in + {output_tokens:,} out = "
					f"{input_tokens + output_tokens:,} tokens | "
					f"API: ${usage_record.costs.api_cost_usd:.6f} → "
					f"User: ${usage_record.costs.user_cost_usd:.6f} "
					f"({usage_record.costs.credits_charged} credits)"
				)
			except InsufficientCreditsError:
				self.logger.warning("⚠️ Session suspended due to insufficient credits")
				raise
			except Exception as e:
				# G-1: full traceback the first time per process, then rate-limited
				# (was: a full traceback on EVERY LLM call, flooding the journal
				# when metering was FK-blocked by an unseeded user_profiles row).
				metering_failure_limiter.record(self.logger, e)
		elif self.user_id:
			# Fallback: telemetry only, NO billing (usage_tracker should always exist
			# in production). G5: made visible so JSON/DB divergence is diagnosable.
			self.logger.warning(
				"usage_tracker not available - telemetry only, NO BILLING! "
				"Check orchestrator initialization."
			)
			if getattr(self, 'telemetry_manager', None):
				try:
					self.telemetry_manager.capture_llm_usage(
						component="agent",
						purpose=purpose,
						model_name=self.model_name,
						duration_seconds=llm_duration,
						success=True,
						prompt_tokens=input_tokens,
						completion_tokens=output_tokens,
						cached_tokens=cached_tokens,
						agent_id=self.agent_id
					)
				except Exception as e:
					self.logger.debug(f"Failed to capture LLM telemetry: {e}")
		return token_usage

	@staticmethod
	def _classify_llm_error(e: Exception) -> str:
		"""Classify an LLM-call exception into a retry bucket (P7 finalization:
		extracted from _get_next_action_internal's except handler). Pure — decides
		parse / rate_limit / parameter_error / other from the message text."""
		s = str(e).lower()
		if "parse" in s or "json" in s or "validation" in s:
			return "parse"
		if "rate" in s or "limit" in s:
			return "rate_limit"
		if "llm_client" in s:
			return "parameter_error"
		return "other"

	def _verify_vision_post_response(self, response) -> None:
		"""Post-response vision diagnostics (P7 finalization: extracted verbatim from
		_get_next_action_internal). Pure logging + reset of the expected-image counter —
		reads `response` and `self._expected_image_count`, mutates only that counter, and
		is a no-op when no images were expected. No load-bearing local leaks out."""
		if hasattr(self, '_expected_image_count') and self._expected_image_count > 0:
			# Extract response text
			response_text = ""
			if hasattr(response, 'content'):
				response_text = str(response.content)
			elif isinstance(response, dict) and 'parsed' in response:
				parsed_obj = response['parsed']
				if hasattr(parsed_obj, 'current_state'):
					brain_state = parsed_obj.current_state
					# Concatenate all brain state fields
					response_text = " ".join([
						str(getattr(brain_state, field, ""))
						for field in ['page_summary', 'memory', 'evaluation_previous_goal', 'next_goal', 'reasoning']
						if hasattr(brain_state, field)
					])
			else:
				response_text = str(response)

			# ENHANCED VISION VERIFICATION: Three-layer check
			# Layer 0: NEGATIVE patterns (indicates failure - check first!)
			negative_keywords = [
				'cannot view', 'cannot analyze', 'cannot see', 'cannot directly',
				'unfortunately i cannot', 'unable to view', 'unable to analyze', 'not able to',
				'do not have access', 'cannot access', 'don\'t have access',
				'need a url', 'need to navigate', 'current interface',
				'through my interface', 'via browser', 'hosted online',
				'would need', 'could you please', 'share a web link',
				'provide a url', 'upload to', 'host the image'
			]
			has_negative = any(keyword in response_text.lower() for keyword in negative_keywords)

			# Layer 1: Basic vision keywords (mentions images)
			vision_mention_keywords = [
				'image', 'picture', 'screenshot', 'visual', 'photo',
				'see', 'shown', 'displayed', 'appears', 'looks like',
				'in the image', 'from the screenshot', 'attached image'
			]
			mentions_vision = any(keyword in response_text.lower() for keyword in vision_mention_keywords)

			# Layer 2: Actual visual description keywords (describes content)
			visual_description_keywords = [
				'shows', 'depicts', 'contains', 'features', 'displays',
				'color', 'colours', 'shape', 'button', 'field', 'layout', 'text',
				'positioned', 'aligned', 'centered', 'located', 'placed',
				'red', 'blue', 'green', 'white', 'black', 'background', 'foreground',
				'top', 'bottom', 'left', 'right', 'corner', 'center',
				'label', 'heading', 'icon', 'menu', 'section', 'panel'
			]
			has_description = any(keyword in response_text.lower() for keyword in visual_description_keywords)

			# Check negative patterns FIRST (overrides everything else)
			if has_negative:
				matched_negatives = [kw for kw in negative_keywords if kw in response_text.lower()]
				self.logger.error(
					f"❌ VISION FAILURE (NEGATIVE PATTERN): LLM explicitly states inability to view images. "
					f"Matched patterns: {matched_negatives[:3]}\n"
					f"Root causes:\n"
					f"   1. Images did NOT reach the LLM (most likely - check upload timing)\n"
					f"   2. Message format error (multimodal content malformed)\n"
					f"   3. Session auto-started before files uploaded (WEBVIEW_AUTO_START_BUG)\n"
					f"   Response preview: {response_text[:200]}..."
				)
			elif mentions_vision and has_description:
				self.logger.info(
					f"✅ VISION SUCCESS: LLM response contains actual visual analysis. "
					f"Images ({self._expected_image_count}) were processed correctly."
				)
			elif mentions_vision and not has_description:
				self.logger.warning(
					f"⚠️ VISION PARTIAL: Response mentions images but lacks detailed visual description. "
					f"Possible issues:\n"
					f"   1. LLM received images but chose not to describe them\n"
					f"   2. Images may be in wrong position relative to text (MESSAGE_ORDER_BUG)\n"
					f"   3. LLM confused about image context/workflow\n"
					f"   Response preview: {response_text[:200]}..."
				)
			elif not mentions_vision and has_description:
				self.logger.info(
					f"✅ VISION SUCCESS: Response contains visual description despite no explicit mention. "
					f"Images ({self._expected_image_count}) likely processed."
				)
			else:
				# FIXED: Don't log ERROR for non-visual tasks
				# The LLM may correctly ignore screenshots when executing filesystem/tool tasks
				# Only log at DEBUG level since this is expected behavior for non-visual tasks
				self.logger.debug(
					f"📷 VISION SKIPPED: LLM response has no visual content ({self._expected_image_count} image(s) sent). "
					f"This is normal for non-visual tasks (filesystem ops, tool calls). "
					f"Response preview: {response_text[:100] if response_text else '(empty)'}..."
				)

			# Clear the counter
			self._expected_image_count = 0

	async def _get_next_action_internal(self, input_messages: list[BaseMessage]) -> AgentOutput:
		"""Internal implementation of get_next_action."""
		from agents.task.robust_parse_config import RobustParseConfig
		import asyncio
		import time

		# CRITICAL FIX: Clear tool call tracker at start of each get_next_action call
		# This prevents accumulation of IDs across multiple calls within the same step
		if self.tool_call_tracker and self.tool_call_tracker.has_active_calls():
			active_calls = self.tool_call_tracker.get_current_step_calls()
			if active_calls:
				self.logger.debug(f"Clearing {len(active_calls)} stale tool calls from previous get_next_action call")
			self.tool_call_tracker.complete_step()

		# Clear any previous normalized tool calls (but use tracker for IDs)
		if hasattr(self, '_current_normalized_tool_calls'):
			delattr(self, '_current_normalized_tool_calls')

		# Cut messages before LLM call only if we're really close to the limit
		estimated_usage = self.message_manager.get_estimated_context_usage()
		if estimated_usage > 0.92:  # Warn at 92% context usage
			self.logger.warning(f"High context usage: {estimated_usage:.1%} - consider checkpointing")
		
		# Provider-aware format hint injection
		# First determine if we'll be using native tools
		provider = detect_llm_provider(None, self.model_name)
		will_use_native_tools = (
			self.use_native_tools and  # User preference
			self.controller and
			# Use Controller's high-level API instead of directly accessing registry
			self.controller.supports_native_tools(provider)  # Provider capability
		)

		# Use ephemeral message API for format hints (P0 fix from upgrade_tasks.md)
		format_hint_added = False
		if RobustParseConfig.INJECT_FORMAT_HINT_EARLY and not will_use_native_tools:
			# Only inject JSON format hint when NOT using native tools
			formatting_message = HumanMessage(
				content=f"""CRITICAL RESPONSE FORMAT:
Your response MUST be valid JSON with this exact structure:
{{
  "current_state": {{
    "page_summary": "Brief page summary",
    "evaluation_previous_goal": "Success|Failed|Unknown",
    "memory": "Key info to remember",
    "next_goal": "What to do next",
    "reasoning": "Why this action makes sense"
  }},
  "action": [{{"action_name": {{"param": "value"}}}}]
}}

CRITICAL: For "done" action use {{"done": {{"text": "message"}}}} - NOT "message" field!
Use double quotes only. Max {self.max_actions_per_step} actions."""
			)

			# Check if hint would fit within token limits using safety check
			safety = self.message_manager.check_token_safety([formatting_message])
			if safety['safe']:
				# Use the new ephemeral message API instead of local append
				self.message_manager.push_ephemeral_message(formatting_message)
				format_hint_added = True
				self.logger.debug("Pushed early format hint for JSON response mode")
			else:
				self.logger.warning(f"Format hint too large, would exceed safe token limit")
		elif will_use_native_tools and RobustParseConfig.INJECT_FORMAT_HINT_EARLY \
				and not format_nag_exempt(self.model_name, provider):
			# For native tools, remind about JSON brain state format — but only for
			# families that need it (T1-10): the prompt is authored for Claude and the
			# family-note design deliberately gives it none, so the per-step nag was
			# one wasted uncached message per step there.
			native_tools_hint = HumanMessage(
				content="""REMINDER: Your text content must be valid JSON with brain state fields:
{"memory": "...", "evaluation_previous_goal": "...", "next_goal": "...", "reasoning": "..."}
Then emit your function calls."""
			)
			# Use the new ephemeral message API
			self.message_manager.push_ephemeral_message(native_tools_hint)
			format_hint_added = True
			self.logger.debug("Format hint prepared for injection")

		# CRITICAL FIX: Don't consume ephemeral message yet - get regular messages first
		# This prevents losing the format hint during token management
		converted_input_messages = self.message_manager.get_messages()  # NOT get_messages_for_llm() yet!

		# Special handling for deepseek-reasoner after conversion
		if self.model_name == 'deepseek-reasoner':
			# DeepSeek needs additional message merging after conversion
			converted_input_messages = self.message_manager.merge_successive_messages(converted_input_messages, HumanMessage)
			converted_input_messages = self.message_manager.merge_successive_messages(converted_input_messages, AIMessage)

		# Check token safety using MessageManager's API (SINGLE SOURCE OF TRUTH)
		safety_check = self.message_manager.check_token_safety(raise_on_overflow=True)
		self.logger.debug(
			f"Token usage: {safety_check['usage_percent']:.1f}% "
			f"({safety_check['current_tokens']}/{safety_check['max_limit']} tokens)"
		)

		# NOW get final messages with ephemeral message included (one-shot consumption)
		converted_input_messages = self.message_manager.get_messages_for_llm()
		
		# Prepare request info for session data
		request_info = {
			"model": self.model_name,
			"model_library": self.chat_model_library,
			"parameters": self._get_llm_parameters(),
			"estimated_input_tokens": self.message_manager.get_token_count(),
			"message_count": len(converted_input_messages),
			"timestamp": __import__("time").time()
		}
		
		retry_count = 0
		last_error = None
		last_error_type = None  # FIXED: Track error type for better retry logic
		start_time = time.time()
		
		while retry_count <= RobustParseConfig.MAX_PARSE_RETRIES:
			try:
				# Prepare messages for this attempt
				current_messages = converted_input_messages.copy()
				
				# FIXED: Only add retry hint on parse errors, not on first attempt
				# Make retry hints provider-aware
				if retry_count > 0 and last_error_type == "parse":
					if will_use_native_tools:
						# For native tools, emphasize correct tool call format
						retry_hint = HumanMessage(content="Please ensure you emit valid function calls using the tool schemas provided.")
					else:
						# For JSON mode, use the standard JSON template hint
						retry_hint = HumanMessage(content=RobustParseConfig.get_json_template_hint())

					# Check if we have room for retry hint using MessageManager API
					if self.message_manager.check_token_safety([retry_hint])['safe']:
						current_messages.append(retry_hint)
						self.logger.debug(f"Added retry hint on attempt {retry_count + 1}")
				
				# Unified tool calling via registry
				try:
					# Track if we've already warned about structured output issues for this session
					if not hasattr(self, '_structured_output_warned'):
						self._structured_output_warned = False

					# Determine provider from model name
					provider = detect_llm_provider(None, self.model_name)
					self.logger.info(f"[DEBUG_TOOLS] Detected provider: {provider}")

					# Check if provider supports native tool calling
					# Use Controller's high-level API instead of directly accessing registry
					supports_native = self.controller and self.controller.supports_native_tools(provider)
					self.logger.info(f"[DEBUG_TOOLS] Provider {provider} supports native tools: {supports_native}")
					self.logger.info(f"[DEBUG_TOOLS] Has controller: {bool(self.controller)}")

					if supports_native:
						# Get ALL actions (core + tools) for this provider
						# Use Controller's high-level API instead of directly accessing registry
						tools = self.controller.get_all_actions_for_provider(provider)
						self.logger.info(f"[DEBUG_TOOLS] Provider {provider} - Got {len(tools) if tools else 0} actions from registry")
						self.logger.debug(f"Provider {provider} - Got {len(tools) if tools else 0} actions from registry")

						# Debug: Log the actual schemas being generated (only on first step)
						if tools and self.state.n_steps == 1:
							import json
							self.logger.debug(f"Generated {len(tools)} tool schemas for {provider}:")
							# Ensure tools is a list for safe slicing
							tools_list = list(tools) if not isinstance(tools, list) else tools
							for i, tool in enumerate(tools_list[:3]):  # Log first 3
								tool_str = json.dumps(tool, indent=2) if isinstance(tool, dict) else str(tool)
								self.logger.debug(f"  Tool schema {i}: {tool_str[:300]}...")

						# NO LIMITS: Pass all tools to the LLM - modern models can handle it
						# Note: "tools" = LLM function calling schemas, "actions" = our internal Registry actions
						self.logger.info(f"Using all {len(tools) if tools else 0} tools for native function calling")

						if tools and len(tools) > 0:
							# Ensure tools is a list for safe slicing
							tools_list = list(tools) if not isinstance(tools, list) else tools
							action_names = [t.get('function', {}).get('name') if isinstance(t, dict) else getattr(t, 'name', 'unknown') for t in tools_list[:5]]
							if len(tools_list) > 5:
								action_names.append(f"... +{len(tools_list)-5} more")
							self.logger.debug(f"Sample tool names: {action_names}")

							# Try native tool calling first
							self.logger.debug(f"Using native tool calling for {provider} with {len(tools)} actions")
							self.logger.info(f"[DEBUG_TOOLS] About to call LLM with tools parameter")
							self.logger.info(f"[DEBUG_TOOLS] Tools type: {type(tools)}, Tools count: {len(tools) if tools else 0}")
							self.logger.info(f"[DEBUG_TOOLS] LLM adapter type: {type(self.llm).__name__}")
							try:
								# Calculate timeout using MessageManager
								tool_count = len(self.controller.get_action_names()) if self.controller else 0
								timeout_seconds = self.message_manager.calculate_llm_timeout(
									tool_count=tool_count,
									use_vision=self.use_vision
								)

								# Call with tools with better error handling
								try:
									parsed = None  # Initialize to prevent NameError
									llm_start = time.time()
									
									# Track LLM call state for stall detection
									self._llm_call_in_progress = True
									self._llm_call_start_time = llm_start

									# 019 P0: llm_started span event — surfaces render
									# "thinking" instead of dead air during LLM latency
									# (the llm_usage completion record never reaches the
									# feed). Fail-open; flag-gated.
									try:
										from core.config_policy import AutonomyConfig as _RunEvCfg
										if _RunEvCfg.run_events_enabled():
											from agents.task.telemetry.views import LLMStartedEvent
											self.telemetry_manager.capture_event(LLMStartedEvent(
												agent_id=self.agent_id,
												step=self.state.n_steps,
												provider=provider,
												model_name=self.model_name,
												attempt=retry_count,
												context_tokens_est=request_info.get("estimated_input_tokens"),
											))
									except Exception:
										pass

									# CONSISTENT ADAPTER PATTERN:
									# Our adapters (OpenAIAdapter, DeepSeekAdapter, etc.) accept tools via kwargs
									# They extract tools from kwargs and call client.generate_agent_response()
									# This provides consistent interface across all providers
									# Stream if supported and callbacks registered
									chunk_count = 0  # Track streaming chunks for telemetry
									if self._supports_streaming() and self.hitl_manager.has_streaming_callbacks():
										self.logger.debug("Using streaming mode for LLM call")
										self.logger.info(f"[DEBUG_TOOLS] Calling llm.astream with tools={len(tools) if tools else 0}")
										full_content = ""
										collected_tool_calls = []

										usage_metadata = None  # Collect usage metadata from final chunk
										# Fix pass 2 (money-correctness): astream() only ever yields ONE chunk
										# (the full ainvoke() result wrapped as a 1-item async iterator -- see
										# LLMClientAdapter.astream), already carrying the per-call
										# `_polyrob_provider_response_id` stamped by adapters.py._agenerate.
										# Propagate it onto the rebuilt `response` below so streaming-mode
										# calls get the same stable, per-call billing dedup key as the
										# non-streaming path instead of falling back to the shared,
										# concurrency-racy `<client>.last_response` read.
										stamped_provider_response_id = None
										async def stream_with_timeout():
											nonlocal full_content, collected_tool_calls, chunk_count, usage_metadata, stamped_provider_response_id
											async for chunk in self.llm.astream(current_messages, tools=tools):
												# Extract content
												if hasattr(chunk, 'content') and chunk.content:
													# Handle both string and list content in streaming
													chunk_text = chunk.content if isinstance(chunk.content, str) else "".join(str(b.text if hasattr(b, "text") else b) for b in chunk.content if b)
													full_content += chunk_text
													await self.hitl_manager.stream_output(chunk_text)
													chunk_count += 1

												# Collect tool calls
												if hasattr(chunk, 'tool_calls') and chunk.tool_calls:
													collected_tool_calls.extend(chunk.tool_calls)

												chunk_stamped_id = getattr(chunk, '_polyrob_provider_response_id', None)
												if chunk_stamped_id:
													stamped_provider_response_id = chunk_stamped_id

											# Collect usage metadata (usually in final chunk)
											if hasattr(chunk, 'usage_metadata') and chunk.usage_metadata:
												usage_metadata = chunk.usage_metadata


										await asyncio.wait_for(stream_with_timeout(), timeout=timeout_seconds)

										# Build complete response
										response = AIMessage(content=full_content, usage_metadata=usage_metadata)
										if collected_tool_calls:
											response.tool_calls = collected_tool_calls
										if stamped_provider_response_id:
											response._polyrob_provider_response_id = stamped_provider_response_id
									else:
										# Regular batch call
										self.logger.info(f"[DEBUG_TOOLS] Calling llm.ainvoke with tools={len(tools) if tools else 0}")
										response = await asyncio.wait_for(
											self.llm.ainvoke(current_messages, tools=tools),
											timeout=timeout_seconds
										)
										self.logger.info(f"[DEBUG_TOOLS] llm.ainvoke returned, response type: {type(response).__name__}")
									llm_duration = time.time() - llm_start
									
									# Clear LLM call tracking
									self._llm_call_in_progress = False
									self._llm_call_start_time = None

									# Extract token usage from response using centralized utility
									token_usage = extract_token_usage(response, provider)
									input_tokens = token_usage.get('prompt_tokens', 0) or 0
									output_tokens = token_usage.get('completion_tokens', 0) or 0
									cached_tokens = token_usage.get('cached_tokens', 0) or 0
									cache_creation_tokens = token_usage.get('cache_creation_tokens', 0) or 0

									self.logger.debug(
										f"Extracted tokens: {input_tokens} input + {output_tokens} output "
										f"(cached: {cached_tokens})"
									)

									# G-26 reachability fix: extract the provider's own response id
									# (when available) as the STABLE dedup key for this completion.
									# Runs synchronously right after the LLM call above returned, with
									# no intervening await, so a concurrent call on a shared client
									# can't overwrite the source before we read it.
									_native_provider = getattr(self, 'llm_provider', 'unknown')
									request_id = extract_stable_request_id(getattr(self, 'llm', None), response, _native_provider)

									# Record LLM usage to ALL systems atomically (SINGLE WRITE PATH)
									# This replaces separate calls to usage_meter + telemetry_manager
									if self.usage_tracker and self.user_id:
										try:
											usage_record = await self.usage_tracker.record_llm_usage(
												user_id=self.user_id,
												session_id=self.session_id,
												agent_id=self.agent_id,
												model=self.model_name,
												provider=_native_provider,
												input_tokens=input_tokens,
												output_tokens=output_tokens,
												cached_tokens=cached_tokens,
												cache_creation_tokens=cache_creation_tokens,
												duration_seconds=llm_duration,
												component="agent",
												purpose="next_action",
												success=True,
												request_id=request_id
											)

											# Log what user was charged (transparent billing)
											self.logger.info(
												f"✓ Step {self.state.n_steps}: {input_tokens:,} in + {output_tokens:,} out = "
												f"{input_tokens + output_tokens:,} tokens | "
												f"API: ${usage_record.costs.api_cost_usd:.6f} → "
												f"User: ${usage_record.costs.user_cost_usd:.6f} "
												f"({usage_record.costs.credits_charged} credits)"
											)

										except InsufficientCreditsError:
											# Billing error - stop execution immediately
											self.logger.warning(
												f"⚠️ Session suspended due to insufficient credits"
											)
											# Re-raise to propagate up to orchestrator
											raise
										except Exception as e:
											self.logger.error(f"Failed to record LLM usage: {e}", exc_info=True)
											# Continue execution - don't fail agent for tracking errors
									elif self.user_id:
										# Fallback: just log telemetry without billing
										# In production, usage_tracker should always be available
										self.logger.warning(
											"usage_tracker not available - telemetry only, NO BILLING! "
											"Check orchestrator initialization."
										)

										# Capture to telemetry for display (no billing)
										if self.telemetry_manager:
											try:
												self.telemetry_manager.capture_llm_usage(
													component="agent",
													purpose="next_action",
													model_name=self.model_name,
													duration_seconds=llm_duration,
													success=True,
													prompt_tokens=input_tokens,
													completion_tokens=output_tokens,
													cached_tokens=cached_tokens,
													agent_id=self.agent_id
												)
											except Exception as e:
												self.logger.debug(f"Failed to capture LLM telemetry: {e}")

									# Emit streaming telemetry if streaming was used
									if self._supports_streaming() and self.hitl_manager.has_streaming_callbacks() and chunk_count > 0:
										if self.telemetry_manager:
											try:
												from agents.task.telemetry.views import StreamingOutputEvent

												event = StreamingOutputEvent(
													agent_id=self.agent_id,
													step=self.state.n_steps,
													total_chunks=chunk_count,
													total_chars=len(full_content) if full_content else 0,
													provider=getattr(self, 'llm_provider', 'unknown'),
													duration_seconds=llm_duration,
													callbacks_count=self.hitl_manager.get_callback_count(),
													callback_failures=self.hitl_manager._callback_failures
												)
												self.telemetry_manager.capture_event(event)
											except Exception as e:
												self.logger.debug(f"Failed to emit streaming telemetry: {e}")

									# Debug: Log detailed response information
									if hasattr(response, 'tool_calls'):
										self.logger.debug(f"Tool calls in response: {len(response.tool_calls) if response.tool_calls else 0}")
										if response.tool_calls:
											for tc in response.tool_calls[:3]:  # Log first 3
												self.logger.debug(f"  Tool call: {tc.function.name if hasattr(tc, 'function') else tc}")
									if hasattr(response, 'content'):
										# Handle both string and list content formats
										content_for_preview = response.content if response.content else "None"
										if isinstance(content_for_preview, list):
											content_preview = str(content_for_preview)[:200]
										else:
											content_preview = str(content_for_preview)[:200]
										self.logger.debug(f"Response content preview: {content_preview}...")
									
									# Store the raw response for later token extraction
									raw_tool_response = response

									# Initialize actions to empty list (will be populated if tool calls exist)
									actions = []
									normalized_tool_calls = []

									# Check if response contains tool calls
									if hasattr(response, 'tool_calls') and response.tool_calls:
										self.logger.info(f"[NATIVE_TOOLS] Received {len(response.tool_calls)} tool_calls from LLM")

										# Get provider name for normalization
										provider = getattr(self, 'llm_provider', None) or 'unknown'

									# Process tool calls using ToolCallBuilder + Registry pipeline
										try:
											from agents.task.agent.message_manager.tool_call_builder import ToolCallBuilder

											# STEP 1: Normalize tool calls using ToolCallBuilder
											normalized_tool_calls = [
												ToolCallBuilder.normalize_and_correct(tc, provider=provider)
												for tc in response.tool_calls
											]
											self.logger.info(f"Normalized {len(response.tool_calls)} tool_calls via ToolCallBuilder")

											# STEP 2: Register with tracker FIRST (before MessageManager)
											if self.tool_call_tracker and normalized_tool_calls:
												registered_ids = self.tool_call_tracker.register_tool_calls(normalized_tool_calls)
												self.logger.info(f"[NATIVE_TOOLS] ✅ Registered {len(registered_ids)} tool calls with tracker")

											# ✅ FIX #6: Don't add model output here - let step() handle it
											# This eliminates duplicate add_model_output() calls

											# STEP 3: Convert to actions using Controller's high-level API
											actions = self.controller.tool_calls_to_actions(normalized_tool_calls)
											self.logger.info(f"Converted {len(normalized_tool_calls)} tool_calls -> {len(actions)} actions via Controller")

											# Extract tool call IDs for debugging
											tool_call_ids = [tc.get('id') for tc in normalized_tool_calls if tc.get('id')]
											
											# Store normalized tool calls for MessageManager (but NOT IDs - use tracker)
											self._current_normalized_tool_calls = normalized_tool_calls  # Store for MessageManager
											
											# Log for debugging
											self.logger.debug(f"Stored {len(tool_call_ids)} tool call IDs: {tool_call_ids}")
											self.logger.debug(f"Stored {len(normalized_tool_calls)} normalized tool calls")
											
											# Check if we got zero actions despite having tool calls
											if len(actions) == 0 and len(normalized_tool_calls) > 0:
												self.logger.error(f"❌ Failed to convert {len(normalized_tool_calls)} tool_calls to actions")
												
												# Log what failed
												for i, tc in enumerate(normalized_tool_calls):
													tc_name = tc.get('name', 'unknown')
													tc_args = tc.get('args', {})
													self.logger.error(f"  Tool call {i}: name='{tc_name}', args={tc_args}")

												# Log available actions for comparison
												# Use Controller's high-level API instead of directly accessing registry
												available = self.controller.list_actions()[:20]
												self.logger.error(f"  Available actions (first 20): {available}")
												
												# This is a CRITICAL error - raise exception instead of continuing
												raise ValueError(
													f"Tool call conversion failed: LLM called {[tc.get('name') for tc in normalized_tool_calls]} "
													f"but these actions don't exist in registry. Check tool loading and action registration."
												)
											
										except Exception as conversion_error:
											self.logger.error(f"Error processing tool_calls: {conversion_error}", exc_info=True)
											# Fall back to safe default
											actions = []
											normalized_tool_calls = []
											# Let execution continue with empty actions - will trigger appropriate error handling
									
									# Initialize action_list to prevent undefined variable errors
									action_list = []
									action_tool_call_ids = []  # P0-2: parallel to action_list; re-stamped onto parsed.action

									if actions:
										# COMPLEX SITUATION: We need BOTH behaviors!
										# 1. Validator needs ActionModel instances (to call .model_dump())
										# 2. AgentOutput creation needs dicts (Pydantic type validation)
										#
										# SOLUTION: Keep instances in actions list, convert to dicts for AgentOutput
										# The validator will use model_output.action (which has instances)
										#
										# But wait - we're creating AgentOutput here with action_list
										# And validator validates model_output.action
										# These are the SAME thing!
										#
										# The issue: AgentOutput type annotation is list[ActionModel] (base class)
										# But we're passing list[ActionModel_toolname] (subclasses)
										# Pydantic rejects subclasses in strict mode!
										#
										# REAL FIX: Convert to dicts for AgentOutput, validator will handle them
										#
										# P0-2: model_dump(exclude_unset=True) DROPS the `_tool_call_id`
										# PrivateAttr that tool_calls_to_actions stamped onto each instance
										# (PrivateAttrs are not serialized). AgentOutput then re-validates the
										# dicts into FRESH instances with _tool_call_id=None — so downstream
										# execution.py always read None, `_pair_results_to_calls.have_ids` was
										# ALWAYS False, and result↔call pairing silently fell to POSITION every
										# step (misattributing results whenever the executor skips a failed-
										# validation action, MCP throttling reorders, or is_done breaks mid-list).
										# Capture the ids here and re-stamp them onto parsed.action below.
										action_tool_call_ids = []
										for action in actions:
											action_tool_call_ids.append(getattr(action, '_tool_call_id', None))
											if hasattr(action, 'model_dump'):
												# Convert to dict - validator accepts both dicts and instances
												action_list.append(action.model_dump(exclude_unset=True))
											else:
												action_list.append(action)

									# Extract brain state from response content using JSON parser
									from agents.task.utils_json import extract_brain_state_from_json

									brain_state = None
									# Normalize content to string (handles both string and list formats)
									# Some providers (Gemini) return content as list of blocks
									content_str = None
									if response.content:
										if isinstance(response.content, str):
											content_str = response.content
										elif isinstance(response.content, list):
											# Extract text from content blocks
											content_str = ""
											for block in response.content:
												if isinstance(block, str):
													content_str += block
												elif hasattr(block, 'text'):
													content_str += block.text
												elif isinstance(block, dict) and 'text' in block:
													content_str += block['text']
										else:
											content_str = str(response.content)

									content_available = content_str and content_str.strip()

									if content_available:
										try:
											brain_fields = extract_brain_state_from_json(content_str)
											brain_state = AgentBrain(**brain_fields)
											self.logger.info(f"✅ Extracted brain state: {brain_state.memory[:100]}")
											
											# ✅ CRITICAL FIX (Nov 17, 2025): Validate memory is NOT identical to previous step
											# This prevents the memory rejection loop where LLM repeats same text
											if hasattr(self, '_last_brain_state') and self._last_brain_state:
												previous_memory = self._last_brain_state.memory if hasattr(self._last_brain_state, 'memory') else ""
												current_memory = brain_state.memory
												
												# Check if memory is identical (exact match)
												if previous_memory and current_memory and previous_memory.strip() == current_memory.strip():
													self.logger.warning(
														f"⚠️  MEMORY VALIDATION FAILED: Memory identical to previous step!\n"
														f"   Previous: {previous_memory[:150]}...\n"
														f"   Current:  {current_memory[:150]}...\n"
														f"   This violates CRITICAL MEMORY RULE #3: Memory MUST be unique each step.\n"
														f"   Will attempt to auto-fix by synthesizing step-specific memory from actions."
													)
													# Mark for synthesis - will be regenerated after actions execute
													brain_state = None
												
												# Check if memory is suspiciously similar (>90% overlap by simple word count)
												elif previous_memory and current_memory:
													prev_words = set(previous_memory.lower().split())
													curr_words = set(current_memory.lower().split())
													if prev_words and curr_words:
														overlap = len(prev_words & curr_words) / max(len(prev_words), len(curr_words))
														if overlap > 0.90:
															self.logger.warning(
																f"⚠️  MEMORY VALIDATION WARNING: Memory {overlap:.0%} similar to previous step.\n"
																f"   LLM may be repeating instead of adding new step-specific information."
															)
											
											# Store current brain state for next validation
											self._last_brain_state = brain_state if brain_state else self._last_brain_state
											
										except Exception as e:
											# ✅ FIX: Defer synthesis until after actions execute
											# This allows synthesis to use actual action results
											self.logger.info(f"Brain state extraction failed: {e}. Will synthesize after actions execute.")
											brain_state = None  # Synthesis #1 will handle this after actions
									else:
										# ✅ FIX: LLM returned tool calls WITHOUT text content
										# Defer synthesis until after actions execute to use actual results
										self.logger.info(
											f"LLM returned tool calls without content (common with OpenAI). "
											f"Will synthesize brain state after actions execute."
										)
										brain_state = None  # Synthesis #1 will handle this after actions

									# Create parsed output with extracted brain state and converted actions
									# This happens for BOTH cases: with actions or without actions
									# If brain_state is None, use a placeholder that will be replaced by synthesis
									if brain_state is None:
										brain_state = AgentBrain(
											page_summary="",
											memory="Synthesis pending - will be generated after actions execute",
											evaluation_previous_goal="Pending",
											next_goal="Pending",
											reasoning="Synthesis pending"
										)
										self.logger.info("Using placeholder brain state - will synthesize after actions")

									parsed = self.AgentOutput(
										current_state=brain_state,
										action=action_list
									)

									# P0-2: re-stamp the tool_call_id PrivateAttr that AgentOutput's
									# re-validation dropped, so identity pairing (result_processing.
									# _pair_results_to_calls) works instead of falling to position.
									# parsed.action is built 1:1 in-order from action_list, so zip aligns.
									try:
										for _act, _tcid in zip(parsed.action, action_tool_call_ids):
											if _tcid is not None:
												_act._tool_call_id = _tcid
									except Exception as _restamp_err:
										self.logger.debug(f"tool_call_id re-stamp skipped: {_restamp_err}")

								except asyncio.TimeoutError:
									self.logger.error(f"LLM call timed out after {timeout_seconds} seconds with {len(tools)} tools", exc_info=True)
									raise
								except TypeError as type_error:
									# Handle cases where bind_tools() is not supported or has invalid tool schemas
									self.logger.error(f"Failed to bind tools to LLM: {type_error}", exc_info=True)
									self.logger.warning("Falling back to non-tool calling mode")
									# Set parsed to None to trigger fallback to structured output
									parsed = None
									# Don't raise - let it fall through to structured output fallback
								except Exception as llm_error:
									self.logger.error(f"LLM call failed with {len(tools)} tools: {llm_error}", exc_info=True)
									# Log tool sample for debugging
									if tools:
										sample = tools[:2] if len(tools) > 2 else tools
										self.logger.debug(f"Sample tools that caused error: {sample}")
									raise

							except Exception as tool_error:
								if not self._structured_output_warned:
									self.logger.warning(f"Native tool calling failed for {provider}: {tool_error}")
									self._structured_output_warned = True
								parsed = None
						else:
							# No tools available - fall back to structured output
							parsed = None
					else:
						# Provider doesn't support native tools - use structured output
						parsed = None

					# Fall back to structured output if native tools didn't work
					if parsed is None:
						self.logger.info("Native tool calling didn't produce parsed output - falling back to structured output")
						try:
							# Try structured output based on provider capabilities
							if provider == 'openai' or 'gpt' in self.model_name.lower():
								# OpenAI - use function_calling method
								structured_llm = self.llm.with_structured_output(
									self.AgentOutput,
									method="function_calling",
									include_raw=True
								)
							else:
								# Other providers - use default structured output
								structured_llm = self.llm.with_structured_output(
									self.AgentOutput,
									include_raw=True
								)

							# Make the call
							tool_count = len(self.controller.get_action_names()) if self.controller else 0
							timeout_seconds = self.message_manager.calculate_llm_timeout(
								tool_count=tool_count,
								use_vision=self.use_vision
							)
							_fb_start = time.time()
							response = await asyncio.wait_for(
								structured_llm.ainvoke(current_messages),
								timeout=timeout_seconds
							)
							# G2: structured-output fallback completion must be billed too.
							await self._bill_llm_response(response, time.time() - _fb_start, provider, purpose="next_action_structured")

							# Extract parsed result
							if isinstance(response, dict) and 'parsed' in response:
								parsed = response['parsed']
								self.logger.info("Got parsed output from structured response")
							elif isinstance(response, self.AgentOutput):
								parsed = response
								self.logger.info("Got direct AgentOutput from structured response")
							else:
								parsed = None
								self.logger.warning("Structured output didn't return valid parsed output")

						except Exception as struct_error:
							if not self._structured_output_warned:
								self.logger.warning(f"Structured output failed: {struct_error}")
								self._structured_output_warned = True
							self.logger.info("Falling back to regular LLM call")
							# Fall back to regular call
							try:
								tool_count = len(self.controller.get_action_names()) if self.controller else 0
								timeout_seconds = self.message_manager.calculate_llm_timeout(
									tool_count=tool_count,
									use_vision=self.use_vision
								)
								# Stream if supported and callbacks registered
								_fb_start = time.time()
								if self._supports_streaming() and self.hitl_manager.has_streaming_callbacks():
									self.logger.debug("Using streaming mode for fallback LLM call")
									response = await self._stream_plain_fallback(current_messages, timeout_seconds)
								else:
									response = await asyncio.wait_for(
										self.llm.ainvoke(current_messages),
										timeout=timeout_seconds
									)
								# G2: plain fallback completion must be billed too.
								await self._bill_llm_response(response, time.time() - _fb_start, provider, purpose="next_action_fallback")
							except asyncio.TimeoutError:
								self.logger.error(f"LLM call timed out after {timeout_seconds:.0f} seconds", exc_info=True)
								raise
							parsed = None
				
				except Exception as structured_error:
					if not self._structured_output_warned:
						self.logger.warning(f"All structured output attempts failed: {structured_error}, using manual parsing")
						self._structured_output_warned = True
					# Fallback to regular LLM call with manual parsing
					try:
						# Calculate timeout using MessageManager
						tool_count = len(self.controller.get_action_names()) if self.controller else 0
						timeout_seconds = self.message_manager.calculate_llm_timeout(
							tool_count=tool_count,
							use_vision=self.use_vision
						)
						# Stream if supported and callbacks registered
						_fb_start = time.time()
						if self._supports_streaming() and self.hitl_manager.has_streaming_callbacks():
							self.logger.debug("Using streaming mode for final fallback LLM call")
							full_content = ""
							fallback_usage_metadata = None  # Initialize for fallback path
							# Fix pass 2 (money-correctness): propagate the per-call stamped
							# provider response id through this rebuild too (see
							# _stream_plain_fallback above for the full rationale).
							stamped_provider_response_id = None

							async def stream_with_timeout():
								nonlocal full_content, fallback_usage_metadata, stamped_provider_response_id
								async for chunk in self.llm.astream(current_messages):
									if hasattr(chunk, 'content') and chunk.content:
										full_content += chunk.content
										await self.hitl_manager.stream_output(chunk.content)
									# Collect usage metadata from final chunk if available
									if hasattr(chunk, 'usage_metadata') and chunk.usage_metadata:
										fallback_usage_metadata = chunk.usage_metadata
									chunk_stamped_id = getattr(chunk, '_polyrob_provider_response_id', None)
									if chunk_stamped_id:
										stamped_provider_response_id = chunk_stamped_id

							await asyncio.wait_for(stream_with_timeout(), timeout=timeout_seconds)
							response = AIMessage(content=full_content, usage_metadata=fallback_usage_metadata)
							if stamped_provider_response_id:
								response._polyrob_provider_response_id = stamped_provider_response_id
						else:
							response = await asyncio.wait_for(
								self.llm.ainvoke(current_messages),
								timeout=timeout_seconds
							)
						# G2: final manual-parse fallback completion must be billed too.
						await self._bill_llm_response(response, time.time() - _fb_start, provider, purpose="next_action_fallback")
					except asyncio.TimeoutError:
						self.logger.error(f"LLM call timed out after {timeout_seconds} seconds", exc_info=True)
						raise
					parsed = None
				
				# Handle manual parsing if structured output failed
				if parsed is None:
					self.logger.info("All structured methods failed - attempting manual JSON parsing")
					# FIXED: More robust content extraction for different LLM response formats
					content = None
					
					# Try multiple ways to extract content from different response formats
					if hasattr(response, 'content'):
						# Standard response format
						content = response.content
					elif isinstance(response, dict):
						# Dictionary response format
						if 'content' in response:
							content = response['content']
						elif 'text' in response:
							content = response['text']
						elif 'message' in response and hasattr(response['message'], 'content'):
							content = response['message'].content
						elif 'generation_info' in response and 'raw' in response['generation_info']:
							# Provider-specific raw payload
							content = str(response['generation_info']['raw'])
					elif isinstance(response, str):
						# Direct string response
						content = response
					elif hasattr(response, 'text'):
						# Alternative text attribute
						content = response.text
					elif hasattr(response, 'message') and hasattr(response.message, 'content'):
						# Nested message structure
						content = response.message.content
					elif hasattr(response, 'generations') and len(response.generations) > 0:
						# Generations format
						gen = response.generations[0]
						if hasattr(gen, 'message') and hasattr(gen.message, 'content'):
							content = gen.message.content
						elif hasattr(gen, 'text'):
							content = gen.text
					elif hasattr(response, 'additional_kwargs') and response.additional_kwargs:
						# Check additional kwargs for content
						if 'content' in response.additional_kwargs:
							content = response.additional_kwargs['content']
						elif 'raw' in response.additional_kwargs:
							content = str(response.additional_kwargs['raw'])


					# Normalize content to string (some providers return list)
					if content is not None and isinstance(content, list):
						content_parts = []
						for block in content:
							if isinstance(block, str):
								content_parts.append(block)
							elif hasattr(block, 'text'):
								content_parts.append(block.text)
							elif isinstance(block, dict) and 'text' in block:
								content_parts.append(block['text'])
						content = "".join(content_parts)

					if content is not None and content.strip():
						try:
							# Direct JSON extraction using centralized utility
							# REFACTORED (Dec 2025): Removed duplicate preprocessing logic
							# normalize_action_schema now handles all action field corrections
							from agents.task.utils_json import (
								extract_json_from_model_output,
								normalize_action_schema,
								preprocess_action_data,
								apply_action_field_corrections
							)

							extracted_json = extract_json_from_model_output(content)

							# Handle single-action format (without 'action' wrapper)
							# This case is NOT handled by normalize_action_schema
							if extracted_json and isinstance(extracted_json, dict):
								if len(extracted_json) == 1 and not any(k in extracted_json for k in ['action', 'current_state', 'title', 'decision', 'confidence']):
									action_name, params = preprocess_action_data(extracted_json)
									params = apply_action_field_corrections(action_name, params)
									extracted_json = {'action': [{action_name: params}]}

							# normalize_action_schema handles all action field corrections
							normalized_json = normalize_action_schema(extracted_json)
							
							# AgentOutput requires 'action' field - LLM must always provide actions
							if 'action' not in normalized_json:
								self.logger.error("Normalized JSON missing 'action' field - LLM response invalid")
								self.logger.error("The LLM must provide actions in every response. Empty actions are not valid.")
								raise LLMResponseError("LLM response missing required 'action' field")

							parsed = self.AgentOutput.model_validate(normalized_json)
							self.logger.info("Manual JSON extraction and validation succeeded")
						except Exception as extract_error:
							self.logger.error(f"Manual JSON extraction failed: {extract_error}", exc_info=True)
							self.logger.error(f"Full content that failed (first 2000 chars, exc_info=True): {content[:2000]}")
							raise LLMResponseError(f"Could not parse response: {str(extract_error)}")
					else:
						# Log the full response for debugging when no content is found
						self.logger.error(f"No content available for manual parsing. Response type: {type(response)}, Response: {str(response)[:1000]}", exc_info=True)
						raise LLMResponseError("No content available for manual parsing")
				
				# Extract token usage and response metadata
				response_for_metadata = raw_tool_response if 'raw_tool_response' in locals() else response if 'response' in locals() else None

				# Try to extract usage from AIMessage.additional_kwargs first (new method)
				token_usage = {}
				if response_for_metadata and hasattr(response_for_metadata, 'additional_kwargs'):
					usage_data = response_for_metadata.additional_kwargs.get('usage')
					if usage_data:
						token_usage = usage_data
						self.logger.debug(f"Extracted usage from AIMessage.additional_kwargs: {token_usage}")

				# Fallback to old extraction method if no usage found
				if not token_usage or token_usage.get('total_tokens') is None:
					self.logger.debug("No usage in additional_kwargs, falling back to extract_token_usage")
					token_usage = extract_token_usage(response_for_metadata, provider)

				total_tokens = token_usage.get("total_tokens")

				# Extract model response metadata
				response_metadata = {}
				try:
					raw_resp = response_for_metadata.get('raw') if response_for_metadata and isinstance(response_for_metadata, dict) else response_for_metadata
					if raw_resp and hasattr(raw_resp, 'model'):
						response_metadata['model'] = raw_resp.model
					if raw_resp and hasattr(raw_resp, 'id'):
						response_metadata['response_id'] = raw_resp.id
					if raw_resp and hasattr(raw_resp, 'created'):
						response_metadata['created'] = raw_resp.created
				except Exception as e:
					self.logger.debug(f"Error extracting response metadata: {e}")
				
				# Update request info with completion details
				elapsed_time = time.time() - start_time
				request_info.update({
					"elapsed_time": elapsed_time,
					"token_usage": token_usage,
					"response_metadata": response_metadata
				})

				# Add request info to session data if available
				if hasattr(self, 'session_data'):
					if "llm_requests" not in self.session_data:
						self.session_data["llm_requests"] = []
					self.session_data["llm_requests"].append(request_info)
				
				# CRITICAL FIX: Ensure parsed is always an AgentOutput object, never a dict
				if parsed is not None and not isinstance(parsed, self.AgentOutput):
					try:
						# If parsed is a dict, validate it into AgentOutput
						if isinstance(parsed, dict):
							self.logger.debug("Converting parsed dict to AgentOutput object")
							# Use centralized normalization for field name consistency
							normalized_dict = normalize_action_schema(parsed)
							parsed = self.AgentOutput.model_validate(normalized_dict)
						else:
							self.logger.warning(f"Unexpected parsed type: {type(parsed)}, attempting validation")
							parsed = self.AgentOutput.model_validate(parsed)
					except Exception as validation_error:
						self.logger.error(f"Failed to validate parsed response into AgentOutput: {validation_error}", exc_info=True)
						parsed = None
						last_error_type = "parse"
				
				if parsed is None and RobustParseConfig.should_retry_parse_error(retry_count):
					# FIXED: Better retry logic with error type tracking
					retry_count += 1
					
					delay = RobustParseConfig.get_exponential_backoff_delay(retry_count - 1)
					
					self.logger.warning(f"Parse attempt {retry_count} failed, retrying in {delay:.1f}s")
					last_error = ValueError("Could not parse response")
					last_error_type = "parse"
					
					await asyncio.sleep(delay)
					continue
				
				if parsed is None:
					# FIX 8: Return safe default instead of raising error after MAX_PARSE_RETRIES
					self.logger.error('Could not parse response after all retry attempts, returning safe default', exc_info=True)
					# P0-5: AgentBrain requires evaluation_previous_goal/memory/next_goal —
					# a bare {"error": ...} dict raises ValidationError, so this "safe
					# default" actually re-raised and was reclassified as a parse error
					# (dead graceful-degradation path). Build a VALID brain instead.
					parsed = self.AgentOutput(
						current_state=AgentBrain(
							evaluation_previous_goal="Failed",
							memory="Previous LLM response could not be parsed after retries.",
							next_goal="Retry with a valid response.",
							reasoning="parse_failed: Failed to parse LLM response after retries",
						),
						action=[]
					)
					# Add parse failure telemetry
					if hasattr(self, 'session_data'):
						if 'parse_failures' not in self.session_data:
							self.session_data['parse_failures'] = []
						self.session_data['parse_failures'].append({
							'timestamp': time.time(),
							'model': self.model_name,
							'retry_count': retry_count
						})

				# FINAL VALIDATION: Double-check that we have a proper AgentOutput object
				if not isinstance(parsed, self.AgentOutput):
					# FIX 8: Convert to safe default instead of raising
					self.logger.error(f'Parsed response is not an AgentOutput object, got: {type(parsed)}', exc_info=True)
					# P0-5: same fix — a valid AgentBrain, not a bare error dict.
					parsed = self.AgentOutput(
						current_state=AgentBrain(
							evaluation_previous_goal="Failed",
							memory=f"Parsed response had the wrong type ({type(parsed).__name__}); expected AgentOutput.",
							next_goal="Retry with a valid response.",
							reasoning="invalid_type: parser returned a non-AgentOutput object",
						),
						action=[]
					)

				# NOTE: LLM telemetry is captured by usage_tracker.record_llm_usage() in the step loop
				# (see line ~3565). Do NOT add duplicate capture_llm_request() here as it causes
				# double counting in stats (50 steps → 100 LLM usage entries).

				# Cut the number of actions to max_actions_per_step
				parsed.action = parsed.action[: self.max_actions_per_step]
				self._log_response(parsed)

				# Post-response vision verification (extracted → _verify_vision_post_response, P7).
				self._verify_vision_post_response(response)

				# DEBUG: Log final parsed output

				return parsed
				
			except Exception as e:
				retry_count += 1
				last_error = e
				elapsed_time = time.time() - start_time
				
				last_error_type = self._classify_llm_error(e)
				
				self.logger.error(f"LLM request attempt {retry_count} failed after {elapsed_time:.2f}s: {str(e)}", exc_info=True)
				
				# Update request info with error details
				request_info.update({
					"elapsed_time": elapsed_time,
					"error": str(e),
					"error_type": last_error_type
				})
				
				# Add error request to session data if available
				if hasattr(self, 'session_data'):
					if "llm_requests" not in self.session_data:
						self.session_data["llm_requests"] = []
					self.session_data["llm_requests"].append(request_info)
				
				# NOTE: LLM telemetry for failed requests is also captured by usage_tracker
				# in the step loop. Do NOT add duplicate capture_llm_request() here.
				
				# Check if we should retry based on error type
				if RobustParseConfig.should_retry_parse_error(retry_count - 1) and last_error_type in ["parse", "rate_limit", "parameter_error"]:
					delay = RobustParseConfig.get_exponential_backoff_delay(retry_count - 1)
					self.logger.warning(f"Retrying LLM call in {delay:.1f}s (attempt {retry_count + 1}, error: {last_error_type})")
					await asyncio.sleep(delay)
					continue
				else:
					self.logger.error(f"LLM call failed after {retry_count} attempts", exc_info=True)
					raise
		
		# Should not reach here, but just in case
		raise last_error or RuntimeError("LLM call failed after all retries")


