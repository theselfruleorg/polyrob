"""ExecutionMixin — Controller hot path (UP-11 god-file split, verbatim code-motion).

multi_act / act + their execution-support helpers. Methods keep their decorators and
exact signatures; behaviour is identical to the pre-split inline versions.
"""
import asyncio
import hashlib
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Type, Union

from tools.controller.registry.views import ActionModel
from tools.controller.types import ActionResult
from tools.controller.execution_context import ActionExecutionContext
from tools.browser.context import BrowserContext
from agents.task.utils import time_execution_async, time_execution_sync
from modules.llm.adapters import BaseChatModel
from modules.llm.messages import AIMessage
from tools.controller._helpers import observe


def _dedup_action_error(action_name: str, e: Exception, tb: str) -> str:
    """Build the action-failure message without doubling the prefix.

    ``registry.execute_action`` already raises ``Error executing action {name}: …``,
    so ``str(e)`` usually carries that prefix; re-prefixing here produced
    ``Error executing action X: Error executing action X: …`` in both the log and
    the ``ActionResult.error`` the agent reads back from its own memory.
    """
    detail = str(e)
    dup = f"Error executing action {action_name}: "
    if detail.startswith(dup):
        detail = detail[len(dup):]
    return f"Error executing action {action_name}: {detail}\n{tb}"


