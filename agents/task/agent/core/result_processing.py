"""Action result-processing mixin (roadmap P9; code-motion from step.py).

Post-execution handling: turning action results into tool messages and processing
them into the next-step brain/memory. Split out of StepMixin so step.py trends
toward the step-phase skeleton. Agent composes ResultProcessingMixin; the step
loop calls these via MRO.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from agents.task.agent.views import ActionResult, AgentBrain
from agents.task.agent.core.untrusted_wrap import maybe_wrap
from modules.llm.messages import AIMessage, ToolMessage

logger = logging.getLogger(__name__)


def _pair_results_to_calls(result, tool_calls_to_pass, source_for=None):
	"""Map each tool_call id -> (content, had_error) by identity.

	Prefers ActionResult.tool_call_id (set by multi_act). Falls back to
	positional pairing only when ids are absent (legacy / non-native path).

	UP-06: when ``source_for`` is provided (a callable ``tool_call_id ->
	(action_name, tool)``), the string ``extracted_content`` of an untrusted
	tool result (mcp/browser/web/perplexity) is framed in
	``<untrusted_tool_result>`` delimiters. ``source_for=None`` (default) skips
	wrapping entirely — preserving the legacy 2-arg call sites/tests byte-for-byte.
	Only the returned string is wrapped; the ``ActionResult`` object is never
	mutated, so memory previews / telemetry stay clean.
	"""
	def _entry(ar, tc_id=None):
		if ar.error:
			# UP-06: an untrusted tool can control its own error string, so the error
			# branch must go through the same wrap as extracted_content — otherwise
			# injected instructions in a tool's error reach the model unframed.
			content = f"Error: {ar.error}"
			if source_for is not None and tc_id is not None:
				action_name, tool = source_for(tc_id)
				content = maybe_wrap(action_name, tool, content)
			return (content, True)
		if ar.extracted_content:
			content = str(ar.extracted_content)
			if source_for is not None and tc_id is not None:
				action_name, tool = source_for(tc_id)
				content = maybe_wrap(action_name, tool, content)
			return (content, False)
		if ar.is_done:
			return ("Task marked as complete", False)
		return ("Action completed successfully", False)

	results = result or []
	have_ids = len(results) > 0 and all(getattr(r, "tool_call_id", None) for r in results)
	paired = {}
	if have_ids:
		ids = [ar.tool_call_id for ar in results]
		if len(set(ids)) == len(ids):
			return {ar.tool_call_id: _entry(ar, ar.tool_call_id) for ar in results}
		# Duplicate tool_call_id in one step: identity pairing would silently drop a
		# result (both dups collapse to one dict key). Fall back to POSITION-keyed
		# pairing — (tool_call_id, index) — so every result surfaces; the consumer
		# looks up by (id, i) first, then by id. Keeps the unique early-return above
		# (keyed by id) unchanged.
		logger.warning(
			"[pair] duplicate tool_call_id detected (%s); falling back to positional pairing",
			ids,
		)
	for i, tc in enumerate(tool_calls_to_pass):
		if i < len(results):
			tc_id = tc.get("id")
			paired[(tc_id, i)] = _entry(results[i], tc_id)
	return paired


class ResultProcessingMixin:
	"""Tool-message construction + action-result processing for Agent."""

	async def _add_tool_messages(self, result, model_output, tool_calls_to_pass) -> bool:
		"""Add AIMessage + ToolMessages for the step's tool calls.

		Returns True on success (continue the step) and False on the corrupted-state
		path (atomic mismatch), which signals the caller to abort the step.
		"""
		# Atomic message addition: AIMessage + all ToolMessages added together
		if self.use_native_tools and tool_calls_to_pass:
			self.logger.info(f"[ATOMIC] Preparing atomic addition for {len(tool_calls_to_pass)} tool calls")

			# FIXED: Get validation errors from controller for tool calls that failed validation
			validation_errors = {}
			if self.controller:
				validation_errors = self.controller.get_last_validation_errors()
				if validation_errors:
					self.logger.warning(f"[ATOMIC] {len(validation_errors)} tool calls had validation errors")

			# Step 1: Build all tool responses from action results
			# Pair results to calls by IDENTITY (tool_call_id), not list position —
			# the executor reorders/truncates actions, so positional pairing misaligns.
			tool_responses = []
			# UP-06: resolve each tool call's (action_name, tool namespace) so untrusted
			# results (mcp/browser/web/perplexity) get framed as DATA before entering
			# history. Gated on the flag; source_for=None => no wrapping (legacy).
			source_for = None
			from agents.task.constants import UNTRUSTED_TOOL_RESULT_WRAP
			if UNTRUSTED_TOOL_RESULT_WRAP:
				name_by_id = {tc.get('id'): tc.get('name') for tc in tool_calls_to_pass if tc.get('id')}

				def _source_for(tc_id, _name_by_id=name_by_id):
					name = _name_by_id.get(tc_id)
					tool = None
					if name and self.controller:
						# NOTE: get_action() returns the callable; get_action_details()
						# returns the RegisteredAction carrying the .tool namespace.
						ra = self.controller.get_action_details(name)
						tool = getattr(ra, 'tool', None) if ra else None
					return (name, tool)

				source_for = _source_for
			paired = _pair_results_to_calls(result, tool_calls_to_pass, source_for=source_for)

			for i, tc in enumerate(tool_calls_to_pass):
				tc_id = tc.get('id')
				tc_name = tc.get('name', 'unknown')

				if not tc_id:
					self.logger.error(f"Tool call at index {i} missing ID, skipping")
					continue

				# FIXED: Check validation errors FIRST - these tool calls were never executed
				if tc_id in validation_errors:
					error_msg = validation_errors[tc_id]
					self.logger.warning(f"Tool call '{tc_name}' (id={tc_id}) failed validation: {error_msg[:100]}")
					tool_responses.append((tc_id, f"Error: {error_msg}"))

					if self.tool_call_tracker:
						self.tool_call_tracker.mark_failed(tc_id, f"Validation failed: {error_msg[:100]}")
					# This call was never executed (no result to pair)
					continue

				# This tool call passed validation - pair its result. Try the
				# position-keyed entry first (duplicate-id fallback), then the
				# identity entry (the normal unique-id path).
				entry = paired.get((tc_id, i))
				if entry is None:
					entry = paired.get(tc_id)
				if entry is not None:
					content, had_error = entry
					tool_responses.append((tc_id, content))
					if self.tool_call_tracker:
						if had_error:
							self.tool_call_tracker.mark_failed(tc_id, content)
						else:
							self.tool_call_tracker.mark_completed(tc_id, content[:200])
				else:
					error_msg = (
						f"Action '{tc_name}' was NOT executed this step "
						f"(deferred, truncated, or a prior action stopped execution). "
						f"Retry it next step if still needed."
					)
					tool_responses.append((tc_id, f"Error: {error_msg}"))
					if self.tool_call_tracker:
						self.tool_call_tracker.mark_failed(tc_id, "Not executed this step")

			# Step 2: Extract AI content from model_output (reuse existing pattern from line 2057)
			try:
				if model_output and hasattr(model_output, 'current_state'):
					brain_dict = model_output.current_state.model_dump(exclude_unset=True)
					ai_content = json.dumps(brain_dict, separators=(',', ':'))  # Compact JSON
				else:
					ai_content = "Processing actions"
			except Exception as e:
				self.logger.warning(f"Failed to serialize brain state: {e}")
				ai_content = "Brain state extraction failed"

			# Step 3: Atomic addition - all or nothing
			try:
				self.message_manager.add_tool_call_pair_atomic(
					ai_content=ai_content,
					tool_calls=tool_calls_to_pass,
					tool_responses=tool_responses
				)
				self.logger.info(f"[ATOMIC] ✅ Added AIMessage + {len(tool_responses)} ToolMessages atomically")

				# Complete the step in tracker
				if self.tool_call_tracker:
					self.tool_call_tracker.complete_step()

			except ValueError as e:
				# FIX: Fail fast on mismatch - don't use degraded fallback
				# The atomic method already rolled back, so we're in a clean state
				# Adding separately would create inconsistent message state
				self.logger.error(
					f"[ATOMIC] Failed atomic addition (mismatch): {e}. "
					f"Expected {len(tool_calls_to_pass)} tool_calls, got {len(tool_responses)} responses. "
					f"Tool call IDs: {[tc.get('id') for tc in tool_calls_to_pass]}"
				)
				# Mark step as failed in tracker
				if self.tool_call_tracker:
					self.tool_call_tracker.complete_step()
				# Set error result so next step can retry
				self._last_result = [ActionResult(
					error=f"Tool response mismatch: {e}. Actions executed but responses couldn't be recorded atomically.",
					include_in_memory=True
				)]
				return False  # Don't continue with corrupted state
			except Exception as e:
				# Other error during atomic addition
				self.logger.error(f"[ATOMIC] Failed atomic addition: {e}")
				# History already rolled back by atomic method
				raise

		elif tool_calls_to_pass:
			self.logger.debug("Using non-atomic pattern (non-native tools)")
			tool_call_id = self.message_manager.add_model_output(model_output, tool_calls=tool_calls_to_pass)

		return True

	async def _process_action_results(self, result, model_output, tool_calls_to_pass):
		"""Phase 3b: record action results into context (large-result handling + atomic tool-message pairing). Returns the result list, or None to abort on corrupted state."""
		if model_output and hasattr(model_output, 'action') and model_output.action:
			# OPTIMIZATION: Log tool outputs (Task 1 - Nov 14, 2025)
			self._log_tool_outputs(model_output.action, result, self.state.n_steps)

			# CRITICAL: Handle large action results BEFORE adding to context
			# This offloads content >10K to files, preventing context overflow
			# Must happen BEFORE tool responses are added to message history
			self._handle_large_action_results(result)

			# Atomic message addition: AIMessage + all ToolMessages added together
			if not await self._add_tool_messages(result, model_output, tool_calls_to_pass):
				return None  # Don't continue with corrupted state

			# Build intelligent brain state from previous state + action results
			if model_output and hasattr(model_output, 'action') and model_output.action and result:
				# Start with previous brain state for continuity
				previous_brain = None
				if hasattr(self, '_last_model_output') and self._last_model_output and hasattr(self._last_model_output, 'current_state'):
					previous_brain = self._last_model_output.current_state

				# Build specific action summaries with actual results
				action_summaries = []
				has_errors = False
				has_completion = False

				for i, res in enumerate(result):
					if i >= len(model_output.action):
						break

					# Safe extraction with validation (matching pattern from _log_response)
					action_dump = model_output.action[i].model_dump(exclude_unset=True)
					if not action_dump:
						self.logger.warning(f"Action {i} has empty model_dump, skipping")
						continue

					action_keys = list(action_dump.keys())
					if not action_keys:
						self.logger.warning(f"Action {i} has no keys in model_dump, skipping")
						continue

					action_name = action_keys[0]
					action_params = action_dump[action_name]

					# Build specific description with params and results
					param_str = ""
					if isinstance(action_params, dict) and action_params:
						# Show key params (not all)
						key_params = []
						for k, v in list(action_params.items())[:2]:  # First 2 params only
							if isinstance(v, str):
								key_params.append(f"{k}={v[:30]}")
							else:
								key_params.append(f"{k}={v}")
						param_str = f"({', '.join(key_params)})" if key_params else ""

					if res.error:
						action_summaries.append(f"{action_name}{param_str}→ERROR:{res.error[:50]}")
						has_errors = True
					elif res.is_done:
						action_summaries.append(f"{action_name}{param_str}→DONE")
						has_completion = True
					elif res.extracted_content:
						# Show actual extracted content
						content_preview = res.extracted_content[:80].replace('\n', ' ')
						action_summaries.append(f"{action_name}{param_str}→{content_preview}")
					else:
						action_summaries.append(f"{action_name}{param_str}→OK")

				# Extract previous memory and progress for continuity
				previous_memory = None
				previous_progress = None
				if previous_brain and hasattr(previous_brain, 'memory'):
					previous_memory = previous_brain.memory
					previous_progress = self._extract_progress_from_memory(previous_memory)

				# Build memory that preserves previous context
				new_memory = self._build_memory_from_actions(
					step_number=self.state.n_steps,
					action_summaries=action_summaries,
					previous_memory=previous_memory,
					previous_progress=previous_progress
				)

				# Build evaluation
				if has_completion:
					evaluation = f"Success - Task marked complete at step {self.state.n_steps}"
				elif has_errors:
					evaluation = f"Partial - {sum(1 for r in result if not r.error)}/{len(result)} actions succeeded"
				else:
					evaluation = f"Success - All {len(result)} actions completed"

				# Keep previous goal if it existed, otherwise create continuation goal
				if previous_brain and hasattr(previous_brain, 'next_goal') and previous_brain.next_goal and 'Continue' not in previous_brain.next_goal:
					next_goal = previous_brain.next_goal  # Preserve specific goal
				else:
					# Create new goal based on what we just did
					next_goal = f"Continue based on results from step {self.state.n_steps}"

				# Build reasoning from action sequence
				reasoning = f"Executed: {' → '.join([a.split('→')[0] for a in action_summaries[:3]])}"

				# ✅ FIX #3: Enhanced brain state synthesis
				# Synthesis happens AFTER actions execute, so it can use actual results
				# Also detect placeholder brain state and replace it
				needs_synthesis = (
					not model_output.current_state
					or not model_output.current_state.memory
					or "Synthesis pending" in model_output.current_state.memory
					or model_output.current_state.evaluation_previous_goal == "Pending"
				)

				# FIX #3: Detect hallucinated brain state
				# If LLM says it "created/wrote/generated" but actual actions were only "read", force synthesis
				if not needs_synthesis and model_output.current_state and model_output.current_state.memory:
					llm_memory = model_output.current_state.memory.lower()
					actual_action_names = [a.split('→')[0].split('(')[0].lower() for a in action_summaries]

					# Check for hallucination: LLM claims write actions but only read actions occurred
					write_claims = any(word in llm_memory for word in ['created', 'wrote', 'generated', 'saved', 'writing'])
					actual_reads_only = all('read' in a for a in actual_action_names)

					if write_claims and actual_reads_only and len(actual_action_names) > 0:
						self.logger.warning(
							f"🚨 Detected hallucinated brain state: LLM claims write actions "
							f"but actual actions were [{', '.join(actual_action_names)}]. Forcing synthesis."
						)
						needs_synthesis = True

				if needs_synthesis:
					# LLM failed to provide brain state OR used placeholder OR hallucinated
					model_output.current_state = AgentBrain(
						page_summary="",
						memory=new_memory[:500],
						evaluation_previous_goal=evaluation,
						next_goal=next_goal,
						reasoning=reasoning[:200]
					)
					self.logger.info(f"✅ Synthesized brain state from action results: {new_memory[:120]}")
					# CO-F8: write the synthesized brain back to _last_brain_state so the
					# next memory-prefetch recall query is enriched instead of degrading
					# to the static task string (no-text-content / tool-calls-only steps).
					self._last_brain_state = model_output.current_state
				# ✅ FIX: Removed enhancement logic - don't corrupt LLM-provided brain state

		return result

