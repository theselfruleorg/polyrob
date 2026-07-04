"""Error-recovery + provider-failover mixin (roadmap P9; code-motion from llm_runner.py).

Step-error handling, progress recovery, LLM provider fallback, and the
provider-failure / fallback-success telemetry. Split out of llm_runner.py so that
file owns LLM *invocation* and this one owns *error recovery / failover*. ``Agent``
composes ``ErrorRecoveryMixin``; call sites (step.py, get_next_action) unchanged
via MRO.
"""
from __future__ import annotations

import asyncio
import logging
import time
import traceback
from typing import Any, List, Optional

from pydantic import ValidationError

try:
    from google.api_core.exceptions import ResourceExhausted
except ImportError:  # google-api-core not installed
    class ResourceExhausted(Exception):
        pass

# NOTE: AgentError is imported from views (not core.exceptions) because only views'
# AgentError exposes format_error(), which _handle_step_error uses at its first line.
# core.exceptions.AgentError has no such method (importing it there crashes on any error).
from agents.task.agent.views import ActionResult, AgentError
from agents.task.telemetry.views import ProviderFailureEvent, ProviderFallbackSuccessEvent
from agents.task.utils import time_execution_async
from core.env import bool_env as _bool_env
from core.exceptions import InsufficientCreditsError
from core.exceptions import (
    RateLimitError,
    LLMError,
    LLMRateLimitError,
    LLMAuthenticationError,
    LLMConnectionError,
    LLMContextLengthError,
    LLMResponseError,
    LLMPermanentError,
    LLMProviderExhaustedError,
)


def _billing_failover_enabled() -> bool:
    """Whether billing errors should attempt provider fallback (default off)."""
    return _bool_env("BILLING_FAILOVER_ENABLED", False)


