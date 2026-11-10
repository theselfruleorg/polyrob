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
from agents.task.runtime.stop_reason import StopReason
from agents.task.telemetry.views import ProviderFailureEvent, ProviderFallbackSuccessEvent
from agents.task.utils import time_execution_async
from core.error_classifier import credit_death_in_chain
from core.env import bool_env as _bool_env
from core.exceptions import InsufficientCreditsError
from core.exceptions import (
    RateLimitError,
    LLMError,
    LLMAuthenticationError,
    LLMConfigError,
    LLMContextLengthError,
    LLMResponseError,
    LLMPermanentError,
    LLMProviderExhaustedError,
    LLMRefusalError,
    REFUSAL_HALT_PREFIX,
)


def _billing_failover_enabled() -> bool:
    """Whether billing errors should attempt provider fallback (default on, I-5).

    Note (proposal 008, provider-fallback goal resilience): on a
    single-funded-provider deployment there is nothing to fall back *to* — the flag
    being on is necessary but not sufficient for prod resilience until a second
    provider key is funded (owner decision, proposal 008 Lever 1).
    """
    return _bool_env("BILLING_FAILOVER_ENABLED", True)


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

	async def _trip_sentinel_if_credit_death(self, error: Exception) -> None:
		"""Trip the provider-credit sentinel iff this error is credit death by
		``credit_death_in_chain``, which reads only the LLM and credit error types.

		Pure side-effect (latch + ONE owner notice). Never raises, never decides
		control flow — the caller's branching is unaffected either way.
		"""
		matched = credit_death_in_chain(error)
		if matched is None:
			return
		try:
			from core.credit_sentinel import extract_reset_ts, trip_credit_sentinel
			# Scope the latch to the account that actually died. Without this a
			# dead secondary (a legacy cron pin, a stale fallback) pauses every
			# provider — including a flat-rate seat, where credit death cannot
			# happen at all.
			from agents.task.utils import resolve_serving_provider
			_provider = resolve_serving_provider(
				getattr(self, "llm", None), getattr(self, "model_name", None))
			await trip_credit_sentinel(
				matched[:300],
				provider=_provider,
				container=getattr(self, "container", None),
				user_id=getattr(self, "user_id", None),
				# Plan-quota death states its own reset time ("…limit will
				# reset at 2026-08-18 18:01:49"); latch until then instead of
				# re-tripping (and re-pinging the owner) every release window.
				release_ts=extract_reset_ts(matched))
		except Exception:
			self.logger.debug("credit sentinel trip failed (fail-open)", exc_info=True)

	async def _handle_step_error(self, error: Exception) -> list[ActionResult]:
		"""Decide what a failed step does. A permanent error halts. A transient error tries
		a fallback, then waits with backoff; the circuit breaker reports it when it stays."""
		include_trace = self.logger.isEnabledFor(logging.DEBUG)
		error_msg = AgentError.format_error(error, include_trace=include_trace)
		prefix = f'❌ Result failed {self.state.consecutive_failures + 1}/{self.max_failures} times:\n '
		error_type = type(error).__name__

		# 098 MO-08: a billed call that raised keeps its credit hold; the admin sees it now.
		from core.billing_context import record_orphaned_holds
		await record_orphaned_holds()

		# === CONTEXT-LENGTH OVERFLOW - COMPACT, DON'T FALLBACK (CO-F6) ===
		# A context-overflow needs a SMALLER prompt, not a different provider — retrying
		# on a fallback provider with the same oversized history just overflows again.
		# Must run BEFORE the generic is_llm_error block below (which would otherwise
		# route this typed error into a blind provider-fallback retry) AND before the
		# credit-death sentinel trip below (final whole-branch review, Finding 2): a
		# long session's overflow message can legitimately carry a token count that
		# contains "402" (e.g. "however you requested 130402 tokens"), which the
		# sentinel's bare-substring classifier would otherwise read as a billing
		# failure and pause autonomy for CREDIT_SENTINEL_RELEASE_HOURS over a session
		# that goes on to compact, retry, and complete fine.
		if isinstance(error, LLMContextLengthError):
			self.logger.warning("Context-length overflow — checkpoint + emergency prune, then retry")
			self.message_manager.checkpoint_history()
			self.message_manager.emergency_context_prune()
			self.state.consecutive_failures += 1
			return []  # retry next step with pruned history

		# === REFUSAL / CONTENT FILTER - TELL THE OWNER, NO RETRY, NO FALLBACK ===
		# The same request gets the same answer, and another provider must not answer
		# what this model declined. Not an account fault: never "check API configuration".
		if isinstance(error, LLMRefusalError):
			text = error.owner_text()
			self.logger.warning(f"LLM output stopped: {text}: {str(error)[:300]}")
			self.state.consecutive_failures = self.max_failures
			self.state.request_stop(StopReason.ERROR)
			return [ActionResult(
				error=f"{REFUSAL_HALT_PREFIX}{text}. Nothing was retried on another model.",
				include_in_memory=True
			)]

		# === CREDIT-DEATH SENTINEL — ONE universal trip site, BEFORE any branching ===
		# §6.3/Task 10. error_recovery is on every LLM path (chat, goals, cron,
		# sub-agents), so tripping here closes the interactive gap that let the
		# 2026-07-16 outage pass in silence — the owner's chat turn 402'd and nothing
		# tripped, because the only trip sites were cron/runner.py + goals/dispatcher.py
		# (both background-only).
		#
		# It MUST live here, above the `is_permanent` branch below, and be gated ONLY
		# on looks_like_credit_death (plus the exception-type gate in
		# `_trip_sentinel_if_credit_death` — see Finding 2 above) — never nested under
		# `is_permanent`: a billing failover that succeeds returns from that branch.
		#
		# Side-effect only: fail-open (a broken sentinel must never mask the original error) and
		# idempotent (single call site; the sentinel itself no-ops while the latch holds).
		await self._trip_sentinel_if_credit_death(error)

		# === PERMANENT/CRITICAL ERRORS - HALT IMMEDIATELY ===
		# These errors indicate account-level issues that won't be resolved by fallback
		credit_death = credit_death_in_chain(error)
		is_permanent = (
			isinstance(error, (LLMPermanentError, LLMAuthenticationError, LLMConfigError))
			or credit_death is not None
		)

		if is_permanent:
			# Purely a failover/halt decision: the sentinel tripped above.
			if credit_death is not None and _billing_failover_enabled():
				self.logger.warning("💳 Billing error with BILLING_FAILOVER_ENABLED — attempting provider fallback")
				# HIGH-2: record the exhausted provider BEFORE failover so repeated billing
				# errors accumulate the failed-set (prevents A↔B ping-pong / step-budget burn).
				# reset_llm_errors() below clears only error-type counts, not this set.
				self.state.track_llm_error("billing", self._current_llm_provider(self.model_name))
				if await self._attempt_llm_fallback_in_handler("billing"):
					self.logger.info("✅ Billing failover succeeded; continuing on fallback provider")
					self.state.reset_llm_errors()
					return []
				self.logger.error("❌ Billing failover found no alternative provider; halting")
			self.logger.error(f"❌ PERMANENT ERROR - Session halted: {error}")
			self.state.consecutive_failures = self.max_failures
			self.state.request_stop(StopReason.ERROR)
			return [ActionResult(
				error=f"PERMANENT ERROR: {str(error)[:400]}. Session halted. Please check API configuration.",
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
		if isinstance(error, LLMError):
			# Track error for circuit breaker
			current_provider = self._current_llm_provider(self.model_name)
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
					self.state.request_stop(StopReason.ERROR)
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

			# Backoff before retry with current provider (P4: shared formula).
			from core.backoff import jittered_exponential_delay
			delay = jittered_exponential_delay(
				self.retry_delay, self.state.consecutive_failures,
				multiplier=2, cap=120, cap_after_jitter=True,
			)
			self.logger.info(f'Waiting {delay:.1f}s before retry...')
			self._emit_retry_wait('llm_error', delay, provider=current_provider)
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
				self._emit_retry_wait('parse_error', delay)
				await asyncio.sleep(delay)

			self.state.consecutive_failures += 1

		elif isinstance(error, RateLimitError) or isinstance(error, ResourceExhausted):
			# Regular rate limit - can retry with fallback
			self.logger.warning(f'{prefix}{error_msg}')

			# Track for circuit breaker and try fallback
			current_provider = self._current_llm_provider(self.model_name)
			self.state.track_llm_error('RateLimitError', current_provider)

			if self.state.consecutive_failures >= 1:
				fallback_result = await self._attempt_llm_fallback_in_handler('RateLimitError')
				if fallback_result:
					self.logger.info(f"✅ Switched to fallback provider after rate limit")
					self.state.reset_llm_errors()
					return []

			# Enhanced rate limit handling with jitter (P4: shared formula).
			from core.backoff import jittered_exponential_delay
			delay = jittered_exponential_delay(
				self.retry_delay, self.state.consecutive_failures,
				multiplier=2, cap=120, cap_after_jitter=True,
			)
			self.logger.info(f'Rate limited - waiting {delay:.1f}s before retry (with jitter)')
			self._emit_retry_wait('rate_limit', delay, provider=current_provider)
			await asyncio.sleep(delay)
			self.state.consecutive_failures += 1

		elif isinstance(error, ConnectionError):
			self.logger.warning(f'{prefix}Connection error: {error_msg}')

			# Track for circuit breaker and try fallback
			current_provider = self._current_llm_provider(self.model_name)
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
			self._emit_retry_wait('connection_error', delay, provider=current_provider)
			await asyncio.sleep(delay)
			self.state.consecutive_failures += 1

		else:
			self.logger.error(f'{prefix}{error_msg}', exc_info=True)
			# For unknown errors, add a small delay to prevent rapid retries
			if self.state.consecutive_failures > 0:
				delay = min(2 * self.state.consecutive_failures, 10)
				self.logger.info(f'Unknown error - brief delay of {delay}s before retry')
				self._emit_retry_wait('unknown_error', delay)
				await asyncio.sleep(delay)
			self.state.consecutive_failures += 1

		return [ActionResult(error=error_msg, include_in_memory=True)]

	def _emit_retry_wait(self, reason: str, delay: float, provider: str = '') -> None:
		"""019 P1: emit a retry_wait feed event before a backoff sleep.

		Fail-open — surfaces show "retrying (rate_limit) 8s"
		instead of a silent stall while the handler sleeps.
		"""
		try:
			from agents.task.telemetry.views import RetryWaitEvent
			self.telemetry_manager.capture_event(RetryWaitEvent(
				agent_id=self.agent_id,
				step=self.state.n_steps,
				reason=reason,
				delay_sec=float(delay),
				attempt=int(self.state.consecutive_failures),
				provider=provider or '',
				session_id=self.session_id,
			))
		except Exception:
			pass

	async def _attempt_llm_fallback_in_handler(self, original_error_type: str) -> bool:
		"""Attempt to switch to a fallback LLM provider.

		Called from _handle_step_error when LLM errors occur.

		Args:
			original_error_type: The error type that triggered fallback

		Returns:
			True if fallback successful, False otherwise
		"""
		try:
			current_provider = self._current_llm_provider(self.model_name)
			# dict.fromkeys: dedupe while keeping order — the current provider is
			# usually already in llm_providers_failed, and the doubled entry
			# rendered as "excluding: ['openrouter', 'openrouter']" in prod logs.
			exclude_providers = list(dict.fromkeys(
				list(self.state.llm_providers_failed) + [current_provider]))

			self.logger.info(f"🔄 Attempting LLM fallback (excluding: {exclude_providers})")

			fallback_llm = await self._get_fallback_llm(
				exclude_providers=exclude_providers,
				original_model=self.model_name
			)

			if fallback_llm:
				original_model = self.model_name
				await self.adopt_fallback_llm(fallback_llm, original_error_type)

				# Emit telemetry
				self._emit_fallback_success_telemetry(
					original_provider=current_provider,
					original_model=original_model,
					fallback_provider=self.llm_provider,
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
					temperature=0.0,
					money_run=self._is_money_run()
				)
			else:
				self.logger.warning("LLMManager does not support get_fallback_chat_model")
				return None
				
		except Exception as e:
			self.logger.error(f"Error getting fallback LLM: {e}")
			return None

	def _is_money_run(self) -> bool:
		"""Money-class, by the run, the model or the toolset (112 F2: ONE predicate).

		The run is money-class when the runner stamped it so — ``is_money_class`` of its
		payload, carried as ``orchestrator._money_class`` — or it is on ``HEAVY_MODEL``,
		or it is autonomous and holds a money tool. An owner chat keeps its model choice."""
		try:
			if getattr(getattr(self, "orchestrator", None), "_money_class", False) is True:
				return True
			from agents.task.session_class import is_autonomous_session
			from core.money.classify import money_action
			from core.runtime_config import heavy_model_pin
			heavy = heavy_model_pin()
			if heavy and heavy == self.model_name:
				return True
			return bool(is_autonomous_session(getattr(self, "session_id", None))) and any(
				money_action(name) for name in self.controller.get_action_names())
		except Exception as e:
			self.logger.debug(f"money-run check failed: {e}")
			return False

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
