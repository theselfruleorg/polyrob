from __future__ import annotations

import logging
import sys
from typing import Any, Dict, List, Optional

from agents.task.agent.views import ActionResult, AgentBrain, AgentOutput, AgentStepInfo
from modules.llm.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from modules.llm.adapters import BaseChatModel

# LLM module availability flag + token counters (mirrors service.py module-level setup)
try:
    from modules.llm.model_registry import get_model_config
    from modules.llm import count_tokens, count_messages_tokens
    LLM_MODULE_AVAILABLE = True
except ImportError:
    LLM_MODULE_AVAILABLE = False

logger = logging.getLogger(__name__)


class TokenCounterMixin:
	# Empty slots so the composed MessageManager keeps its own __slots__ (no __dict__).
	__slots__ = ()

	def _calculate_token_limits(self, llm: BaseChatModel,
	                            max_input_override: Optional[int]) -> tuple:
		"""Calculate token limits from model config (SINGLE SOURCE OF TRUTH).

		Args:
			llm: Language model instance
			max_input_override: Optional explicit limit (None = auto-calculate)

		Returns:
			(max_input_tokens, safe_input_tokens, completion_reserve)
		"""
		from modules.llm.model_registry import get_model_config
		from agents.task.robust_parse_config import RobustParseConfig

		# Env override (operational knob): cap the effective input budget regardless of
		# the model's native window. An explicit constructor override still wins.
		if max_input_override is None:
			import os
			_env_cap = os.getenv("TASK_MAX_INPUT_TOKENS")
			if _env_cap and _env_cap.strip().isdigit():
				max_input_override = int(_env_cap)
				self.logger.info(f"TASK_MAX_INPUT_TOKENS override: max_input={max_input_override}")

		# Use override if provided
		if max_input_override is not None:
			max_input = max_input_override
			safe_input = int(max_input * (1 - RobustParseConfig.SAFETY_MARGIN_PERCENT))
			self.logger.info(f"Using explicit token limit: max={max_input}, safe={safe_input}")
			return max_input, safe_input, 4096  # Default completion reserve

		# Auto-calculate from model
		model_name = self._model_name
		model_config = get_model_config(model_name)

		if model_config and model_config.context_window:
			# 95% of context window minus completion reserve
			context_95pct = int(model_config.context_window * 0.95)
			completion_reserve = model_config.max_completion_tokens
			max_input = max(1000, context_95pct - completion_reserve)

			# Apply safety margin (default 5%)
			safe_input = int(max_input * (1 - RobustParseConfig.SAFETY_MARGIN_PERCENT))

			self.logger.info(
				f"Calculated token limits for {model_name}: "
				f"context={model_config.context_window}, "
				f"max_input={max_input}, safe_input={safe_input}, "
				f"completion_reserve={completion_reserve}"
			)

			return max_input, safe_input, completion_reserve
		else:
			# Fallback defaults
			self.logger.warning(f"No model config found for {model_name}, using fallback limits")
			max_input = 120000
			safe_input = int(max_input * 0.95)
			return max_input, safe_input, 4096

	def _count_message_tokens(self, message: BaseMessage) -> int:
		"""Count tokens for a single message using modules.llm.

		Args:
			message: BaseMessage to count tokens for

		Returns:
			Token count
		"""
		if not LLM_MODULE_AVAILABLE:
			text = str(message.content if hasattr(message, 'content') else message)
			return max(1, len(text) // 4) if text else 0

		try:
			# Convert to dict format for modules.llm
			message_dict = {
				"role": message.type,
				"content": message.content
			}
			# CX-H4: tool-call arguments are real prompt bytes — the downstream
			# counter supports them; omitting them makes compaction fire late.
			tool_calls = getattr(message, "tool_calls", None)
			if tool_calls:
				message_dict["tool_calls"] = tool_calls
			return count_messages_tokens([message_dict], self.model_name)
		except Exception as e:
			self.logger.debug(f"Token counting failed: {e}, using fallback")
			text = str(message.content if hasattr(message, 'content') else message)
			fallback_len = len(text)
			tool_calls = getattr(message, "tool_calls", None)
			if tool_calls:
				import json
				fallback_len += len(json.dumps(tool_calls))
			return max(1, fallback_len // 4) if fallback_len else 0

	def _increment_token_count(self, delta: int) -> None:
		"""Increment the total token count by delta.

		This method provides efficient token counting without recalculation.
		Token counts are now trusted from centralized modules.llm - no periodic recalibration needed.

		Args:
			delta: The number of tokens to add (positive) or remove (negative)
		"""
		previous_count = self.history.total_tokens
		self.history.total_tokens = max(0, self.history.total_tokens + delta)

		if delta != 0:
			self.logger.debug(
				f"Token count incremented: {previous_count} → {self.history.total_tokens} (Δ{delta:+})"
			)

		# Check if we need to trim after increment
		if self.history.total_tokens > self.max_input_tokens:
			self.logger.debug(
				f"Token count {self.history.total_tokens} exceeds limit {self.max_input_tokens} "
				f"(trimming disabled - using full context)"
			)

	def get_total_tokens(self) -> int:
		"""Get total tokens including all foundation messages.
		
		FIX #4: Single source of truth for total token count.
		Includes system message, initial task, and conversation history.
		
		Returns:
			Total token count across all message storage
		"""
		return (
			self.history.total_tokens +
			self._system_message_tokens +
			self._initial_task_tokens +
			getattr(self, '_skill_message_tokens', 0) +
			getattr(self, '_self_context_tokens', 0) +
			getattr(self, '_project_context_tokens', 0) +
			getattr(self, '_runtime_identity_tokens', 0)
		)

	def estimate_tokens(self, messages: List[BaseMessage]) -> int:
		"""Estimate total tokens for a list of messages using modules.llm.

		Args:
			messages: List of BaseMessage objects to count tokens for

		Returns:
			Total estimated token count for all messages
		"""
		if not LLM_MODULE_AVAILABLE:
			# Fallback: estimate 4 chars per token
			total_chars = sum(len(str(m.content)) for m in messages if hasattr(m, 'content'))
			return max(1, total_chars // 4)

		try:
			# Convert to message dicts for modules.llm
			message_dicts = []
			for msg in messages:
				message_dict = {"role": msg.type, "content": msg.content}
				# CX-H4: mirror _count_message_tokens — include tool-call args.
				tool_calls = getattr(msg, "tool_calls", None)
				if tool_calls:
					message_dict["tool_calls"] = tool_calls
				message_dicts.append(message_dict)
			return count_messages_tokens(message_dicts, self.model_name)
		except Exception as e:
			self.logger.debug(f"Token counting failed: {e}, using fallback")
			import json
			total_chars = 0
			for m in messages:
				if hasattr(m, 'content'):
					total_chars += len(str(m.content))
				tool_calls = getattr(m, "tool_calls", None)
				if tool_calls:
					total_chars += len(json.dumps(tool_calls))
			return max(1, total_chars // 4)

	def _get_model_token_limits(self) -> tuple[int, int]:
		"""Get token limits from model registry.

		Returns:
			Tuple of (context_window, max_completion_tokens)
		"""
		from modules.llm.model_registry import get_model_config
		model_name = self.model_name

		# Use centralized model registry
		config = get_model_config(model_name)
		if config:
			context_window = config.context_window
			completion_tokens = config.max_completion_tokens
		else:
			# Conservative fallback
			context_window = 128000
			completion_tokens = 16384
		self.logger.info(f"Got limits for {model_name}: context={context_window}, completion={completion_tokens}")
		return context_window, completion_tokens

	def get_memory_usage_estimate(self) -> int:
		"""Estimate memory usage of message history in bytes.

		Returns:
			Estimated memory usage in bytes
		"""
		import sys

		total_size = 0
		for managed_msg in self.history.messages:
			msg = managed_msg.message

			# Estimate size of message content
			if hasattr(msg, 'content') and msg.content:
				total_size += sys.getsizeof(str(msg.content))

			# Add size of metadata
			if hasattr(managed_msg, 'metadata'):
				total_size += sys.getsizeof(managed_msg.metadata)

			# Add overhead for message object itself
			total_size += sys.getsizeof(msg)

		return total_size

	def recalibrate_token_counts(self, force: bool = False) -> None:
		"""Recalculate token counts using unified counting method.

		This is now only called when explicitly requested or when model changes.

		Args:
			force: If True, always recalibrate even if not necessary
		"""
		self.logger.info("Recalibrating token counts for all messages")

		# Check if model limits have changed (e.g. a fallback to a smaller-context
		# model). Recompute the input budget the SAME way _calculate_token_limits does
		# — 95% of the window MINUS the completion reserve — instead of using the raw
		# context window (which leaves zero room for the response). Update BOTH
		# max_input_tokens AND safe_input_tokens so check_token_safety stays consistent.
		context_window, completion_tokens = self._get_model_token_limits()

		if context_window > 0:
			from agents.task.robust_parse_config import RobustParseConfig
			new_max_input = max(1000, int(context_window * 0.95) - completion_tokens)
			if new_max_input < self.max_input_tokens:
				old_limit = self.max_input_tokens
				self.max_input_tokens = new_max_input
				self.safe_input_tokens = int(new_max_input * (1 - RobustParseConfig.SAFETY_MARGIN_PERCENT))
				self.logger.info(
					f"Updated token limits after model change: max_input={new_max_input}, "
					f"safe_input={self.safe_input_tokens} (was max_input={old_limit})"
				)
		
		# Store the original total
		original_total = self.history.total_tokens
		
		# Reset total
		self.history.total_tokens = 0
		
		# Recalculate for each message
		for managed_msg in self.history.messages:
			old_count = managed_msg.metadata.input_tokens
			new_count = self._count_message_tokens(managed_msg.message)
			managed_msg.metadata.input_tokens = new_count
			self.history.total_tokens += new_count
		
		# Log the change (only if significant to reduce spam)
		diff = self.history.total_tokens - original_total
		if abs(diff) > 100:  # Only log if significant change
			self.logger.info(f"Token count recalibration: {original_total} → {self.history.total_tokens} (Δ{diff:+})")
		elif diff != 0:
			self.logger.debug(f"Token count recalibration: {original_total} → {self.history.total_tokens} (Δ{diff:+})")
		# else: no logging for unchanged recalibration
		
		# Capture telemetry for token count changes if significant
		if abs(diff) > 100:  # Only capture if change is significant
			try:
				from agents.task import capture_llm_request
				
				capture_llm_request(
					component="MessageManager",
					purpose="token_recalibration",
					model_name=self.model_name,
					duration_seconds=0.0,  # No actual LLM call
					success=True,
					token_count=self.history.total_tokens,
					session_id=self.session_id
				)
				
				self.logger.debug("Captured telemetry for token count recalibration")
				
			except Exception as e:
				self.logger.debug(f"Failed to capture telemetry for token recalibration: {e}")
		
		# Log if token count exceeds max (trimming disabled)
		if self.history.total_tokens > self.max_input_tokens:
			self.logger.debug(f"Token count {self.history.total_tokens} exceeds limit {self.max_input_tokens} (trimming disabled)")

	def get_token_count(self) -> int:
		"""Get the current total token count including all separately stored messages.

		NOTE: This does NOT include H-MEM or ephemeral messages.
		Use get_actual_token_count() for the true count that will be sent to LLM.
		"""
		return (self.history.total_tokens + self._system_message_tokens
				+ self._initial_task_tokens + getattr(self, '_skill_message_tokens', 0)
				+ getattr(self, '_self_context_tokens', 0)
				+ getattr(self, '_project_context_tokens', 0)
				+ getattr(self, '_runtime_identity_tokens', 0))

	def get_actual_token_count(self) -> int:
		"""Get the ACTUAL token count including H-MEM and ephemeral messages.

		This returns the true token count that will be sent to the LLM,
		including dynamically injected hierarchical memory and ephemeral messages.

		Use this for accurate context usage calculations before LLM calls.

		NOTE: H-MEM tokens are cached per-step to avoid duplicate hierarchical searches.
		Cache is invalidated when invalidate_hmem_cache() is called (typically at step start).
		"""
		# Start with base count (system + task + history)
		base_count = self.get_token_count()

		# Add ephemeral messages if any
		ephemeral_tokens = 0
		if hasattr(self, '_ephemeral_messages') and self._ephemeral_messages:
			ephemeral_tokens = self.estimate_tokens(self._ephemeral_messages)

		# Estimate H-MEM tokens if available
		hmem_tokens = 0
		if self.task_context_manager and self.session_id:
			try:
				context = self.task_context_manager.get_context_injection(self.session_id)
				if context:
					# Rough estimate: words * 1.3 tokens per word
					hmem_tokens = int(len(context.split()) * 1.3)
			except Exception:
				pass

		total = base_count + ephemeral_tokens + hmem_tokens

		# Log if there's a significant difference from base count
		if ephemeral_tokens + hmem_tokens > 1000:
			self.logger.debug(
				f"Token count: base={base_count}, ephemeral={ephemeral_tokens}, "
				f"hmem={hmem_tokens}, total={total}"
			)

		return total

	def get_estimated_context_usage(self) -> float:
		"""Get estimated context usage as a ratio (0.0 to 1.0).

		Uses actual token count including H-MEM and ephemeral messages.
		"""
		if self.max_input_tokens <= 0:
			return 0.0
		total_tokens = self.get_actual_token_count()
		return min(1.0, total_tokens / self.max_input_tokens)

	def check_token_safety(self,
	                       additional_messages: Optional[List[BaseMessage]] = None,
	                       raise_on_overflow: bool = False) -> Dict[str, Any]:
		"""Check if current or projected token usage is safe.

		Uses actual token count including H-MEM and ephemeral messages.

		Args:
			additional_messages: Optional messages to add (for projection)
			raise_on_overflow: Raise LLMResponseError if unsafe

		Returns:
			{
				'safe': bool,
				'current_tokens': int,
				'estimated_tokens': int,
				'safe_limit': int,
				'max_limit': int,
				'would_overflow': bool,
				'usage_percent': float
			}

		Raises:
			LLMResponseError: If raise_on_overflow=True and unsafe
		"""
		from agents.task.robust_parse_config import RobustParseConfig
		from core.exceptions import LLMResponseError

		current = self.get_actual_token_count()

		# Estimate with additional messages
		if additional_messages:
			additional_tokens = self.estimate_tokens(additional_messages)
			estimated = current + additional_tokens
		else:
			estimated = current

		# Check against safe limit
		safe = estimated <= self.safe_input_tokens

		# Check context overflow (hard limit)
		would_overflow = RobustParseConfig.should_abort_context_overflow(
			estimated,
			self._model_name
		)

		if would_overflow:
			safe = False

		usage_percent = (estimated / self.max_input_tokens * 100) if self.max_input_tokens > 0 else 0

		result = {
			'safe': safe,
			'current_tokens': current,
			'estimated_tokens': estimated,
			'safe_limit': self.safe_input_tokens,
			'max_limit': self.max_input_tokens,
			'would_overflow': would_overflow,
			'usage_percent': usage_percent
		}

		# Optionally raise
		if raise_on_overflow and not safe:
			raise LLMResponseError(
				f"Token overflow: {estimated} tokens exceeds safe limit {self.safe_input_tokens} "
				f"({usage_percent:.1f}% of max {self.max_input_tokens})"
			)

		return result

	def get_token_stats(self) -> Dict[str, Any]:
		"""Get current token statistics.

		Uses actual token count including H-MEM and ephemeral messages.

		Returns:
			{
				'current': int,
				'base': int (without H-MEM/ephemeral),
				'max': int,
				'safe': int,
				'remaining': int,
				'usage_percent': float
			}
		"""
		base_count = (self.history.total_tokens + self._system_message_tokens
					+ self._initial_task_tokens + getattr(self, '_skill_message_tokens', 0)
					+ getattr(self, '_self_context_tokens', 0)
					+ getattr(self, '_project_context_tokens', 0)
					+ getattr(self, '_runtime_identity_tokens', 0))
		current = self.get_actual_token_count()
		remaining = self.safe_input_tokens - current
		usage_percent = (current / self.max_input_tokens * 100) if self.max_input_tokens > 0 else 0

		return {
			'current': current,
			'base': base_count,
			'max': self.max_input_tokens,
			'safe': self.safe_input_tokens,
			'remaining': remaining,
			'usage_percent': usage_percent
		}

	def _get_min_safe_tokens(self, model_name: str) -> int:
		"""Get minimum safe token limit for a model.

		This ensures we don't reduce tokens below a reasonable threshold for each model.
		"""
		# Try to get config from the centralized LLM module
		if LLM_MODULE_AVAILABLE:
			try:
				config = get_model_config(model_name)
				if config:
					return config.min_safe_tokens
			except Exception as e:
				self.logger.debug(f"Error getting min safe tokens from LLM module: {e}")

		# Fallback to hardcoded logic
		# For high-context models, use a larger minimum
		if any(name in model_name for name in ["gpt-4", "claude", "o1"]):
			# These models work well with at least 16k tokens
			return 16000
		elif "gpt-3.5" in model_name:
			# GPT-3.5 can handle at least 8k
			return 8000
		elif "llama" in model_name:
			# Llama models typically have smaller contexts
			return 4000
		else:
			# Conservative default for unknown models
			return 4000

	def get_context_usage_percent(self) -> float:
		"""Get current context usage as a percentage.

		Returns:
			Percentage of context window used (0-100)
		"""
		if self.max_input_tokens <= 0:
			return 0.0

		# Use get_actual_token_count for complete picture including H-MEM
		total_tokens = self.get_actual_token_count()
		usage_pct = (total_tokens / self.max_input_tokens) * 100
		return min(usage_pct, 100.0)
