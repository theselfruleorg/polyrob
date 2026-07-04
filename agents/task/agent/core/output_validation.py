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
				self._judge_llm = self._provision_aux_llm("judge")
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

		import time as _time
		_t0 = _time.time()
		if validator is not None:
			response: dict[str, Any] = await validator.ainvoke(msg)  # type: ignore
			_resp_for_meter = response  # extract_token_usage unwraps {'raw': ...} structured output
			parsed: ValidationResult = response['parsed']
		else:
			from agents.task.utils_json import extract_json_from_model_output
			raw = await judge_llm.ainvoke(msg)
			_resp_for_meter = raw
			content = getattr(raw, 'content', raw)
			data = extract_json_from_model_output(content if isinstance(content, str) else str(content)) or {}
			parsed = ValidationResult(
				is_valid=bool(data.get('is_valid', True)),
				reason=str(data.get('reason') or 'No reason provided'),
			)
		# A3: meter this aux LLM call through the single deduction path (fail-open).
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
				
				duration = time.time() - start_time
				
				# Only log timing since get_next_action already sent telemetry
				self.logger.debug(f"next_action wrapper completed in {duration:.2f}s")
				
				return agent_output
				
			except Exception as e:
				duration = time.time() - start_time
				
				# Log error but don't send duplicate telemetry 
				self.logger.error(f"next_action failed after {duration:.2f}s: {e}", exc_info=True)
				raise


