"""Step validate + execute phases (roadmap P9; code-motion from step.py).

The _validate_and_intervene gate, execution-context construction, and the
_execute_actions phase — split out of StepMixin so step.py drops under 700L while
keeping the step pipeline readable. Agent composes StepExecutionMixin; _step_impl
calls these via MRO.
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

from agents.task.agent.views import ActionResult
from modules.llm.messages import HumanMessage
from agents.task.constants import MAX_MCP_PER_STEP

#: OR-4: how many consecutive empty-action steps before the strong "thinking loop"
#: intervention fires. Default 2 (one step sooner than the legacy 3) so a model
#: that answers in prose without a tool call — Grok/GPT-5 on content-gen — is
#: pushed to act faster, wasting fewer steps. Set EMPTY_ACTION_FAST_ESCALATE=off
#: to restore the legacy threshold of 3.
_FAST_ESCALATE = os.getenv("EMPTY_ACTION_FAST_ESCALATE", "true").strip().lower() not in (
    "0",
    "false",
    "off",
    "no",
)
_EMPTY_ACTION_ESCALATE_AT = 2 if _FAST_ESCALATE else 3


class StepExecutionMixin:
	"""Validate-and-intervene + execute-actions step phases for Agent."""

	def _validate_and_intervene(self, model_output) -> bool:
		"""Phase 2b: validate model output; inject corrective guidance and return False if the step should end early, else True."""
		# Validate model output
		if not self._validate_model_output(model_output):
			# Inject corrective guidance
			# Initialize thinking loop counter if needed
			if not hasattr(self, '_empty_action_counter'):
				self._empty_action_counter = 0

			self._empty_action_counter += 1
			self.logger.warning(f"Empty action response {self._empty_action_counter}/{_EMPTY_ACTION_ESCALATE_AT}")

			# D1-a (seed resolution): tolerate a bounded number of tool-free "planning"
			# turns. Instead of treating the very first empty response as an error,
			# acknowledge it as a planning turn and nudge (not scold) the model to act
			# next. The >=3 thinking-loop escalation below remains the hard backstop, so
			# the loop bound is unchanged. Disable with ALLOWED_REASONING_TURNS=0.
			from agents.task.constants import ALLOWED_REASONING_TURNS
			if self._empty_action_counter <= ALLOWED_REASONING_TURNS:
				self.logger.info(
					f"📝 Tool-free planning turn {self._empty_action_counter}/{ALLOWED_REASONING_TURNS} "
					f"allowed (no action this step; must act next)"
				)
				self.message_manager.inject_user_guidance([{
					'text': (
						"📝 Planning turn noted. You may reason this turn, but you MUST call at "
						"least one function on your NEXT step to make progress "
						"(e.g. done(text=...) if you are finished)."
					),
					'kind': 'guidance',
					'metadata': {'source': 'reasoning_turn_allowance', 'turn': self._empty_action_counter}
				}])
				# Not an error: a planning turn is a legitimate (bounded) outcome.
				# Tagged for telemetry/clarity; the R1 conversational-exit treats it as
				# a non-reply step (it lacks conversational_reply), so it resets the
				# reply-run rather than counting toward an exit.
				self._last_result = [ActionResult(
					extracted_content="Planning turn (no action taken). Act on the next step.",
					include_in_memory=False,
					metadata={'planning_turn': True}
				)]
				if self.tool_call_tracker:
					self.tool_call_tracker.complete_step()
				return False

			# Get available action count for context
			available_actions = len(self.controller.get_action_names()) if self.controller else 0

			# Escalating intervention after N consecutive failures (OR-4: N defaults
			# to 2 so a prose-only model is pushed to act one step sooner).
			if self._empty_action_counter >= _EMPTY_ACTION_ESCALATE_AT:
				_n = self._empty_action_counter
				self.logger.error(
					f"🚨 CRITICAL: Thinking loop detected - {_n} consecutive steps without function calls"
				)

				# Strong intervention with explicit example
				self.message_manager.inject_user_guidance([{
					'text': (
						"🚨 INTERVENTION: Thinking loop detected.\n\n"
						f"You have failed to call functions for {_n} consecutive steps.\n"
						"This violates the core agent contract.\n\n"
						"**STOP thinking. START acting.**\n\n"
						"Example for current task:\n"
						f"Task: {self.task}\n\n"
						"If this is a file operation → call filesystem_write_file(...)\n"
						"If this is a search → call mcp_execute_tool(...)\n"
						"If you're stuck → call done(text='explanation')\n\n"
						"**CALL A FUNCTION IN YOUR NEXT RESPONSE.**\n"
						"No more explanations, no more planning."
					),
					'kind': 'intervention',
					'metadata': {
						'source': 'thinking_loop_detector',
						'counter': _n,
						'task': self.task[:100] if self.task else 'unknown'
					}
				}])

				# History clearing disabled - preserves context
				self.logger.info("Thinking loop detected - guidance injected")

				# Reset counter for next attempt
				self._empty_action_counter = 0

				# Set error result
				self._last_result = [ActionResult(
					error="Thinking loop detected - guidance injected. YOU MUST CALL A FUNCTION.",
					include_in_memory=True
				)]
			else:
				# Normal error handling for attempts 1-2
				self.message_manager.inject_user_guidance([
					{
						'text': (
							"❌ CRITICAL ERROR: No function calls detected.\n\n"
							"You MUST call at least one function every step.\n"
							"There is no 'thinking mode' or 'planning phase'.\n\n"
							"**What to do RIGHT NOW:**\n"
							"1. Look at the available functions below\n"
							"2. Pick one that makes progress on the task\n"
							"3. CALL IT with proper parameters\n\n"
							f"You have {available_actions} functions available - use them!"
						),
						'kind': 'error',
						'metadata': {'source': 'empty_actions_validation', 'attempt': self._empty_action_counter}
					}
				])

				# Set error result
				self._last_result = [ActionResult(
					error=f"No function calls (attempt {self._empty_action_counter}/{_EMPTY_ACTION_ESCALATE_AT})",
					include_in_memory=True
				)]

			# Clean up tracker (no messages added since execution never happened)
			if self.tool_call_tracker:
				self.tool_call_tracker.complete_step()
				self.logger.debug("Cleared tool call tracker after validation error")
			return False

		# Check if model output has no actions - treat as failure
		if not model_output.action or len(model_output.action) == 0:
			self.logger.error("❌ Model failed to generate actions - this indicates an LLM error")
			self.logger.error("Empty actions violate the agent contract. LLM must provide at least one action per step.")

			# Treat as a normal failure
			self.state.increment_failures()
			self._last_result = [ActionResult(
				error="LLM failed to generate actions. This may be a prompt issue, API problem, or model limitation.",
				include_in_memory=True
			)]
			if self.tool_call_tracker:
				self.tool_call_tracker.complete_step()
			return False

		return True

	def _build_execution_context(self, browser_context):
		"""Build the ActionExecutionContext for this step's action execution."""
		from tools.controller.execution_context import ActionExecutionContext
		# SK-F10: stamp the forged-turn marker (set by _drain_user_messages when the
		# turn was opened by a self-wake / async-delegation-result re-entry, cleared
		# on a genuine drained turn) so action closures (skill_manage,
		# self_context_manage) can tell a forged main-agent turn apart from a
		# genuine one — both otherwise look identical (role='orchestrator',
		# is_sub_agent=False). getattr default is None: an orchestrator that never
		# drained a forged message is genuine.
		turn_kind = getattr(self.orchestrator, '_forged_turn_kind', None)
		execution_context = ActionExecutionContext(
			browser_context=browser_context,  # Use local variable from step
			# Agent identification for sub-agent isolation
			agent_id=self.agent_id,
			is_sub_agent=self._is_sub_agent,
			role=getattr(self, '_role', 'orchestrator'),
			parent_session_id=self._parent_session_id if self._is_sub_agent else None,
			# Use effective_session_id (virtual for sub-agents, real for main)
			session_id=self.effective_session_id,
			user_id=self.user_id,
			workspace_dir=self.orchestrator.workspace_dir,
			available_file_paths=self.available_file_paths or [],
			sensitive_data=self.sensitive_data or {},
			metadata={"turn_kind": turn_kind},
		)
		return execution_context

	async def _execute_actions(self, model_output, tool_calls_to_pass, state, step_info, browser_context):
		"""Phase 3: run the model's actions via the controller and handle results. Returns the result list, or None to signal the step should abort (corrupted state)."""
		# Allow agents to use as many actions as needed per step
		if model_output and hasattr(model_output, 'action') and model_output.action:
			# CRITICAL FIX: Limit MCP actions to prevent timeout cascades
			# MCP tools (scraping, searches, APIs) are expensive and execute SEQUENTIALLY
			actions_list = model_output.action if isinstance(model_output.action, list) else [model_output.action]

			# Re-queue deferred MCP actions from previous step (if any)
			if self._deferred_mcp_actions:
				self.logger.info(f"🔄 Re-queuing {len(self._deferred_mcp_actions)} deferred MCP actions from previous step")
				# Prepend deferred actions so they execute first
				actions_list = self._deferred_mcp_actions + list(actions_list)
				self._deferred_mcp_actions = []
				# CO-F2: make the merged list authoritative — execution reads
				# model_output.action, which is otherwise only rebuilt when the
				# MCP throttle re-fires, silently dropping the re-queued actions.
				model_output.action = actions_list

			# Analyze action composition
			mcp_actions = []
			non_mcp_actions = []

			for action in actions_list:
				action_dump = action.model_dump(exclude_unset=True) if hasattr(action, 'model_dump') else {}
				action_keys = list(action_dump.keys())
				action_name = action_keys[0] if action_keys else None

				if action_name and action_name.startswith('mcp_execute_tool'):
					mcp_actions.append(action)
				else:
					non_mcp_actions.append(action)

			# Apply MCP throttling to prevent timeout cascades
			# Uses centralized constant from agents.task.constants

			if len(mcp_actions) > MAX_MCP_PER_STEP:
				self.logger.warning(
					f"⚠️  Step has {len(mcp_actions)} MCP actions - limiting to {MAX_MCP_PER_STEP} "
					f"to prevent timeout cascades (MCP actions execute sequentially, ~30-180s each)"
				)

				# Keep first MAX_MCP_PER_STEP MCP actions
				kept_mcp = mcp_actions[:MAX_MCP_PER_STEP]
				deferred_mcp = mcp_actions[MAX_MCP_PER_STEP:]

				# Save deferred actions for next step (up to max deferrals)
				# Track deferral count via _deferral_count attribute on action
				for action in deferred_mcp:
					if not hasattr(action, '_deferral_count'):
						action._deferral_count = 0
					action._deferral_count += 1

					if action._deferral_count <= self._max_mcp_deferrals:
						self._deferred_mcp_actions.append(action)
					else:
						action_dump = action.model_dump(exclude_unset=True) if hasattr(action, 'model_dump') else {}
						action_name = list(action_dump.keys())[0] if action_dump else "unknown"
						self.logger.warning(
							f"⚠️ Dropping MCP action '{action_name}' after {self._max_mcp_deferrals} deferrals"
						)

				# Log what's being executed vs deferred
				self.logger.info(
					f"Executing {len(kept_mcp)} MCP + {len(non_mcp_actions)} other actions this step. "
					f"Deferred {len(self._deferred_mcp_actions)} MCP actions to next step."
				)

				# Reconstruct action list: kept MCP + all non-MCP
				model_output.action = kept_mcp + non_mcp_actions
				actions_list = model_output.action

			action_count = len(actions_list)
			self.logger.info(f"Executing {action_count} actions in this step")

			# VALIDATION: Check that all requested actions exist before execution
			invalid_actions = []
			for action in actions_list:
				action_dump = action.model_dump(exclude_unset=True) if hasattr(action, 'model_dump') else {}
				action_keys = list(action_dump.keys())
				action_name = action_keys[0] if action_keys else None

				if action_name and not self.controller.has_action(action_name):
					invalid_actions.append(action_name)

			if invalid_actions:
				error_msg = (
					f"Agent requested invalid actions: {invalid_actions}. "
					f"These actions are not registered in the controller. "
					f"Available actions: {len(self.controller.registry.list_action_names())}"
				)
				self.logger.error(f"❌ {error_msg}")
				raise ValueError(error_msg)

			# Issue #8 Fix: Pre-execution MCP validation loop check
			# Block MCP actions that have repeatedly failed validation (saves resources)
			if mcp_actions and self.tool_call_tracker:
				blocked_mcp = []
				allowed_mcp = []

				for action in mcp_actions:
					action_dump = action.model_dump(exclude_unset=True) if hasattr(action, 'model_dump') else {}
					mcp_params = action_dump.get('mcp_execute_tool', {})
					server_name = mcp_params.get('server_name', '')
					tool_name = mcp_params.get('tool_name', '')

					if server_name and tool_name:
						if self.tool_call_tracker.should_inject_mcp_schema(server_name, tool_name):
							blocked_mcp.append((server_name, tool_name))
							self.logger.warning(
								f"🚫 Blocking repeated MCP validation failure: {server_name}/{tool_name}. "
								f"Check the error messages above for correct parameter format."
							)
						else:
							allowed_mcp.append(action)
					else:
						allowed_mcp.append(action)

				if blocked_mcp:
					# Update the MCP actions list to only include allowed ones
					mcp_actions = allowed_mcp
					model_output.action = mcp_actions + non_mcp_actions
					actions_list = model_output.action

					# Inject guidance
					blocked_names = [f"{s}/{t}" for s, t in blocked_mcp]
					self.message_manager.inject_user_guidance([{
						'text': (
							f"⚠️ Blocked {len(blocked_mcp)} MCP actions due to repeated validation failures: {blocked_names}\n\n"
							f"The schema has been injected in previous error messages. "
							f"Please review the FULL SCHEMA and correct your arguments before retrying."
						),
						'kind': 'warning',
						'metadata': {'source': 'mcp_validation_loop_prevention'}
					}])

			# PHASE 2 FIX (Nov 4, 2025): Loop Detection
			# Detect if agent is repeating same actions without progress
			if model_output and hasattr(model_output, 'current_state') and model_output.current_state:
				loop_detected, loop_warning = self.detect_action_loop(
					model_output.current_state,
					actions_list
				)

				if loop_detected and loop_warning:
					# Log warning
					self.logger.warning(f"⚠️  Loop detected at step {self.state.n_steps}")

					# Inject warning into agent's next context as ephemeral message
					# HumanMessage imported at module level from modules.llm.messages
					self.message_manager.push_ephemeral_message(
						HumanMessage(content=loop_warning)
					)

					# Optional: Could halt after N consecutive loops
					# For now: Non-blocking, agent gets feedback and can adjust

			# CRITICAL FIX: Actually execute the actions through controller
			execution_context = self._build_execution_context(browser_context)

			# FIX (Jan 2026): Tool call rate limiting to prevent context overflow
			# Limit parallel tool calls to prevent 60K+ token additions in a single step
			MAX_TOOL_CALLS_PER_STEP = 3  # Allow up to 3 parallel calls
			actions_to_execute = model_output.action
			deferred_actions = []

			if len(actions_to_execute) > MAX_TOOL_CALLS_PER_STEP:
				self.logger.warning(
					f"⚠️ Tool limit: {len(actions_to_execute)} actions requested, "
					f"executing first {MAX_TOOL_CALLS_PER_STEP}, deferring {len(actions_to_execute) - MAX_TOOL_CALLS_PER_STEP}"
				)
				deferred_actions = actions_to_execute[MAX_TOOL_CALLS_PER_STEP:]
				actions_to_execute = actions_to_execute[:MAX_TOOL_CALLS_PER_STEP]

				# Inject deferral notice as ephemeral message for next step
				deferred_names = [getattr(a, 'name', str(a)[:30]) for a in deferred_actions]
				self.message_manager.push_ephemeral_message(
					HumanMessage(content=(
						f"⚠️ **Tool Call Limit Applied**\n\n"
						f"{len(deferred_actions)} tool calls were deferred to prevent context overflow:\n"
						f"- {', '.join(deferred_names[:5])}"
						f"{'...' if len(deferred_names) > 5 else ''}\n\n"
						f"Process these in your next step, 1-3 at a time."
					))
				)

			result = await self.controller.multi_act(
				actions=actions_to_execute,
				execution_context=execution_context
			)
			self._last_result = result
			# L3: count the actions actually executed, not the pre-truncation request count
			# (a step requesting >MAX_TOOL_CALLS_PER_STEP defers the rest — action_count
			# would over-count what ran, feeding stall detection / telemetry a wrong number).
			self.state.total_actions_count += len(actions_to_execute)

		return result

