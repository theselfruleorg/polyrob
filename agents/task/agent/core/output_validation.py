"""Output-validation mixin (roadmap P9; code-motion from llm_runner.py).

Post-LLM output validation + the thin next_action / _get_llm_parameters helpers,
split out of LLMRunnerMixin. Agent composes OutputValidationMixin; call sites
(service.py _validate_output, step loop) unchanged via MRO.
"""
from __future__ import annotations

from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict

from agents.task.agent.views import AgentOutput, ActionResult
from agents.task.agent.prompts import AgentMessagePrompt
from agents.task.utils import time_execution_async
from modules.llm.messages import BaseMessage, HumanMessage, SystemMessage

# P0-6: hard bound on the judge call so a slow/hung judge can never stall the run
# loop past this (fail-open on timeout). Overridable for tests via monkeypatch.
VALIDATION_JUDGE_TIMEOUT_SEC: float = 60.0


class _Verdict:
	"""Minimal is_valid/reason carrier for the tolerant judge parser (P0-6).

	The judge's structured path returns a Pydantic ``ValidationResult`` (defined
	locally in ``_validate_output``); the manual path routes through this instead
	so the tolerant parser can live at module scope and be unit-tested directly.
	"""

	__slots__ = ("is_valid", "reason")

	def __init__(self, is_valid: bool, reason: str):
		self.is_valid = is_valid
		self.reason = reason


def _parse_validation_verdict(text: str) -> "_Verdict":
	"""Tolerantly extract an is_valid/reason verdict from a judge reply.

	Mirrors the ladder in ``agents/task/goals/completion_judge.py``: try the
	shared JSON extractor first; on a miss (a judge that narrates prose instead
	of JSON — the documented prod failure mode) fall back to a keyword scan for
	an explicit ``is_valid: false``. Defaults to VALID (pass) when the reply
	carries no clear negative verdict, keeping the judge fail-open: it only
	fails a turn on a verdict it can actually read as ``false``.
	"""
	import re
	from agents.task.utils_json import extract_json_from_model_output

	try:
		data = extract_json_from_model_output(text) or {}
		if isinstance(data, dict) and 'is_valid' in data:
			return _Verdict(
				is_valid=bool(data.get('is_valid', True)),
				reason=str(data.get('reason') or 'No reason provided'),
			)
	except Exception:
		pass

	# Keyword fallback: an explicit false verdict anywhere in the prose fails;
	# anything else passes (fail-open). Match `"is_valid": false`, `is_valid=false`,
	# `is_valid false`, tolerant of quotes/whitespace/casing.
	if re.search(r'is_valid["\']?\s*[:=]?\s*false', text, re.IGNORECASE):
		return _Verdict(is_valid=False, reason=text.strip()[:300] or 'Judge returned an invalid verdict')
	return _Verdict(is_valid=True, reason='No clear negative verdict; passing (fail-open)')