class ExecutionMixin:
	def _emit_governance_event(self, kind, execution_context=None, **attrs):
		"""Record a governance event (tool_denied/tool_timeout) to the durable event
		log. The governance surface was logged but never telemetered (audit
		2026-07-04). Fail-open — telemetry must never break tool execution."""
		try:
			from agents.task.telemetry.event_log import get_event_log, event_log_enabled
			if not event_log_enabled():
				return
			uid = getattr(execution_context, "user_id", None) or ""
			sid = getattr(execution_context, "session_id", "") or ""
			get_event_log().record(kind, user_id=uid, session_id=sid,
									source="tool", **attrs)
		except Exception:
			pass

	@observe(name='controller.multi_act')
	@time_execution_async('--multi-act')
	async def multi_act(
		self,
		actions: list[ActionModel],
		execution_context: Optional[ActionExecutionContext] = None,
		# Legacy parameters for backward compatibility
		browser_context: Optional[BrowserContext] = None,
		check_break_if_paused: Optional[Callable[[], bool]] = None,
		check_for_new_elements: bool = True,
		_page_extraction_llm: Optional[BaseChatModel] = None,
		sensitive_data: Optional[Dict[str, str]] = None,
		available_file_paths: Optional[list[str]] = None,
	) -> list[ActionResult]:
		"""Execute multiple actions with enhanced error handling"""
		results = []

		# Check if any actions require browser context
		browser_required = False
		for action in actions:
			action_dump = action.model_dump(exclude_unset=True)
			if action_dump:
				action_name = list(action_dump.keys())[0]
				# Get the registered action to check the tool
				registered_action = self.registry.get_action(action_name)
				if registered_action and registered_action.tool == 'browser':
					browser_required = True
					break

		# Build execution context from parameters (backward compatibility)
		if not execution_context:
			# Only require browser_context if browser actions are present
			if browser_required and not browser_context:
				raise ValueError("browser_context is required for browser actions")
			execution_context = ActionExecutionContext(
				browser_context=browser_context,  # Can be None for non-browser actions
				session_id=self.session_id,
				user_id=getattr(self, 'user_id', None),
				workspace_dir=getattr(self, 'workspace_dir', None),
				available_file_paths=available_file_paths or [],
				sensitive_data=sensitive_data or {},
			)
		else:
			# Use provided context, potentially override with explicit parameters
			if browser_context:
				execution_context.browser_context = browser_context
			if sensitive_data:
				execution_context.sensitive_data = sensitive_data
			if available_file_paths:
				execution_context.available_file_paths = available_file_paths

		# Validate browser context is available if required
		if browser_required and not execution_context.browser_context:
			raise ValueError("browser_context is required in execution_context for browser actions")

		if not check_break_if_paused:
			check_break_if_paused = lambda: None  # Default no-op

		# Browser-specific setup only if browser context is available
		cached_path_hashes = set()
		if execution_context.browser_context:
			try:
				session = await execution_context.browser_context.get_session()
				cached_selector_map = session.cached_state.selector_map

				# Create a defensive copy of the selector map values
				for e in cached_selector_map.values():
					try:
						if hasattr(e, 'hash') and hasattr(e.hash, 'branch_path_hash'):
							cached_path_hashes.add(e.hash.branch_path_hash)
					except (AttributeError, NotImplementedError) as e:
						self.logger.debug(f"Error accessing hash attribute: {e}")
						# Continue without the hash - this is best-effort

				check_break_if_paused()
				await execution_context.browser_context.remove_highlights()
			except Exception as e:
				self.logger.warning(f"Browser context setup failed: {e}")

		try:
			action_count = len(actions)
			self.logger.info(f"Executing {action_count} actions")

			# NOTE: MCP action throttling is handled by Agent.step() BEFORE calling multi_act()
			# Agent uses MAX_MCP_PER_STEP from agents.task.constants to limit expensive MCP operations
			# See agents/task/agent/service.py for throttling logic

			for i, action in enumerate(actions):
				try:
					check_break_if_paused()

					# Log the action being executed with parameters
					action_dump = action.model_dump(exclude_unset=True)
					if not action_dump:
						# Try to get action name from the model even if dump is empty
						action_name = "unknown"
						if hasattr(action, '__dict__'):
							for key in action.__dict__:
								if not key.startswith('_'):
									action_name = key
									break

						self.logger.warning(f"Action {i+1}/{action_count} ({action_name}) has no data, skipping")
						results.append(ActionResult(
							error=f"Empty action received ({i+1}/{action_count}, action={action_name})",
							include_in_memory=True,
							tool_call_id=getattr(action, "_tool_call_id", None)
						))
						continue
						
					action_type = list(action_dump.keys())[0] if action_dump.keys() else "unknown_action"
					action_params = action_dump.get(action_type, {})

					# pre_tool_call hooks: allow a hook to veto this action before execution.
					deny_reason = await self._run_pre_tool_call_hooks(action_type, action_params, execution_context)
					if deny_reason:
						self.logger.warning(f"⛔ Action '{action_type}' blocked by pre_tool_call hook: {deny_reason}")
						self._emit_governance_event("tool_denied", execution_context,
													action=action_type, reason=str(deny_reason)[:200])
						results.append(ActionResult(
							error=f"Action '{action_type}' blocked: {deny_reason}",
							include_in_memory=True,
							tool_call_id=getattr(action, "_tool_call_id", None)
						))
						continue

					self.logger.debug(f"Executing action {i+1}/{action_count}: {action_type} - Parameters: {action_params}")
					
					# Log action start for visibility (first 3 actions only to avoid spam)
					if i < 3:
						self.logger.debug(f"Starting action {i+1}/{action_count}: {action_type}")

					action_index = None
					try:
						# Get action index with error handling
						action_index = action.get_index()
					except (AttributeError, NotImplementedError) as e:
						self.logger.debug(f"Error getting action index: {e}")
						# Continue with action_index = None

					# Only check for new elements if we have browser context
					if action_index is not None and i != 0 and check_for_new_elements and execution_context.browser_context:
						try:
							new_state = await execution_context.browser_context.get_state()
							new_path_hashes = set()

							# Safely collect the new path hashes
							for e in new_state.selector_map.values():
								try:
									if hasattr(e, 'hash') and hasattr(e.hash, 'branch_path_hash'):
										new_path_hashes.add(e.hash.branch_path_hash)
								except (AttributeError, NotImplementedError) as e:
									self.logger.debug(f"Error accessing hash attribute: {e}")
									continue

							if not new_path_hashes.issubset(cached_path_hashes):
								msg = f'Something new appeared after action {i} / {action_count}'
								self.logger.info(msg)
								results.append(ActionResult(extracted_content=msg, include_in_memory=True, tool_call_id=getattr(action, "_tool_call_id", None)))
								break
						except Exception as e:
							self.logger.debug(f"Error checking for new elements: {e}")
							# Continue with execution rather than breaking

					check_break_if_paused()

					# Execute the action with comprehensive error handling and timeout
					import asyncio
					from agents.task.constants import TimeoutConfig

					# Get tool-specific timeout from centralized config
					reg_act = self.registry.get_action(action_type)
					tool_name = reg_act.tool if reg_act and reg_act.tool else 'default'
					
					# Special handling for sub-agent actions (they need much longer timeouts)
					# UP-10 2.4: delegate_task is the consolidated delegation verb and must
					# also get the sub-agent timeout (previously omitted -> sync delegate_task
					# wrongly got the 'default' timeout).
					if action_type in ('subtask', 'parallel_subtasks', 'delegate_task'):
						action_timeout = TimeoutConfig.PARALLEL_SUBTASKS_TIMEOUT
						self.logger.info(f"🤖 Sub-agent action '{action_type}' - timeout: {action_timeout}s")
					else:
						action_timeout = TimeoutConfig.get_tool_timeout(tool_name)

					if tool_name == 'mcp':
						self.logger.info(f"🌐 MCP action '{action_type}' - timeout: {action_timeout}s")
					elif tool_name == 'browser':
						self.logger.debug(f"🌐 Browser action '{action_type}' - timeout: {action_timeout}s")

					# 019 P0: span-start — announce the tool the moment it is
					# DISPATCHED (the completion event alone leaves a long tool
					# invisible until it returns). The span id joins start to
					# completion: the LLM tool-call id when present, else a
					# synthesized per-batch id stamped on the action (a separate
					# private attr — never write _tool_call_id, which drives
					# message-sequence pairing).
					span_id = getattr(action, "_tool_call_id", None)
					if not span_id:
						_step = getattr(self.orchestrator, 'current_step', 0) if self.orchestrator else 0
						span_id = f"s{_step}i{i}"
						try:
							action._run_span_id = span_id
						except Exception:
							span_id = None
					self._capture_tool_started(
						action_name=action_type,
						tool_name=tool_name,
						params=action_params,
						call_id=span_id,
						index=i,
						total_in_batch=action_count,
					)

					result = await asyncio.wait_for(
						self.act(
							action,
							execution_context
						),
						timeout=action_timeout
					)
					# P2 hooks: transform may rewrite the result; post observes the final value.
					result = await self._run_transform_tool_result_hooks(action_type, action_params, result, execution_context)
					await self._run_post_tool_call_hooks(action_type, action_params, result, execution_context)
					result.tool_call_id = getattr(action, "_tool_call_id", None)
					results.append(result)

					# Log the result with detailed information
					if result.error:
						self.logger.warning(f"Action {i+1}/{action_count} ({action_type}) failed: {result.error}")
					elif result.is_done:
						self.logger.info(f"Action {i+1}/{action_count} ({action_type}) completed task")
						self.logger.debug(f"Done result: {result.extracted_content}")
					elif result.extracted_content:
						self.logger.debug(f"Action {i+1}/{action_count} ({action_type}) completed with result: {result.extracted_content}")

				except asyncio.TimeoutError:
					# Handle action timeout
					self.logger.error(f"⏱️ Action {i+1}/{action_count} ({action_type}) timed out after {action_timeout} seconds")
					self._emit_governance_event("tool_timeout", execution_context,
												action=action_type, timeout_s=action_timeout)
					err = ActionResult(
						error=f"Action {i+1}/{action_count} ({action_type}) timed out after {action_timeout} seconds",
						include_in_memory=True,
						tool_call_id=getattr(action, "_tool_call_id", None)
					)
					results.append(await self._observe_error_result(action_type, action_params, err, execution_context))
					# Log timeout for visibility
					if i < 3:
						self.logger.debug(f"Action {i+1} timed out after {action_timeout}s")
				except NotImplementedError as e:
					# For NotImplementedError, create a helpful error result but continue
					self.logger.warning(f"NotImplementedError in action {i+1}/{action_count}: {e}")
					err = ActionResult(
						error=f"Action {i+1}/{action_count} not fully implemented: {str(e)}",
						include_in_memory=True,
						tool_call_id=getattr(action, "_tool_call_id", None)
					)
					results.append(await self._observe_error_result(action_type, action_params, err, execution_context))
				except Exception as e:
					# For other exceptions, log but continue
					self.logger.warning(f"Error executing action {i+1}/{action_count}: {e}")
					err = ActionResult(
						error=f"Error in action {i+1}/{action_count}: {str(e)}",
						include_in_memory=True,
						tool_call_id=getattr(action, "_tool_call_id", None)
					)
					results.append(await self._observe_error_result(action_type, action_params, err, execution_context))

				# FIX (Dec 2025): Only break on is_done, NOT on error
				# Previously: breaking on ANY error stopped subsequent independent actions
				# This caused "No result available" for actions that never executed
				# 
				# New behavior:
				# - is_done: Stop execution (task completed)
				# - error: Log but CONTINUE with remaining actions (they may be independent)
				# - last action: Stop (nothing left to do)
				if results[-1].is_done:
					self.logger.info(f"Action {i+1}/{action_count} completed task - stopping execution")
					break
				
				# Log errors but continue - subsequent actions may be independent
				if results[-1].error:
					self.logger.warning(
						f"Action {i+1}/{action_count} failed but continuing with remaining actions. "
						f"Error: {results[-1].error[:100]}..."
					)
				
				# Stop if this was the last action
				if i == action_count - 1:
					break

				# Only wait between actions if browser context is configured
				if execution_context.browser_context and hasattr(execution_context.browser_context, 'config'):
					await asyncio.sleep(execution_context.browser_context.config.wait_between_actions)
				else:
					# Small default delay for non-browser actions
					await asyncio.sleep(0.1)

			self.logger.info(f"Completed execution of {len(results)}/{action_count} actions")
			return results
		except Exception as e:
			# Handle any exceptions that escape the main loop
			self.logger.error(f"Critical error in multi_act: {e}")
			if not results:
				# If no results yet, create one with the error
				results.append(ActionResult(
					error=f"Error executing actions: {str(e)}",
					include_in_memory=True
				))
			return results

	async def _observe_error_result(self, action_type, action_params, result, execution_context):
		"""Run the transform + post tool-call hooks on an error/timeout result, mirroring
		the success path in multi_act, so audit/billing/metrics hooks (and a fail-closed
		post hook) observe EVERY tool invocation — not just successful ones.

		H6: guarded. A fail-closed hook that raises HERE (on the already-failed action's
		error result) must not escape and unwind the whole multi_act loop — that would
		drop this action's result and silently skip every remaining queued action,
		breaking the tool_call<->result pairing invariant. The action already failed and
		is being recorded, so a raising hook is logged and swallowed."""
		try:
			result = await self._run_transform_tool_result_hooks(action_type, action_params, result, execution_context)
			await self._run_post_tool_call_hooks(action_type, action_params, result, execution_context)
		except Exception as hook_error:
			self.logger.error(
				f"hook.error during error-result observation for {action_type}: {hook_error}"
			)
		return result

	@time_execution_sync('--act')
	async def act(
		self,
		action: ActionModel,
		execution_context: Optional[ActionExecutionContext] = None,
		# Legacy parameters for backward compatibility
		browser_context: Optional[BrowserContext] = None,
		page_extraction_llm: Optional[BaseChatModel] = None,
		sensitive_data: Optional[Dict[str, str]] = None,
		available_file_paths: Optional[list[str]] = None,
	) -> ActionResult:
		"""Execute an action with improved error handling and telemetry"""
		import time

		try:
			action_data = action.model_dump(exclude_unset=True)

			# Filter out None values to find actual action
			action_data = {k: v for k, v in action_data.items() if v is not None}

			# Handle empty action data
			if not action_data:
				error_msg = "Empty action data received, cannot execute"
				self.logger.warning(error_msg)
				return ActionResult(error=error_msg, include_in_memory=True)

			for action_name, params in action_data.items():
				# ARCHITECTURE FIX: Lookup action ONCE at the start to get metadata
				# This eliminates the insane pattern of looking it up 3 times:
				# 1. In registry.execute_action() to execute
				# 2. In _detect_tool_from_action() to get tool
				# 3. In registry.get_action() inside _detect_tool_from_action()
				registered_action = self.registry.get_action(action_name)
				tool_name = registered_action.tool if registered_action and registered_action.tool else 'default'
				
				# Track execution time for telemetry
				start_time = time.time()
				# Check retry limit to prevent infinite loops
				operation_key = self._get_operation_key(action_name, params)
				attempt_count = self._operation_attempts.get(operation_key, 0)

				# Get tool-specific retry limit or default
				max_retries = self._get_retry_limit_for_tool(tool_name)

				if attempt_count >= max_retries:
					error_msg = (
						f"⚠️ Maximum retries ({max_retries}) exceeded for {action_name}. "
						f"This operation has failed {attempt_count} times. "
						f"Try a different approach or parameters."
					)
					self.logger.error(error_msg)
					return ActionResult(
						error=error_msg,
						include_in_memory=True
					)

				# Increment attempt counter
				self._operation_attempts[operation_key] = attempt_count + 1

				try:
					laminar_available = False
					try:
						from laminar import Laminar
						laminar_available = True
					except ImportError:
						pass
						
					if laminar_available:
						with Laminar.start_as_current_span(
							name=action_name,
							input={
								'action': action_name,
								'params': params,
							},
							span_type='TOOL',
						) as span:
							try:
								# Execute the action through Registry (handles all tools uniformly)
								result = await self.registry.execute_action(
									action_name,
									params,
									execution_context=execution_context,
									# Also pass legacy params for backward compatibility
									browser=execution_context.browser_context,
									page_extraction_llm=page_extraction_llm,
									sensitive_data=execution_context.sensitive_data,
									available_file_paths=execution_context.available_file_paths,
									session_id=execution_context.session_id,
								)
								
								# Handle case where result might be another coroutine
								import inspect
								if inspect.iscoroutine(result):
									result = await result
								
								# Set the span output for telemetry
								Laminar.set_span_output(result)

								# Reset retry counter only on GENUINE success. An action that
								# fails by RETURNING ActionResult(error=...) (the dominant
								# convention — most tools don't raise) must still count toward
								# max_retries, else the guard never fires and it retries unbounded.
								if operation_key in self._operation_attempts and not getattr(result, "error", None):
									self._operation_attempts[operation_key] = 0

								# Calculate execution time
								duration = time.time() - start_time

								# Convert result to ActionResult
								action_result = None
								if isinstance(result, str):
									action_result = ActionResult(extracted_content=result)
								elif isinstance(result, ActionResult):
									action_result = result
								elif result is None:
									action_result = ActionResult()
								elif isinstance(result, dict) and "is_done" in result:
									action_result = ActionResult(
										extracted_content=result.get("content", ""),
										is_done=result["is_done"],
										include_in_memory=True
									)
								else:
									action_result = ActionResult(extracted_content=str(result))

								# Capture tool execution telemetry
								self._capture_tool_telemetry(
									action_name=action_name,
									tool_name=tool_name,  # ← Pass tool directly (no lookup needed)
									params=params,
									duration=duration,
									success=True,
									result=action_result,
									execution_context=execution_context,
									call_id=getattr(action, "_tool_call_id", None) or getattr(action, "_run_span_id", None)
								)

								return action_result
								
							except NotImplementedError as e:
								# Special handling for NotImplementedError
								duration = time.time() - start_time
								error_msg = f"Action {action_name} not fully implemented: {str(e)}"
								self.logger.error(error_msg)
								
								# Create a detailed error message with action info
								detailed_error = f"""
NotImplementedError in action {action_name}:
Parameters: {params}
Error: {str(e)}

This usually indicates a method was called that's not fully implemented.
Check the controller registry and action implementations.
"""
								# Add error details to span for telemetry
								if span is not None:
									span.set_status(span.Status.ERROR)
									span.record_exception(e)

								# Capture tool execution telemetry for error
								action_result = ActionResult(error=detailed_error, include_in_memory=True)
								self._capture_tool_telemetry(
									action_name=action_name,
									tool_name=tool_name,  # ← Pass tool directly (no lookup needed)
									params=params,
									duration=duration,
									success=False,
									result=action_result,
									execution_context=execution_context,
									error=str(e),
									call_id=getattr(action, "_tool_call_id", None) or getattr(action, "_run_span_id", None)
								)

								# Return informative action result
								return action_result

							except Exception as e:
								# Enhanced general exception handling
								import traceback
								duration = time.time() - start_time
								error_msg = _dedup_action_error(action_name, e, traceback.format_exc())
								self.logger.error(error_msg)
								
								# Add error details to span for telemetry
								if span is not None:
									span.set_status(span.Status.ERROR)
									span.record_exception(e)

								# Capture tool execution telemetry for error
								action_result = ActionResult(error=error_msg, include_in_memory=True)
								self._capture_tool_telemetry(
									action_name=action_name,
									tool_name=tool_name,  # ← Pass tool directly (no lookup needed)
									params=params,
									duration=duration,
									success=False,
									result=action_result,
									execution_context=execution_context,
									error=str(e),
									call_id=getattr(action, "_tool_call_id", None) or getattr(action, "_run_span_id", None)
								)
								
								# Return informative action result
								return action_result
					else:
						# If Laminar not available, run without span tracking
						if execution_context:
							# Use execution context if available
							result = await self.registry.execute_action(
								action_name,
								params,
								execution_context=execution_context,
								# Also pass legacy params for backward compatibility
								browser=execution_context.browser_context,
								page_extraction_llm=page_extraction_llm,
								sensitive_data=execution_context.sensitive_data,
								available_file_paths=execution_context.available_file_paths,
								session_id=execution_context.session_id,
							)
						else:
							# Fall back to legacy parameters
							result = await self.registry.execute_action(
								action_name,
								params,
								browser=browser_context,
								page_extraction_llm=page_extraction_llm,
								sensitive_data=sensitive_data,
								available_file_paths=available_file_paths,
								session_id=self.session_id,
							)
						
						# Handle case where result might be another coroutine
						import inspect
						if inspect.iscoroutine(result):
							result = await result

						# Reset retry counter only on GENUINE success — an action that fails
						# by RETURNING ActionResult(error=...) must still count toward
						# max_retries (see the Laminar path above).
						if operation_key in self._operation_attempts and not getattr(result, "error", None):
							self._operation_attempts[operation_key] = 0

						# Handle result types
						if isinstance(result, str):
							return ActionResult(extracted_content=result)
						elif isinstance(result, ActionResult):
							return result
						elif result is None:
							return ActionResult()
						elif isinstance(result, dict) and "is_done" in result:
							return ActionResult(
								extracted_content=result.get("content", ""),
								is_done=result["is_done"],
								include_in_memory=True
							)
						else:
							return ActionResult(extracted_content=str(result))
				except Exception as e:
					import traceback
					error_msg = _dedup_action_error(action_name, e, traceback.format_exc())
					self.logger.error(error_msg)
					return ActionResult(
						error=error_msg,
						include_in_memory=True
					)
			
			# If no action was executed, return an empty result
			return ActionResult()
		except Exception as e:
			# Catch-all for any unexpected exceptions
			import traceback
			
			# Get the current span from Laminar if it exists
			current_span = None
			try:
				from laminar import Laminar
				current_span = getattr(Laminar, 'current_span', None)
			except ImportError:
				pass
			
			# Record error in span if it exists
			if current_span is not None:
				try:
					current_span.set_status(current_span.Status.ERROR)
					current_span.record_exception(e)
				except Exception as span_err:
					self.logger.error(f"Could not record error in span: {str(span_err)}")
			
			error_msg = f"Unexpected error in Controller.act: {str(e)}\n{traceback.format_exc()}"
			self.logger.error(error_msg)
			return ActionResult(
				error=error_msg,
				include_in_memory=True
			)

	def _get_operation_key(self, action_name: str, params: Any) -> str:
		"""Generate unique key for retry tracking.

		Args:
			action_name: Name of the action being executed
			params: Parameters for the action

		Returns:
			Unique string key for tracking retries
		"""
		# For MCP tools, key by server + tool name (ignore specific arguments)
		if action_name == "mcp_execute_tool" or action_name.startswith("mcp_"):
			if isinstance(params, dict):
				server = params.get("server_name", "")
				tool = params.get("tool_name", "")
				return f"mcp:{server}:{tool}"
			else:
				# Pydantic model or other object
				server = getattr(params, "server_name", "")
				tool = getattr(params, "tool_name", "")
				return f"mcp:{server}:{tool}"

		# For other actions, key by action name + a stable hash of the params, so
		# distinct targets (e.g. different file_path) get INDEPENDENT retry budgets.
		# Keying by action name alone let a burst of failures against bad targets
		# exhaust the counter and then reject a later VALID call to the same action
		# (a blocked call never runs, so it never resets). This matches what the old
		# comment already (incorrectly) claimed the code did.
		try:
			if isinstance(params, dict):
				payload = params
			elif hasattr(params, "model_dump"):
				payload = params.model_dump()
			elif hasattr(params, "dict"):
				payload = params.dict()
			else:
				payload = getattr(params, "__dict__", {}) or {}
			items = sorted(payload.items(), key=lambda kv: str(kv[0]))
			disc = hashlib.sha1(repr(items).encode("utf-8", "replace")).hexdigest()[:8]
			return f"{action_name}:{disc}"
		except Exception:
			return action_name

	def _get_retry_limit_for_tool(self, tool_name: str) -> int:
		"""Get the retry limit for a specific tool type.

		Some tools (like MCP/Polymarket) need higher retry limits due to
		network latency, subprocess initialization time, etc.

		Args:
			tool_name: Name of the tool (e.g., 'polymarket', 'mcp', 'browser')

		Returns:
			Maximum retry attempts for this tool type
		"""
		if not tool_name:
			return self._max_operation_retries

		# Check for exact match first
		if tool_name in self._tool_retry_limits:
			return self._tool_retry_limits[tool_name]

		# Check for prefix match (e.g., 'polymarket_search_markets' matches 'polymarket')
		tool_lower = tool_name.lower()
		for prefix, limit in self._tool_retry_limits.items():
			if tool_lower.startswith(prefix):
				return limit

		return self._max_operation_retries

	def _capture_tool_started(
		self,
		action_name: str,
		tool_name: str,
		params: dict,
		call_id: Optional[str] = None,
		index: int = 0,
		total_in_batch: int = 1,
	) -> None:
		"""Emit the 019 ``tool_started`` span-start feed event (fail-open).

		Gated by ``RUN_EVENTS_ENABLED``; a telemetry failure never affects
		execution.
		"""
		try:
			from core.config_policy import AutonomyConfig
			if not AutonomyConfig.run_events_enabled():
				return
			if not self.orchestrator or not hasattr(self.orchestrator, 'telemetry_manager'):
				return
			telemetry_manager = self.orchestrator.telemetry_manager
			if not telemetry_manager:
				return
			step = getattr(self.orchestrator, 'current_step', 0)
			telemetry_manager.capture_tool_started(
				step=step,
				tool_name=tool_name,
				action_name=action_name,
				parameters=params,
				call_id=call_id,
				index=index,
				total_in_batch=total_in_batch,
			)
		except Exception as e:
			self.logger.debug(f"Failed to capture tool started: {e}")

	def _capture_tool_telemetry(
		self,
		action_name: str,
		tool_name: str,  # ← Now passed directly (no detection needed!)
		params: dict,
		duration: float,
		success: bool,
		result: Any,
		execution_context: Optional[ActionExecutionContext] = None,
		error: Optional[str] = None,
		call_id: Optional[str] = None
	) -> None:
		"""Capture telemetry for tool execution.
		
		ARCHITECTURE FIX: tool_name is now passed directly instead of being "detected"
		from action_name. This eliminates redundant registry lookups.
		
		Args:
			action_name: Name of the action executed
			tool_name: Tool this action belongs to (from registered action metadata)
			params: Action parameters
			duration: Execution duration in seconds
			success: Whether execution succeeded
			result: Action result
			execution_context: Execution context
			error: Error message if failed
		"""
		try:
			# Get telemetry manager from orchestrator
			if not self.orchestrator or not hasattr(self.orchestrator, 'telemetry_manager'):
				return

			telemetry_manager = self.orchestrator.telemetry_manager
			if not telemetry_manager:
				return

			# Get current step from orchestrator if available
			step = 0
			if hasattr(self.orchestrator, 'current_step'):
				step = getattr(self.orchestrator, 'current_step', 0)

			# tool_name is now passed directly - no detection/lookup needed!

			# Calculate result size and preview
			# FIX (Dec 31, 2025): Use tool-specific preview sizes for better debugging visibility
			TOOL_PREVIEW_SIZES = {
				'mcp': 2000,       # MCP results often contain important structured data
				'browser': 1000,  # Browser DOM snapshots need more context
				'filesystem': 3000,  # File contents should show more
				'default': 500    # General default (increased from 200)
			}
			preview_size = TOOL_PREVIEW_SIZES.get(tool_name, TOOL_PREVIEW_SIZES['default'])

			result_size = None
			result_preview = None
			result_truncated = False

			if result and hasattr(result, 'extracted_content'):
				content = result.extracted_content
				if content:
					result_size = len(str(content))
					result_preview = str(content)[:preview_size]
					result_truncated = result_size > preview_size

			# Capture tool execution event
			telemetry_manager.capture_tool_execution(
				step=step,
				tool_name=tool_name,
				action_name=action_name,
				parameters=params,
				duration=duration,
				success=success,
				error=error,
				result_size=result_size,
				result_truncated=result_truncated,
				result_preview=result_preview,
				call_id=call_id
			)
		except Exception as e:
			# Don't let telemetry failures affect execution
			self.logger.debug(f"Failed to capture tool telemetry: {e}")
