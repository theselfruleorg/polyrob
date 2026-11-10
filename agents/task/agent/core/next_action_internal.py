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
    MAX_MCP_PER_STEP,
    format_nag_exempt,
)

# The ONE brain-state shape (native tools): the SystemPrompt's RESPONSE FORMAT
# and this per-step reminder must both name it. utils_json.
# extract_brain_state_from_json reads the nested ``current_state`` object first
# (a flat object is only a tolerated legacy fallback).
_PARSE_FAILURE_RE = re.compile(r"\bpars(?:e|ing)\b|\bjson\b|\bvalidation\b")

NATIVE_TOOLS_BRAIN_HINT = (
    "REMINDER: Your text content must be valid JSON with the brain state nested "
    "under \"current_state\":\n"
    '{"current_state": {"evaluation_previous_goal": "...", "memory": "...", '
    '"next_goal": "...", "reasoning": "..."}}\n'
    "Then emit your function calls."
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
from modules.llm.call_result import read_stamps, replay_fields
from modules.llm.llm_client import translate_llm_error
from modules.llm.retry_log import log_exhausted
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
# ProductTelemetry is accessed via TelemetryManager
from agents.task.utils import (
	time_execution_async, detect_llm_provider, extract_token_usage,
	resolve_serving_provider,
)
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

# Rate limiter for the swallowed usage_tracker.record_llm_usage failure log
from agents.task.agent.core.metering_failure_log import metering_failure_limiter

# STABLE per-completion idempotency key for
# usage_tracker.record_llm_usage's request_id dedup.
from modules.llm.aux_metering import extract_stable_request_id

# Call-quality causes, name repair, regime.
from agents.task.agent.core import call_quality as cq





def _prefix_stamps(agent, response) -> dict:
	"""The usage-record metadata: the stamps of this call plus the session `regime`
	— never a failure (metadata must not break billing)."""
	return cq.with_regime(read_stamps(response), agent)


#: What a call's message carries for its billing, kept when a stream rebuilds the message.
_CALL_MARKS = ("_polyrob_provider_response_id", "_polyrob_billing_reservation_id", "call_facts")


def _carry_call_marks(chunk, marks: dict) -> None:
	for name in _CALL_MARKS:
		value = getattr(chunk, name, None)
		if value:
			marks[name] = value


class NextActionInternalMixin:
	"""Core LLM-invocation method (_get_next_action_internal) split whole out of
	LLMRunnerMixin so llm_runner.py drops under 700L. Agent composes it;
	get_next_action calls it via MRO. Imports above are llm_runner's full set,
	replicated so every name the method references resolves (post-MAX_MCP defense)."""


	def _describe_rejected_tool_calls(self, normalized_tool_calls) -> str:
		"""One honest line per rejected call: unknown action vs. failed validation."""
		errors = {}
		try:
			errors = dict(getattr(self.controller.registry, "_last_validation_errors", {}) or {})
		except Exception:
			errors = {}
		known = set()
		try:
			known = set(self.controller.list_actions())
		except Exception:
			known = set()
		parts = []
		for tc in normalized_tool_calls or []:
			name = tc.get("name", "unknown")
			reason = errors.get(tc.get("id")) or ""
			if name in known:
				parts.append(f"'{name}' exists but its arguments failed validation"
				             + (f": {reason[:300]}" if reason else ""))
			else:
				parts.append(self.controller.describe_unloaded_action(name) if hasattr(self.controller, "describe_unloaded_action") else f"'{name}' is not a registered action")  
		return "; ".join(parts) or "no tool call could be converted"

	def _push_rejected_tool_calls_feedback(self, detail: str) -> None:
		"""Let the model self-correct next step instead of repeating the call blind."""
		try:
			note = HumanMessage(content=("⚠️ Your tool call was rejected before execution and did NOT run:\n"
				f"{detail}\n\nFix the arguments (or pick an action that exists) and call again."))
			note.metadata = {"per_call": True}  # answers ONE call: a restore must not stack it
			self.message_manager.push_ephemeral_message(note)
		except Exception:
			self.logger.debug("rejected-tool-call feedback not pushed", exc_info=True)

	async def _scoped_llm_call(self, messages, tools, timeout_seconds, input_tokens=None):
		"""One model call of this step, ``(response, chunk_count)``. The call's inputs end
		with it, so a later aux call of this task reads defaults."""
		with cq.step_call_scope(self, timeout=timeout_seconds, input_tokens=input_tokens):
			cq.stamp_step_context(self)  # Context slots onto the usage record
			if self._supports_streaming() and self.hitl_manager.has_streaming_callbacks():
				if tools is None:
					return await self._stream_plain_fallback(messages, timeout_seconds), 0
				return await self._stream_with_tools(messages, tools, timeout_seconds)
			response = await asyncio.wait_for(self.llm.ainvoke(messages, tools=tools), timeout=timeout_seconds)
			return response, 0

	async def _stream_plain_fallback(self, current_messages, timeout_seconds) -> AIMessage:
		"""Stream the plain call of a provider with no native tools, accumulating content
		and usage metadata into one AIMessage."""
		reset_stream = getattr(self.hitl_manager, "reset_output_stream", None)
		if callable(reset_stream):
			reset_stream()
		full_content = ""
		fallback_usage_metadata = None
		marks = {}

		async def _run():
			nonlocal full_content, fallback_usage_metadata
			async for chunk in self.llm.astream(current_messages, tools=None):
				if hasattr(chunk, 'content') and chunk.content:
					full_content += chunk.content
					await self.hitl_manager.stream_output(chunk.content)
				if hasattr(chunk, 'usage_metadata') and chunk.usage_metadata:
					fallback_usage_metadata = chunk.usage_metadata
				_carry_call_marks(chunk, marks)

		await asyncio.wait_for(_run(), timeout=timeout_seconds)
		result = AIMessage(content=full_content, usage_metadata=fallback_usage_metadata)
		for name, value in marks.items():
			setattr(result, name, value)
		return result

	async def _bill_llm_response(self, response, llm_duration, provider=None,
								 purpose="next_action"):
		"""Bill exactly one finalized LLM response through the single billing path.

		Billing runs for EVERY response-producing path, so no completion
		consumes tokens without a billing record. Idempotent per response object (marks
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

		self.last_call_facts = getattr(response, "call_facts", None)
		provider = provider or getattr(self, 'llm_provider', 'unknown')
		token_usage = extract_token_usage(response)
		input_tokens = token_usage.get('prompt_tokens', 0) or 0
		output_tokens = token_usage.get('completion_tokens', 0) or 0
		cached_tokens = token_usage.get('cached_tokens', 0) or 0
		cache_creation_tokens = token_usage.get('cache_creation_tokens', 0) or 0
		# The provider's own response id (when available)
		# as the STABLE dedup key for this completion, from its own message.
		request_id = extract_stable_request_id(response, provider)

		# Feed the output-scaled timeout from the SAME extraction the
		# billing record uses — one read of the provider's usage block, not two.
		try:
			self.message_manager.note_call_usage(
				output_tokens=output_tokens, input_tokens=input_tokens,
				cached_tokens=cached_tokens,
				cache_creation_tokens=cache_creation_tokens)
		except Exception:
			pass

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
					cache_creation_1h_tokens=token_usage.get('cache_creation_1h_tokens', 0) or 0,
					duration_seconds=llm_duration,
					component="agent",
					purpose=purpose,
					success=True,
					request_id=request_id,
					# When the provider reports what it actually billed
					# (OpenRouter's `usage.cost`), that figure outranks our
					# per-token estimate. None = not reported, never $0.
					billed_cost_usd=token_usage.get('billed_cost_usd'),
					cache_discount_usd=token_usage.get('cache_discount_usd'),
					metadata=_prefix_stamps(self, response)
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
				# Full traceback the first time per process, then rate-limited
				# (was: a full traceback on EVERY LLM call, flooding the journal
				# when metering was FK-blocked by an unseeded user_profiles row).
				metering_failure_limiter.record(self.logger, e)
		elif self.user_id:
			# Fallback: telemetry only, NO billing (usage_tracker should always exist
			# in production). Visible so JSON/DB divergence is diagnosable.
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
						cache_creation_tokens=cache_creation_tokens,
						agent_id=self.agent_id,
						provider=resolve_serving_provider(self.llm, self.model_name)
					)
				except Exception as e:
					self.logger.debug(f"Failed to capture LLM telemetry: {e}")
		return token_usage

	@staticmethod
	def _classify_llm_error(e: Exception) -> str:
		"""The retry bucket of a failed attempt: the verdict of ``translate_llm_error``,
		and a parse bucket for what no provider said. Only ``parse`` and ``rate_limit``
		retry here: the same key or a dead account fails the same way on every attempt."""
		verdict = translate_llm_error(e)
		if isinstance(verdict, (LLMPermanentError, InsufficientCreditsError)):
			return "permanent"
		if isinstance(verdict, LLMAuthenticationError):
			return "auth"
		if isinstance(verdict, LLMRateLimitError):
			return "rate_limit"
		if isinstance(e, ValidationError) or (
				type(verdict) is LLMError and _PARSE_FAILURE_RE.search(str(e).lower())):
			return "parse"
		return "other"

	def _anchored_input_tokens(self) -> Optional[int]:
		"""The manager's input count for this call: the provider's own number for what it
		already billed, plus the tool schemas. None when it cannot answer."""
		try:
			count = self.message_manager.get_actual_token_count()
		except Exception:
			return None
		return count if isinstance(count, int) and count > 0 else None

	def _verify_vision_post_response(self, response) -> None:
		"""Post-response vision diagnostics (extracted verbatim from
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
				# Don't log ERROR for non-visual tasks
				# The LLM may correctly ignore screenshots when executing filesystem/tool tasks
				# Only log at DEBUG level since this is expected behavior for non-visual tasks
				self.logger.debug(
					f"📷 VISION SKIPPED: LLM response has no visual content ({self._expected_image_count} image(s) sent). "
					f"This is normal for non-visual tasks (filesystem ops, tool calls). "
					f"Response preview: {response_text[:100] if response_text else '(empty)'}..."
				)

			# Clear the counter
			self._expected_image_count = 0

	async def _stream_with_tools(self, current_messages, tools, timeout_seconds) -> Tuple[AIMessage, int]:
		"""Stream the native-tools LLM call, accumulating content / tool calls / usage into one
		AIMessage. Returns ``(response, chunk_count)``. Extracted verbatim from
		``_get_next_action_internal`` — the third inline ``stream_with_timeout``
		copy; ``_stream_plain_fallback`` is the plain-call twin."""
		chunk_count = 0
		self.logger.debug("Using streaming mode for LLM call")
		self.logger.info(f"[DEBUG_TOOLS] Calling llm.astream with tools={len(tools) if tools else 0}")
		reset_stream = getattr(self.hitl_manager, "reset_output_stream", None)
		if callable(reset_stream):
			reset_stream()
		full_content = ""
		collected_tool_calls = []

		usage_metadata = None  # Collect usage metadata from final chunk
		extra_kwargs = {}
		marks = {}
		async def stream_with_timeout():
			nonlocal full_content, collected_tool_calls, chunk_count, usage_metadata
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
				extra_kwargs.update(getattr(chunk, 'additional_kwargs', None) or {})
				_carry_call_marks(chunk, marks)

			# Collect usage metadata (usually in final chunk)
			if hasattr(chunk, 'usage_metadata') and chunk.usage_metadata:
				usage_metadata = chunk.usage_metadata


		await asyncio.wait_for(stream_with_timeout(), timeout=timeout_seconds)

		# Build complete response
		response = AIMessage(content=full_content, usage_metadata=usage_metadata)
		response.additional_kwargs.update(extra_kwargs)
		if collected_tool_calls:
			response.tool_calls = collected_tool_calls
		for name, value in marks.items():
			setattr(response, name, value)
		return response, chunk_count

	@staticmethod
	def _extract_response_content(response) -> Optional[str]:
		"""Pull the text content out of any LLM response shape (AIMessage / dict / str /
		generations / additional_kwargs), normalizing list content to one string. Pure;
		extracted verbatim from ``_get_next_action_internal``."""
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
		return content

	def _parse_fallback_content(self, content: str):
		"""Manual JSON extraction + normalization + ``AgentOutput`` validation for the
		plain fallback path. Raises ``LLMResponseError`` on any failure.
		Extracted verbatim from ``_get_next_action_internal``."""
		try:
			# Direct JSON extraction using centralized utility
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
		return parsed

	def _safe_default_output(self, *, memory: str, reasoning: str):
		"""A VALID ``AgentOutput`` for the graceful-degradation paths: a real
		``AgentBrain`` (a bare error dict raised ValidationError and was reclassified as a
		parse error), no actions."""
		return self.AgentOutput(
			current_state=AgentBrain(
				evaluation_previous_goal="Failed",
				memory=memory,
				next_goal="Retry with a valid response.",
				reasoning=reasoning,
			),
			action=[]
		)

	async def _get_next_action_internal(self, input_messages: list[BaseMessage],
										extra_messages: Optional[list] = None) -> AgentOutput:
		"""One step's call. The request is rebuilt from the message manager (``input_messages``
		is not read); ``extra_messages`` ride the end of this call only (``_call_messages``)."""
		from agents.task.robust_parse_config import RobustParseConfig
		import asyncio
		import time

		# Clear tool call tracker at start of each get_next_action call
		# This prevents accumulation of IDs across multiple calls within the same step
		if self.tool_call_tracker and self.tool_call_tracker.has_active_calls():
			active_calls = self.tool_call_tracker.get_current_step_calls()
			if active_calls:
				self.logger.debug(f"Clearing {len(active_calls)} stale tool calls from previous get_next_action call")
			self.tool_call_tracker.complete_step()

		# Clear any previous normalized tool calls (but use tracker for IDs)
		if hasattr(self, '_current_normalized_tool_calls'):
			delattr(self, '_current_normalized_tool_calls')
		cq.set_call_issue(self, None)  # This call's tool-free cause, if any

		# Cut messages before LLM call only if we're really close to the limit
		estimated_usage = self.message_manager.get_estimated_context_usage()
		if estimated_usage > 0.92:  # Warn at 92% context usage
			self.logger.warning(f"High context usage: {estimated_usage:.1%} - consider checkpointing")
		
		# Provider-aware format hint injection
		# First determine if we'll be using native tools
		provider = resolve_serving_provider(self.llm, self.model_name)
		will_use_native_tools = (
			self.use_native_tools and  # User preference
			self.controller and
			# Use Controller's high-level API instead of directly accessing registry
			self.controller.supports_native_tools(provider)  # Provider capability
		)

		# Use ephemeral message API for format hints
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
			formatting_message.metadata = {"per_call": True}  # Never re-queued

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
			native_tools_hint = HumanMessage(content=NATIVE_TOOLS_BRAIN_HINT)
			native_tools_hint.metadata = {"per_call": True}  # Never re-queued
			# Use the new ephemeral message API
			self.message_manager.push_ephemeral_message(native_tools_hint)
			format_hint_added = True
			self.logger.debug("Format hint prepared for injection")

		# Don't consume ephemeral message yet - get regular messages first
		# This prevents losing the format hint during token management
		converted_input_messages = self.message_manager.get_messages()  # NOT get_messages_for_llm() yet!

		# Check token safety using MessageManager's API (SINGLE SOURCE OF TRUTH).
		# Overflow first gets ONE emergency prune (the recalibration above can
		# reveal an overflow the step-boundary gate never saw); raises if still over.
		from agents.task.agent.core.token_budget import ensure_token_budget
		safety_check = ensure_token_budget(self.message_manager, self.logger)
		self.logger.debug(
			f"Token usage: {safety_check['usage_percent']:.1f}% "
			f"({safety_check['current_tokens']}/{safety_check['max_limit']} tokens)"
		)

		# NOW get final messages with ephemeral message included (one-shot consumption)
		converted_input_messages = self._call_messages(
			self.message_manager.get_messages_for_llm(), extra_messages)

		self.last_call_facts = None

		estimated_input_tokens = self.message_manager.get_token_count()
		
		retry_count = 0
		empty_retry_left = 1  # One in-call retry of a truly empty completion
		last_error = None
		last_error_type = None  # Track error type for better retry logic
		start_time = time.time()
		
		while retry_count <= RobustParseConfig.MAX_PARSE_RETRIES:
			try:
				# Prepare messages for this attempt
				current_messages = converted_input_messages.copy()
				
				# Only add retry hint on parse errors, not on first attempt
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
				parsed = None
				# The provider SERVING this session — not the vendor the model
				# registry credits with the model id. A `zai-coding` session
				# serving `glm-5` (an OpenRouter row in the registry) must emit
				# Anthropic-shaped tools or z.ai answers 422.
				provider = resolve_serving_provider(self.llm, self.model_name)
				self.logger.info(f"[DEBUG_TOOLS] Serving provider: {provider}")

				# Check if provider supports native tool calling
				# Use Controller's high-level API instead of directly accessing registry
				supports_native = self.controller and self.controller.supports_native_tools(provider)
				self.logger.info(f"[DEBUG_TOOLS] Provider {provider} supports native tools: {supports_native}")
				self.logger.info(f"[DEBUG_TOOLS] Has controller: {bool(self.controller)}")

				if supports_native:
					# Get ALL actions (core + tools) for this provider
					# Use Controller's high-level API instead of directly accessing registry
					# A session restored from disk asks for the tool order it
					# emitted BEFORE the restart, so the tools block — the largest
					# cached bytes in the request — comes back identical. None on a
					# fresh session ⇒ the plain sorted order.
					tools = self.controller.get_all_actions_for_provider(
						provider,
						preferred_order=getattr(
							self.message_manager, '_foundation_tool_names', None),
					)
					# Remember what actually went out, so the next save_to_disk
					# persists the live order (a tool loaded mid-session included).
					try:
						from modules.llm.tool_schemas import schema_tool_names
						self.message_manager._foundation_tool_names = \
							schema_tool_names(tools) or None
					except Exception:
						pass
					# Stamp the schema list's token cost
					# onto the gauge — real billed prompt bytes every step that were
					# invisible to get_actual_token_count. Fail-open; memoized in
					# the registry so this is a cheap dict read per step.
					try:
						self.message_manager.set_tool_schema_tokens(
							self.controller.get_tool_schema_token_estimate(provider))
					except Exception:
						pass
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

					# The RIG is the limit. Every registered action is
					# emitted here — there is deliberately no second filter at this
					# seam, because a filter here would disagree with the catalog the
					# agent was shown and with what `load_tool` can materialize.
					# Narrowing belongs where the session is BUILT:
					# `core/config_policy/rigs.py` picks the tool_ids a cron/goal run
					# loads, and progressive disclosure lets that run widen itself.
					# ("tools" = LLM function-calling schemas; "actions" = internal
					# Registry actions.)
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
						# Calculate timeout using MessageManager
						tool_count = len(self.controller.get_action_names()) if self.controller else 0
						timeout_seconds = self.message_manager.calculate_llm_timeout(
							tool_count=tool_count,
							use_vision=self.use_vision
						)
						anchored_tokens = self._anchored_input_tokens()

						# Call with tools with better error handling
						try:
							parsed = None  # Initialize to prevent NameError
							llm_start = time.time()
							
							# In flight for the stall watchdog; cleared in finally, so a
							# raise or a cancel never leaves the watchdog blind.
							from agents.task.agent.core.run_state import llm_call_state
							with llm_call_state(self).active(llm_start):
								# llm_started span event — surfaces render
								# "thinking" instead of dead air during LLM latency
								# (the llm_usage completion record never reaches the
								# feed). Fail-open.
								try:
									from agents.task.telemetry.views import LLMStartedEvent
									self.telemetry_manager.capture_event(LLMStartedEvent(
										agent_id=self.agent_id,
										step=self.state.n_steps,
										provider=provider,
										model_name=self.model_name,
										attempt=retry_count,
										context_tokens_est=estimated_input_tokens,
									))
								except Exception:
									pass

								# CONSISTENT ADAPTER PATTERN:
								# Our adapters (OpenAIAdapter, DeepSeekAdapter, etc.) accept tools via kwargs
								# They extract tools from kwargs and call client.generate_agent_response()
								# This provides consistent interface across all providers
								# Stream if supported and callbacks registered
								response, chunk_count = await self._scoped_llm_call(
									current_messages, tools, timeout_seconds, anchored_tokens)
								llm_duration = time.time() - llm_start

							self.last_call_facts = getattr(response, "call_facts", None)
							# Extract token usage from response using centralized utility
							token_usage = extract_token_usage(response)
							input_tokens = token_usage.get('prompt_tokens', 0) or 0
							output_tokens = token_usage.get('completion_tokens', 0) or 0
							cached_tokens = token_usage.get('cached_tokens', 0) or 0
							cache_creation_tokens = token_usage.get('cache_creation_tokens', 0) or 0

							self.logger.debug(
								f"Extracted tokens: {input_tokens} input + {output_tokens} output "
								f"(cached: {cached_tokens})"
							)
							# The NATIVE-tools path is the one prod runs (purpose
							# "next_action"); it feeds the output-scaled timeout, which
							# otherwise reads the 1,024-token default.
							# Same extraction, one read.
							try:
								self.message_manager.note_call_usage(
									output_tokens=output_tokens, input_tokens=input_tokens,
									cached_tokens=cached_tokens,
									cache_creation_tokens=cache_creation_tokens)
							except Exception:
								pass

							# The provider's own response id (when
							# available) as the STABLE dedup key, from this call's own message.
							_native_provider = getattr(self, 'llm_provider', 'unknown')
							request_id = extract_stable_request_id(response, _native_provider)

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
										cache_creation_1h_tokens=token_usage.get('cache_creation_1h_tokens', 0) or 0,
										duration_seconds=llm_duration,
										component="agent",
										purpose="next_action",
										success=True,
										request_id=request_id,
										# The provider's billed cost (see the helper above).
										billed_cost_usd=token_usage.get('billed_cost_usd'),
										cache_discount_usd=token_usage.get('cache_discount_usd'),
										metadata=_prefix_stamps(self, response)
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
											agent_id=self.agent_id,
											provider=resolve_serving_provider(self.llm, self.model_name)
										)
									except Exception as e:
										self.logger.debug(f"Failed to capture LLM telemetry: {e}")

							# A truly empty completion (no text, no tool call) re-issues the
							# SAME call once here instead of costing a whole agent step.
							if empty_retry_left and cq.worth_empty_retry(response):
								empty_retry_left = 0
								cq.emit_call_quality(self, cq.EMPTY, in_call_retry=True)
								self.logger.warning("Empty completion (no text, no tool call) — re-issuing the same call once")
								llm_start = time.time()
								response, chunk_count = await self._scoped_llm_call(
									current_messages, tools, timeout_seconds, anchored_tokens)
								await self._bill_llm_response(response, time.time() - llm_start, _native_provider, purpose="next_action_empty_retry")

							# Emit streaming telemetry if streaming was used
							if self._supports_streaming() and self.hitl_manager.has_streaming_callbacks() and chunk_count > 0:
								if self.telemetry_manager:
									try:
										from agents.task.telemetry.views import StreamingOutputEvent

										event = StreamingOutputEvent(
											agent_id=self.agent_id,
											step=self.state.n_steps,
											total_chunks=chunk_count,
											total_chars=len(str(getattr(response, "content", "") or "")),
											provider=getattr(self, 'llm_provider', 'unknown'),
											duration_seconds=llm_duration,
											callbacks_count=self.hitl_manager.get_callback_count(),
											callback_failures=self.hitl_manager._callback_failures
										)
										self.telemetry_manager.capture_event(event)
									except Exception as e:
										self.logger.debug(f"Failed to emit streaming telemetry: {e}")

							self.logger.debug(
								f"Tool calls in response: {len(getattr(response, 'tool_calls', None) or [])}; "
								f"content preview: {str(getattr(response, 'content', None) or 'None')[:200]}...")
							# Store the raw response for later token extraction
							raw_tool_response = response

							# Initialize actions to empty list (will be populated if tool calls exist)
							actions = []
							normalized_tool_calls = []

							# In an autonomous run that has acted, a text-only
							# reply IS the final report — it becomes done(text=...).
							_final = cq.prose_final_report(self, response)
							if _final:
								response.tool_calls = [_final]
							elif not getattr(response, 'tool_calls', None):
								cq.set_call_issue(self, cq.EMPTY if cq.is_truly_empty(response) else cq.TEXT_ONLY)
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
									# Repair near-miss names (same registry/permission path after).
									cq.repair_tool_call_names(self, normalized_tool_calls)

									# STEP 2: Register with tracker FIRST (before MessageManager)
									if self.tool_call_tracker and normalized_tool_calls:
										registered_ids = self.tool_call_tracker.register_tool_calls(normalized_tool_calls)
										self.logger.info(f"[NATIVE_TOOLS] ✅ Registered {len(registered_ids)} tool calls with tracker")

									# step() adds the model output (once); keep its reasoning for that.
									self._current_replay_fields = replay_fields(response.additional_kwargs)

									# STEP 3: Convert to actions using Controller's high-level API
									actions = self.controller.tool_calls_to_actions(normalized_tool_calls)
									cq.note_rejected_calls(self, normalized_tool_calls, actions)
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

										# Say what actually happened: a rejected `message` call may
										# name an action that exists whose arguments failed validation.
										# The model must see the reason, or it repeats the same call and
										# two empty steps trip the thinking-loop escalation.
										detail = self._describe_rejected_tool_calls(normalized_tool_calls)
										self._push_rejected_tool_calls_feedback(detail)
										raise ValueError(f"Tool call conversion failed: {detail}")
									
								except Exception as conversion_error:
									self.logger.error(f"Error processing tool_calls: {conversion_error}", exc_info=True)
									# Fall back to safe default
									actions = []
									normalized_tool_calls = []
									# Let execution continue with empty actions - will trigger appropriate error handling
							
							# Initialize action_list to prevent undefined variable errors
							action_list = []
							action_tool_call_ids = []  # Parallel to action_list; re-stamped onto parsed.action

							if actions:
								# AgentOutput needs dicts (a list of ActionModel SUBCLASSES fails its
								# list[ActionModel] validation), so each action is dumped to a dict.
								# Model_dump drops the `_tool_call_id` PrivateAttr, so the ids
								# are captured here and re-stamped onto parsed.action below — else
								# result<->call pairing silently falls back to list POSITION.
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
									
									# Validate memory is NOT identical to previous step
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
									# Defer synthesis until after actions execute
									# This allows synthesis to use actual action results
									self.logger.info(f"Brain state extraction failed: {e}. Will synthesize after actions execute.")
									brain_state = None  # Synthesis #1 will handle this after actions
							else:
								# LLM returned tool calls WITHOUT text content
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

							# Re-stamp the tool_call_id PrivateAttr that AgentOutput's
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
							# Per-attempt: this
							# `raise`s onward to the outer retry loop, which owns the
							# turn's one final ERROR line.
							self.logger.warning(f"LLM call timed out after {timeout_seconds} seconds with {len(tools)} tools", exc_info=True)
							raise
						except Exception as llm_error:
							# The tool-calling
							# attempt failed — per-attempt, `raise`s onward to the
							# outer retry loop.
							self.logger.warning(f"LLM call failed with {len(tools)} tools: {llm_error}", exc_info=True)
							# Log tool sample for debugging
							if tools:
								sample = tools[:2] if len(tools) > 2 else tools
								self.logger.debug(f"Sample tools that caused error: {sample}")
							raise

				# The provider has no native tools, or the registry emitted none: one plain
				# call, parsed from its text. A failed native call never comes here: it would
				# bill again with no tools and lose the tool calls.
				if parsed is None:
					tool_count = len(self.controller.get_action_names()) if self.controller else 0
					timeout_seconds = self.message_manager.calculate_llm_timeout(
						tool_count=tool_count,
						use_vision=self.use_vision
					)
					_fb_start = time.time()
					try:
						response, _ = await self._scoped_llm_call(current_messages, None, timeout_seconds)
					except asyncio.TimeoutError:
						# Per-attempt; `raise`s onward.
						self.logger.warning(f"LLM call timed out after {timeout_seconds:.0f} seconds", exc_info=True)
						raise
					await self._bill_llm_response(response, time.time() - _fb_start, provider, purpose="next_action_fallback")

					# More robust content extraction for different LLM response formats
					content = self._extract_response_content(response)

					if content is not None and content.strip():
						parsed = self._parse_fallback_content(content)
					else:
						# Log the full response for debugging when no content is found.
						# Per-attempt: this
						# `raise`s onward to the outer retry loop, which owns the
						# turn's one final ERROR line.
						self.logger.warning(f"No content available for manual parsing. Response type: {type(response)}, Response: {str(response)[:1000]}", exc_info=True)
						raise LLMResponseError("No content available for manual parsing")
				
				# Ensure parsed is always an AgentOutput object, never a dict
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
						# Per-attempt:
						# doesn't raise, feeds the parse-retry decision below.
						self.logger.warning(f"Failed to validate parsed response into AgentOutput: {validation_error}", exc_info=True)
						parsed = None
						last_error_type = "parse"
				
				if parsed is None and RobustParseConfig.should_retry_parse_error(retry_count):
					# Better retry logic with error type tracking
					retry_count += 1
					
					delay = RobustParseConfig.get_exponential_backoff_delay(retry_count - 1)
					
					self.logger.warning(f"Parse attempt {retry_count} failed, retrying in {delay:.1f}s")
					last_error = ValueError("Could not parse response")
					last_error_type = "parse"
					
					await asyncio.sleep(delay)
					continue
				
				if parsed is None:
					# Return safe default instead of raising error after MAX_PARSE_RETRIES
					self.logger.error('Could not parse response after all retry attempts, returning safe default', exc_info=True)
					# AgentBrain requires evaluation_previous_goal/memory/next_goal —
					# a bare {"error": ...} dict raises ValidationError, so this "safe
					# default" actually re-raised and was reclassified as a parse error
					# (dead graceful-degradation path). Build a VALID brain instead.
					parsed = self._safe_default_output(memory="Previous LLM response could not be parsed after retries.", reasoning="parse_failed: Failed to parse LLM response after retries")
					cq.set_call_issue(self, cq.PARSE_FAILURE)
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
					# Convert to safe default instead of raising
					self.logger.error(f'Parsed response is not an AgentOutput object, got: {type(parsed)}', exc_info=True)
					# Same fix — a valid AgentBrain, not a bare error dict.
					parsed = self._safe_default_output(memory=f"Parsed response had the wrong type ({type(parsed).__name__}); expected AgentOutput.", reasoning="invalid_type: parser returned a non-AgentOutput object")

				# NOTE: LLM telemetry is captured by usage_tracker.record_llm_usage() in the step loop
				# (see line ~3565). Do NOT add duplicate capture_llm_request() here as it causes
				# double counting in stats (50 steps → 100 LLM usage entries).

				self._log_response(parsed)

				# Post-response vision verification (extracted → _verify_vision_post_response).
				self._verify_vision_post_response(response)

				# DEBUG: Log final parsed output

				return parsed
				
			except Exception as e:
				retry_count += 1
				last_error = e
				elapsed_time = time.time() - start_time
				
				last_error_type = self._classify_llm_error(e)

				# "attempt N of M":
				# per-attempt, not the turn's final word (may retry below).
				# The single final ERROR is the `else:` branch further down.
				self.logger.warning(f"LLM request attempt {retry_count} failed after {elapsed_time:.2f}s: {str(e)}", exc_info=True)
				
				# NOTE: LLM telemetry for failed requests is also captured by usage_tracker
				# in the step loop. Do NOT add duplicate capture_llm_request() here.
				
				# Check if we should retry based on error type
				if RobustParseConfig.should_retry_parse_error(retry_count - 1) and last_error_type in ["parse", "rate_limit"]:
					delay = RobustParseConfig.get_exponential_backoff_delay(retry_count - 1)
					self.logger.warning(f"Retrying LLM call in {delay:.1f}s (attempt {retry_count + 1}, error: {last_error_type})")
					await asyncio.sleep(delay)
					continue
				else:
					# The ONE final ERROR for
					# this retry loop -- every per-attempt log above
					# is WARNING. Rendering extracted to modules/llm/retry_log.py.
					log_exhausted(self.logger, retry_count=retry_count, provider=provider, exc=e)
					raise
		
		# Should not reach here, but just in case
		raise last_error or RuntimeError("LLM call failed after all retries")