class ErrorRecoveryMixin:
	"""Step-error handling + LLM provider fallback + failover telemetry for Agent."""

	async def _recover_from_error(self, error: Exception) -> None:
		"""Comprehensive error recovery with state reset and resource cleanup.

		Args:
			error: The exception that triggered recovery
		"""
		self.logger.info("Starting comprehensive error recovery")

		# 1. Clear any pending operations
		if hasattr(self, '_pending_operations'):
			self._pending_operations = []

		# 2. Reset approval state if stuck
		# NOTE: Approval system simplified - no state tracking needed
		if hasattr(self, 'hitl_manager'):
			self.logger.debug("Approval system simplified - no state reset needed")

		# 3. Clean up browser state if browser-related error
		if 'browser' in str(error).lower() or 'playwright' in str(error).lower():
			browser_context = await self.get_browser_context()
			if browser_context:
				try:
					# Navigate to safe page
					pages = browser_context.pages
					if pages:
						await pages[0].goto('about:blank', timeout=5000)
						self.logger.info("Reset browser to blank page")
				except Exception as browser_error:
					self.logger.warning(f"Could not reset browser: {browser_error}")

		# 4. Clear any cached results that might be corrupt
		if hasattr(self, '_last_result'):
			self._last_result = None

		# 6. Reset consecutive failure counter if it's getting too high
		if self.state.consecutive_failures >= self.max_failures - 1:
			# Last chance - checkpoint and warn
			self.logger.warning("Near max failures - checkpointing state")
			self.message_manager.checkpoint_history()

			# Force garbage collection
			import gc
			gc.collect()

		# 7. Clear any tool-specific state
		if self.controller:
			# Reset any tool state that might be stuck
			if hasattr(self.controller, 'reset_tool_state'):
				try:
					await self.controller.reset_tool_state()
				except Exception:
					pass  # Best effort

		# 8. Log recovery completion
		self.logger.info("Error recovery completed")

	async def _handle_step_error(self, error: Exception) -> list[ActionResult]:
		"""Enhanced error handling with LLM fallback support (Dec 2025).

		UPGRADED: Now triggers LLM fallback for recoverable errors instead of
		just waiting and retrying with the same broken client.
		"""
		include_trace = self.logger.isEnabledFor(logging.DEBUG)
		error_msg = AgentError.format_error(error, include_trace=include_trace)
		prefix = f'❌ Result failed {self.state.consecutive_failures + 1}/{self.max_failures} times:\n '
		error_str_lower = str(error).lower()
		error_type = type(error).__name__

		# === CONTEXT-LENGTH OVERFLOW - COMPACT, DON'T FALLBACK (CO-F6) ===
		# A context-overflow needs a SMALLER prompt, not a different provider — retrying
		# on a fallback provider with the same oversized history just overflows again.
		# Must run BEFORE the generic is_llm_error block below (which would otherwise
		# route this typed error into a blind provider-fallback retry).
		if isinstance(error, LLMContextLengthError):
			self.logger.warning("Context-length overflow — checkpoint + emergency prune, then retry")
			self.message_manager.checkpoint_history()
			self.message_manager.emergency_context_prune()
			self.state.consecutive_failures += 1
			return []  # retry next step with pruned history

		# === PERMANENT/CRITICAL ERRORS - HALT IMMEDIATELY ===
		# These errors indicate account-level issues that won't be resolved by fallback
		is_permanent = (
			isinstance(error, LLMPermanentError) or
			isinstance(error, LLMAuthenticationError) or
			'insufficient_quota' in error_str_lower or
			'billing' in error_str_lower or
			'invalid_api_key' in error_str_lower or
			'account_deactivated' in error_str_lower
		)

		if is_permanent:
			import os
			is_billing = ('insufficient_quota' in error_str_lower or 'billing' in error_str_lower)
			if is_billing and _billing_failover_enabled():
				self.logger.warning("💳 Billing error with BILLING_FAILOVER_ENABLED — attempting provider fallback")
				# HIGH-2: record the exhausted provider BEFORE failover so repeated billing
				# errors accumulate the failed-set (prevents A↔B ping-pong / step-budget burn).
				# reset_llm_errors() below clears only error-type counts, not this set.
				self.state.track_llm_error("billing", self._get_provider_from_model(self.model_name))
				if await self._attempt_llm_fallback_in_handler("billing"):
					self.logger.info("✅ Billing failover succeeded; continuing on fallback provider")
					self.state.reset_llm_errors()
					return []
				self.logger.error("❌ Billing failover found no alternative provider; halting")
			self.logger.error(f"❌ PERMANENT ERROR - Session halted: {error}")
			self.state.consecutive_failures = self.max_failures
			self.state.stopped = True
			return [ActionResult(
				error=f"PERMANENT ERROR: {str(error)[:400]}. Session halted. Please check API configuration.",
				include_in_memory=True
			)]
		
		# === PROVIDER EXHAUSTED - All fallbacks failed ===
		if isinstance(error, LLMProviderExhaustedError):
			self.logger.error(f"❌ ALL LLM PROVIDERS EXHAUSTED: {error}")
			self.state.consecutive_failures = self.max_failures
			self.state.stopped = True
			providers = getattr(error, 'providers_tried', [])
			return [ActionResult(
				error=f"All LLM providers failed. Tried: {providers}. Session halted.",
				include_in_memory=True
			)]

		# CRITICAL: State recovery and cleanup (only for non-critical errors)
		try:
			await self._recover_from_error(error)
		except Exception as recovery_error:
			self.logger.error(f"Failed to recover from error: {recovery_error}", exc_info=True)

		# Enhanced error message creation with better context
		if not error_msg or error_msg.strip() == '':
			error_msg = f"Unknown error of type {error_type}"
			if hasattr(error, "__str__"):
				error_detail = str(error)
				if error_detail:
					error_msg += f": {error_detail}"

			# Add context information for debugging
			if hasattr(error, '__dict__'):
				error_context = {k: v for k, v in error.__dict__.items() if not k.startswith('_')}
				if error_context:
					error_msg += f"\nError context: {error_context}"

			if include_trace and hasattr(error, "__traceback__"):
				import traceback
				error_msg += f"\n\nTraceback:\n{traceback.format_tb(error.__traceback__)}"

		# === LLM ERRORS - TRY FALLBACK (Dec 2025 upgrade) ===
		is_llm_error = isinstance(error, (
			LLMError, LLMRateLimitError, LLMConnectionError, LLMContextLengthError
		)) or any(x in error_str_lower for x in ['rate_limit', 'rate limit', '429', 'connection', 'timeout', 'llm'])

		if is_llm_error:
			# Track error for circuit breaker
			current_provider = self._get_provider_from_model(self.model_name)
			circuit_tripped = self.state.track_llm_error(error_type, current_provider)

			if circuit_tripped:
				self.logger.error(f"🚨 CIRCUIT BREAKER TRIPPED: Same error {error_type} repeated {self.state.CIRCUIT_BREAKER_THRESHOLD}+ times")
				# Try one last fallback before giving up
				fallback_result = await self._attempt_llm_fallback_in_handler(error_type)
				if fallback_result:
					self.logger.info(f"✅ Circuit breaker: Fallback successful, continuing with new provider")
					self.state.reset_llm_errors(reset_circuit_breaker=True)
					return []  # Empty result = continue execution
				else:
					self.logger.error(f"❌ Circuit breaker: No fallback available, halting")
					self.state.stopped = True
					self.state.consecutive_failures = self.max_failures
					# Raise typed exception for proper handling upstream
					raise LLMProviderExhaustedError(
						f"All LLM providers exhausted after {error_type}",
						providers_tried=list(self.state.llm_providers_failed)
					)

			# First occurrence or not yet at threshold - try immediate fallback
			self.logger.warning(f'{prefix}LLM Error ({error_type}): {error_msg[:200]}')

			if self.state.consecutive_failures >= 1:  # Already failed once, try fallback
				fallback_result = await self._attempt_llm_fallback_in_handler(error_type)
				if fallback_result:
					self.logger.info(f"✅ Fallback successful after {error_type}")
					self.state.reset_llm_errors()
					return []  # Continue with new LLM

			# Backoff before retry with current provider
			import random
			base_delay = self.retry_delay * (2 ** self.state.consecutive_failures)
			jitter = random.uniform(0.8, 1.2)
			delay = min(base_delay * jitter, 120)
			self.logger.info(f'Waiting {delay:.1f}s before retry...')
			await asyncio.sleep(delay)
			self.state.consecutive_failures += 1

			return [ActionResult(error=error_msg, include_in_memory=True)]

		# === VALIDATION/PARSE ERRORS ===
		if isinstance(error, (ValidationError, ValueError)):
			self.logger.error(f'{prefix}{error_msg}', exc_info=True)
			if 'Max token limit reached' in error_msg or 'context length' in error_msg.lower():
				# Token limit exceeded - checkpoint and warn
				self.logger.warning(f'Token limit exceeded - consider checkpointing or using smaller context')
				self.message_manager.checkpoint_history()

			elif 'Could not parse response' in error_msg or 'json' in error_msg.lower():
				# Enhanced JSON parsing error recovery
				error_msg += '\n\nIMPORTANT: Return a valid JSON object with the required fields. Ensure all JSON is properly formatted and escaped.'

				# Try to recover by reducing complexity
				if hasattr(self, 'max_actions_per_step') and self.max_actions_per_step > 1:
					self.max_actions_per_step = max(1, self.max_actions_per_step - 1)
					self.logger.info(f'Reducing max actions per step to {self.max_actions_per_step} due to parse errors')

				# Smart backoff for parse errors
				delay = min(self.retry_delay * (1.5 ** self.state.consecutive_failures), 30)
				self.logger.info(f'Waiting {delay:.1f}s before retry due to parse error...')
				await asyncio.sleep(delay)

			self.state.consecutive_failures += 1

		elif isinstance(error, RateLimitError) or isinstance(error, ResourceExhausted):
			# IMPORTANT: Rate limits due to quota issues are critical
			if 'quota' in error_str_lower or 'billing' in error_str_lower:
				self.logger.error(f"❌ CRITICAL: Rate limit due to quota/billing issue")
				self.state.consecutive_failures = self.max_failures
				self.state.stopped = True
				return [ActionResult(
					error=f"CRITICAL: {str(error)[:400]}. Session halted due to quota/billing issue.",
					include_in_memory=True
				)]

			# Regular rate limit - can retry with fallback
			self.logger.warning(f'{prefix}{error_msg}')

			# Track for circuit breaker and try fallback
			current_provider = self._get_provider_from_model(self.model_name)
			self.state.track_llm_error('RateLimitError', current_provider)

			if self.state.consecutive_failures >= 1:
				fallback_result = await self._attempt_llm_fallback_in_handler('RateLimitError')
				if fallback_result:
					self.logger.info(f"✅ Switched to fallback provider after rate limit")
					self.state.reset_llm_errors()
					return []

			# Enhanced rate limit handling with jitter
			import random
			base_delay = self.retry_delay * (2 ** self.state.consecutive_failures)
			jitter = random.uniform(0.8, 1.2)
			delay = min(base_delay * jitter, 120)
			self.logger.info(f'Rate limited - waiting {delay:.1f}s before retry (with jitter)')
			await asyncio.sleep(delay)
			self.state.consecutive_failures += 1

		elif isinstance(error, ConnectionError) or 'connection' in error_msg.lower():
			self.logger.warning(f'{prefix}Connection error: {error_msg}')

			# Track for circuit breaker and try fallback
			current_provider = self._get_provider_from_model(self.model_name)
			self.state.track_llm_error('ConnectionError', current_provider)

			if self.state.consecutive_failures >= 1:
				fallback_result = await self._attempt_llm_fallback_in_handler('ConnectionError')
				if fallback_result:
					self.logger.info(f"✅ Switched to fallback provider after connection error")
					self.state.reset_llm_errors()
					return []

			# Progressive backoff for connection errors
			delay = min(5 * (1.5 ** self.state.consecutive_failures), 60)
			self.logger.info(f'Connection error - waiting {delay:.1f}s before retry')
			await asyncio.sleep(delay)
			self.state.consecutive_failures += 1

		else:
			self.logger.error(f'{prefix}{error_msg}', exc_info=True)
			# For unknown errors, add a small delay to prevent rapid retries
			if self.state.consecutive_failures > 0:
				delay = min(2 * self.state.consecutive_failures, 10)
				self.logger.info(f'Unknown error - brief delay of {delay}s before retry')
				await asyncio.sleep(delay)
			self.state.consecutive_failures += 1

		return [ActionResult(error=error_msg, include_in_memory=True)]

	async def _attempt_llm_fallback_in_handler(self, original_error_type: str) -> bool:
		"""Attempt to switch to a fallback LLM provider.

		Called from _handle_step_error when LLM errors occur.

		Args:
			original_error_type: The error type that triggered fallback

		Returns:
			True if fallback successful, False otherwise
		"""
		try:
			current_provider = self._get_provider_from_model(self.model_name)
			exclude_providers = list(self.state.llm_providers_failed) + [current_provider]

			self.logger.info(f"🔄 Attempting LLM fallback (excluding: {exclude_providers})")

			fallback_llm = await self._get_fallback_llm(
				exclude_providers=exclude_providers,
				original_model=self.model_name
			)

			if fallback_llm:
				# Store original for telemetry
				original_model = self.model_name

				# Switch to fallback
				self.llm = fallback_llm
				self.model_name = getattr(fallback_llm, 'model_name', 'fallback')

				self.logger.info(f"✅ Switched LLM: {original_model} → {self.model_name}")

				# Emit telemetry
				self._emit_fallback_success_telemetry(
					original_provider=current_provider,
					original_model=original_model,
					fallback_provider=self._get_provider_from_model(self.model_name),
					fallback_model=self.model_name,
					original_error_type=original_error_type,
					total_attempts=len(self.state.llm_providers_failed) + 1,
					total_time=0.0
				)

				return True
			else:
				self.logger.warning(f"❌ No fallback LLM available")
				return False

		except Exception as e:
			self.logger.error(f"Fallback attempt failed: {e}")
			return False

	async def _get_fallback_llm(
		self,
		exclude_providers: list,
		original_model: str
	) -> Optional[Any]:
		"""Get a fallback LLM from LLMManager.
		
		Args:
			exclude_providers: List of providers to exclude (already failed)
			original_model: Original model name for context
			
		Returns:
			Chat model or None if no fallback available
		"""
		try:
			llm_manager = self.container.get_service('llm') if self.container else None
			if not llm_manager:
				self.logger.warning("LLMManager not available - cannot get fallback")
				return None
			
			# Use LLMManager's fallback method
			if hasattr(llm_manager, 'get_fallback_chat_model'):
				return await llm_manager.get_fallback_chat_model(
					exclude_providers=exclude_providers,
					original_model=original_model,
					temperature=0.0
				)
			else:
				self.logger.warning("LLMManager does not support get_fallback_chat_model")
				return None
				
		except Exception as e:
			self.logger.error(f"Error getting fallback LLM: {e}")
			return None

	def _emit_provider_failure_telemetry(
		self,
		failed_provider: str,
		failed_model: str,
		error_type: str,
		error_message: str,
		attempt_number: int
	) -> None:
		"""Emit telemetry event for provider failure."""
		try:
			if self.telemetry_manager:
				event = ProviderFailureEvent(
					failed_provider=failed_provider,
					failed_model=failed_model,
					error_type=error_type,
					error_message=error_message[:500],
					attempt_number=attempt_number,
					session_id=self.session_id,
					agent_id=self.agent_id,
					step=self.state.n_steps if hasattr(self, 'state') else None
				)
				self.telemetry_manager.capture_event(event)
		except Exception as e:
			self.logger.debug(f"Failed to emit provider failure telemetry: {e}")

	def _emit_fallback_success_telemetry(
		self,
		original_provider: str,
		original_model: str,
		fallback_provider: str,
		fallback_model: str,
		original_error_type: str,
		total_attempts: int,
		total_time: float
	) -> None:
		"""Emit telemetry event for successful fallback."""
		try:
			if self.telemetry_manager:
				event = ProviderFallbackSuccessEvent(
					original_provider=original_provider,
					original_model=original_model,
					fallback_provider=fallback_provider,
					fallback_model=fallback_model,
					original_error_type=original_error_type,
					total_attempts=total_attempts,
					total_fallback_time_seconds=total_time,
					session_id=self.session_id,
					agent_id=self.agent_id,
					step=self.state.n_steps if hasattr(self, 'state') else None
				)
				self.telemetry_manager.capture_event(event)
		except Exception as e:
			self.logger.debug(f"Failed to emit fallback success telemetry: {e}")