class OutputValidationMixin:
	"""LLM output validation + next_action wrapper for Agent."""

	def _get_llm_parameters(self) -> dict:
		"""Extract LLM parameters for logging purposes - delegates to MessageManager"""
		return self.message_manager.get_llm_parameters()

	async def _validate_output(self) -> bool:
		"""Validate the output of the last action is what the user wanted"""
		browser_context = await self.get_browser_context()
		has_browser = bool(browser_context and browser_context.session)

		# A2: task-type-neutral base prompt — this judge now runs for chat/coding/MCP
		# tasks too (the browser-only early-return was removed in T16), so the framing
		# must not assume a browser. Browser-specific guidance is appended ONLY when a
		# live browser session is present.
		system_msg = (
			f'You are a validator of an AI agent. '
			f'Validate if the output of the last action is what the user wanted and if the task is completed. '
			f'If the task is unclearly defined, you can let it pass. But if something is missing or the output does not satisfy the request, dont let it pass. '
		)
		if has_browser:
			system_msg += (
				f'The agent interacts with a browser; try to understand the page and help with '
				f'suggestions like scroll, do x, ... to get the solution right. '
			)
		system_msg += (
			f'Task to validate: {self.task}. Return a JSON object with 2 keys: is_valid and reason. '
			f'is_valid is a boolean that indicates if the output is correct. '
			f'reason is a string that explains why it is valid or not.'
			f' example: {{"is_valid": false, "reason": "The user wanted to search for "cat photos", but the agent searched for "dog photos" instead."}}'
		)

		if has_browser:
			state = await browser_context.get_state()
			content = AgentMessagePrompt(
				state=state,
				result=self._last_result,
				include_attributes=self.include_attributes,
				max_error_length=self.max_error_length,
			)
			msg = [SystemMessage(content=system_msg), content.get_user_message(self.use_vision)]
		else:
			# CO-F1: no live browser session (chat/coding/MCP/non-browser tasks) — judge
			# the final answer as text instead of skipping validation. Previously this
			# branch unconditionally returned True ("can't validate"), which made
			# validate_output a no-op for every non-browser task.
			result_text = "\n".join(
				(r.extracted_content or r.error or "")
				for r in (self._last_result or [])
				if (r.extracted_content or r.error)
			) or "(no output produced)"
			msg = [
				SystemMessage(content=system_msg),
				HumanMessage(content=f"Agent's final output:\n{result_text}"),
			]

		class ValidationResult(BaseModel):
			"""
			Validation results.
			"""
			is_valid: bool
			reason: str

			# FIXED: Add model_config to prevent additionalProperties schema error
			model_config = ConfigDict(extra='forbid')

		# CO-F1: lazily provision the cheap 'judge' aux model on first use, only when
		# validate_output is actually on (mirrors BackgroundReviewMixin's
		# provision-at-point-of-use pattern) — construction.py no longer provisions it
		# unconditionally. Fail-open to the main model; cached on self._judge_llm so
		# repeated validations in the same session don't re-resolve it.
		if getattr(self, "validate_output", False) and getattr(self, "_judge_llm", None) is None:
			try:
				# P2-9: await the async provisioning so building the judge client yields
				# the loop instead of blocking it (run_coroutine_sync) for up to 60s.
				self._judge_llm = await self._provision_aux_llm_async("judge")
			except Exception as e:
				self.logger.debug(f"judge aux model provisioning skipped: {e}")
				self._judge_llm = None

		# UP-10 2.1: route validation through the optional cheap 'judge' aux model when
		# provisioned (AUX_MODEL_JUDGE / AUX_AUTO); fail-open to the main model. Default
		# (no judge configured) is byte-identical to the legacy self.llm path.
		judge_llm = getattr(self, "_judge_llm", None) or self.llm

		# Structured output if the adapter supports it; native adapters generally
		# don't (browser-use legacy), so degrade to a plain call + JSON parse rather
		# than raising AttributeError. (native structured-output fallback, 2026-06.)
		validator = None
		if hasattr(judge_llm, 'with_structured_output'):
			try:
				validator = judge_llm.with_structured_output(ValidationResult, include_raw=True)
			except Exception as schema_error:
				self.logger.debug(f"Structured output unavailable for ValidationResult, using manual parse: {schema_error}")
				validator = None

		# P0-6: the judge is fail-OPEN end-to-end. An unparseable / erroring /
		# hanging judge reply must NOT raise out of the run loop and kill the turn
		# after the agent already called done() (the previous behaviour: a prose
		# judge reply let extract_json_from_model_output raise ValueError straight
		# through the bare `await self._validate_output()` at run_loop.py:459).
		# Fail-open covers judge FAILURES only — a well-formed {"is_valid": false}
		# verdict still fails validation (that's the feature). Mirrors the tolerant
		# ladder the goal judge already uses (agents/task/goals/completion_judge.py).
		import asyncio
		import time as _time
		_t0 = _time.time()
		_resp_for_meter = None
		try:
			if validator is not None:
				response: dict[str, Any] = await asyncio.wait_for(
					validator.ainvoke(msg), timeout=VALIDATION_JUDGE_TIMEOUT_SEC,
				)  # type: ignore
				_resp_for_meter = response  # extract_token_usage unwraps {'raw': ...}
				raw_parsed = response.get('parsed') if isinstance(response, dict) else None
				if raw_parsed is None:
					# LOW-1: structured output can yield {'parsed': None, 'raw': ...}
					# on a schema-mismatch — treat as a parse failure → fail-open.
					self.logger.warning(
						'⚠️ Judge structured output returned no parsed verdict; '
						'passing (fail-open).'
					)
					return True
				parsed = raw_parsed
			else:
				raw = await asyncio.wait_for(
					judge_llm.ainvoke(msg), timeout=VALIDATION_JUDGE_TIMEOUT_SEC,
				)
				_resp_for_meter = raw
				content = getattr(raw, 'content', raw)
				parsed = _parse_validation_verdict(
					content if isinstance(content, str) else str(content)
				)
		except asyncio.TimeoutError:
			self.logger.warning(
				f'⚠️ Judge validation timed out after {VALIDATION_JUDGE_TIMEOUT_SEC}s; '
				'passing (fail-open).'
			)
			return True
		except Exception as e:  # noqa: BLE001 — any judge failure is fail-open
			self.logger.warning(f'⚠️ Judge validation errored ({e!r}); passing (fail-open).')
			return True

		# A3: meter this aux LLM call through the single deduction path (fail-open).
		# Only reached on a successful invoke — a failed/timed-out call is never metered.
		from agents.task.agent.core.aux_metering import meter_aux_llm
		await meter_aux_llm(
			usage_tracker=getattr(self, "usage_tracker", None),
			user_id=getattr(self, "user_id", None),
			session_id=getattr(self, "session_id", ""),
			agent_id=getattr(self, "agent_id", "") or "",
			llm=judge_llm, response=_resp_for_meter, duration_seconds=_time.time() - _t0,
			component="judge", purpose="output_validation",
		)
		is_valid = parsed.is_valid
		if not is_valid:
			self.logger.info(f'❌ Validator decision: {parsed.reason}')
			msg = f'The output is not yet correct. {parsed.reason}.'
			self._last_result = [ActionResult(extracted_content=msg, include_in_memory=True)]
		else:
			self.logger.info(f'✅ Validator decision: {parsed.reason}')
		return is_valid

	async def next_action(self, state: BrowserState, result: Optional[List[ActionResult]] = None) -> AgentOutput:
			"""
			Get the next action from the agent based on current state.
			
			This is a simplified method that delegates to get_next_action for the actual LLM call
			while providing a cleaner interface for external usage.
			
			Args:
				state: Current browser state
				result: Optional previous action results
				
			Returns:
				AgentOutput with the next action to take
			"""
			import time
			
			# CRITICAL FIX: Apply base64 stripping to state before any token operations
			if hasattr(state, 'page_content') and state.page_content:
				from agents.task.robust_parse_config import RobustParseConfig
				state.page_content = RobustParseConfig.strip_base64_images(state.page_content)
			
			start_time = time.time()
			
			try:
				# Store the result for context
				if result:
					self._last_result = result
				
				# Add state and result to message context
				# FIX (Jan 2026): Conditionally include browser state
				include_browser = self._has_active_browser_usage()
				self.message_manager.add_state_message(
					state, result, None, self.use_vision,
					include_browser_state=include_browser
				)
				
				# Get messages for LLM call
				input_messages = self.message_manager.get_messages()
				
				# Call the main next action method - this will handle its own telemetry
				# so we don't send duplicate telemetry from this wrapper method
				agent_output = await self.get_next_action(input_messages)

				# P2-14: the LLM responded (even a parse-failed safe-default means the
				# request was delivered) — drop the one-shot ephemerals now.
				try:
					self.message_manager.commit_ephemeral_consumption()
				except Exception:
					pass

				duration = time.time() - start_time

				# Only log timing since get_next_action already sent telemetry
				self.logger.debug(f"next_action wrapper completed in {duration:.2f}s")

				return agent_output

			except Exception as e:
				duration = time.time() - start_time
				# P2-14: the LLM call ultimately failed — re-queue the ephemerals
				# (correspondent reply / RECALL) so the next step re-includes them
				# instead of losing them forever.
				try:
					self.message_manager.restore_ephemeral_on_failure()
				except Exception:
					pass
				# Log error but don't send duplicate telemetry
				self.logger.error(f"next_action failed after {duration:.2f}s: {e}", exc_info=True)
				raise


