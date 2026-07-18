"""Memory-writing / action-summary mixin (code-motion from service.py)."""

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
    MAX_MCP_PER_STEP
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



class MemoryWriterMixin:
	"""Mixin extracted verbatim from agents/task/agent/service.py (pure code-motion)."""

	def _extract_progress_from_memory(self, memory: Optional[str]) -> Optional[str]:
		"""Extract progress tracking from memory string.

		Args:
			memory: Memory string that may contain progress like "Progress: 3/10"

		Returns:
			Progress string (e.g., "3/10") or None if not found
		"""
		if not memory:
			return None
		import re
		match = re.search(r'Progress:\s*(\d+/\d+)', memory)
		return match.group(1) if match else None

	def _build_memory_from_actions(
		self,
		step_number: int,
		action_summaries: List[str],
		previous_memory: Optional[str] = None,
		previous_progress: Optional[str] = None
	) -> str:
		"""Build memory string from action results.

		Generates a natural memory description from action results when the LLM
		doesn't provide one directly.

		Args:
			step_number: Current step number (for context, not used in output format)
			action_summaries: List of action summary strings (e.g., "action_name→result")
			previous_memory: Previous memory narrative (unused - each step is fresh)
			previous_progress: Optional progress tracking from previous step (e.g., "3/10")

		Returns:
			Natural memory string describing what happened
		"""
		# Part 1: What I JUST did (action taken)
		if action_summaries:
			action_desc = ". ".join(action_summaries[:2])  # First 2 actions for brevity
			if len(action_summaries) > 2:
				action_desc += f" + {len(action_summaries)-2} more actions"
		else:
			action_desc = "No actions taken"

		# Part 2: What I learned (from results)
		learned_parts = []
		for summary in action_summaries[:3]:
			if "→ERROR" in summary:
				error_part = summary.split("→ERROR:")[1] if "→ERROR:" in summary else "action failed"
				learned_parts.append(f"Error: {error_part[:50]}")
			elif "→DONE" in summary:
				learned_parts.append("Task complete")
			elif "→" in summary:
				result = summary.split("→")[1]
				if result and result != "OK":
					learned_parts.append(f"Found: {result[:60]}")

		learned = " ".join(learned_parts[:2]) if learned_parts else ""

		# Part 3: Progress tracking
		progress_part = ""
		if previous_progress:
			progress_part = f" Progress: {previous_progress}."

		# Combine into natural format
		memory = action_desc
		if learned:
			memory += f". {learned}"
		if progress_part:
			memory += f".{progress_part}"

		return memory

	async def _save_step_to_memory(
		self,
		step_number: int,
		brain_state: Dict[str, Any],
		actions: List[Dict[str, Any]],
		results: List[Any],
		step_info: Optional[Any] = None
	) -> None:
		"""Save step to hierarchical memory with brain state memory field.

		CRITICAL: The brain_state.memory field contains the agent's cumulative
		understanding and should be the PRIMARY source for H-MEM findings.

		Args:
			step_number: Current step number
			brain_state: Brain state from LLM (contains 'memory', 'phase', etc.)
			actions: Actions taken this step
			results: Action results
			step_info: Optional AgentStepInfo with max_steps
		"""
		if not self.task_context_manager or not self.session_id:
			self.logger.debug("H-MEM unavailable: task_context_manager or session_id missing")
			return

		try:
			# Build action summary
			action_summary = self._build_action_summary(actions)

			# Extract memory from brain_state.memory (PRIMARY source)
			raw_memory = brain_state.get('memory', '').strip()

			# CO-F4 / A1: the "Synthesis pending..." placeholder brain is not a real
			# finding — it must be treated as empty for BOTH the H-MEM write (handled
			# by the fallback at the bottom) AND the loop-duplicate heuristic. Setting
			# or comparing _last_memory_finding against the placeholder would let two
			# consecutive placeholder steps perturb the thinking-loop signal.
			is_placeholder = raw_memory.startswith("Synthesis pending")

			if raw_memory and not is_placeholder:
				finding = raw_memory

				# Check for duplicate (static memory = thinking loop symptom)
				if hasattr(self, '_last_memory_finding') and finding == self._last_memory_finding:
					self.logger.warning(
						f"⚠️ Memory identical to previous step - thinking loop indicator"
					)
					# Add warning to next step
					try:
						# HumanMessage imported at module level from modules.llm.messages
						self.message_manager.push_ephemeral_message(HumanMessage(
							content=(
								"⚠️ Your memory is identical to the previous step.\n"
								"This suggests you're not making progress.\n\n"
								"Memory should be unique each step:\n"
								"- What you DID this step (actions taken)\n"
								"- What you LEARNED (new insights)\n"
								"- Progress update (X/Y if quantitative)"
							)
						))
					except Exception as e:
						self.logger.warning(f"Could not inject memory warning: {e}")
				else:
					# Save for next comparison
					self._last_memory_finding = finding
			else:
				finding = None

			# Fallback: If memory field empty OR still the early-exit placeholder
			# brain (CO-F4 — "Synthesis pending..." must never land in H-MEM as a
			# real finding), try action results (secondary source).
			if not finding or len(finding) < 10 or finding.startswith("Synthesis pending"):
				self.logger.debug(f"Brain state memory empty at step {step_number}, using action results")
				result_finding = self._extract_finding_from_results(results)
				if result_finding:
					finding = result_finding
				else:
					# P2-3: do NOT fall back to next_goal. next_goal is an IMPERATIVE
					# ("Click the search button"), not a finding, and writing it made junk
					# a permanent recallable cross-session memory row. A step with no real
					# finding writes no finding — the step is still recorded via add_step;
					# only the H-MEM finding is skipped.
					finding = None

			# Get total_steps from step_info if available
			total_steps = step_info.max_steps if step_info and hasattr(step_info, 'max_steps') else None

			# Add to hierarchical memory.
			# HIGH-3: offload off the event loop — add_step_memory is synchronous and, when
			# REFLECTION_LLM_ENABLED=true, blocks on an aux-LLM call (run_coroutine_sync, up to
			# 30s) that would otherwise freeze every concurrent session in this process. We await
			# the result so per-session ordering is preserved (mirrors _save_conversation).
			success = await asyncio.to_thread(
				self.task_context_manager.add_step_memory,
				session_id=self.session_id,
				step=step_number,
				brain_state=brain_state,
				action_summary=action_summary,
				finding=finding,  # Now uses brain_state.memory!
				total_steps=total_steps
			)

			if success:
				# INFO level so we can see H-MEM working in production.
				# P2-3: finding may be None now (no real finding + no next_goal junk
				# fallback) — the step is still recorded, but guard the preview.
				_preview = (finding[:80] + "...") if finding else "(no finding this step)"
				self.logger.info(f"💾 H-MEM saved step {step_number}: {_preview}")
				
				# FIX #4: Track finding in AgentState for loop detection
				self.state.track_finding()
				
				# Validate it's actually in H-MEM
				try:
					session_data = self.task_context_manager.get_session(self.session_id)
					if session_data:
						phase_memory = session_data.memory.get_current_phase_memory()
						if phase_memory:
							finding_count = len(phase_memory.key_findings)
							self.logger.debug(
								f"✅ H-MEM validation: Phase '{phase_memory.phase_name}' "
								f"now has {finding_count} findings"
							)
				except Exception as val_error:
					self.logger.debug(f"H-MEM validation check failed: {val_error}")
			else:
				self.logger.warning(f"⚠️  H-MEM save returned False for step {step_number}")

			# Periodically save to disk (every 10 steps)
			if step_number % 10 == 0:
				self.task_context_manager.save_session(self.session_id, self.user_id)
				self.logger.info(f"💾 Persisted H-MEM to disk (step {step_number})")

		except Exception as e:
			self.logger.error(f"Failed to save step to hierarchical memory: {e}", exc_info=True)

		# P7: also route the completed step through the active MemoryProvider.
		# Cross-session store ingests H-MEM's curated/deduped findings (drain returns
		# only findings new since the last call), NOT the raw per-step brain memory.
		# No-op unless an external provider is registered; isolated + fail-open.
		try:
			from modules.memory.registry import memory_sync_turn
			task_str = getattr(self, 'task', '') or ''
			promoted = []
			if self.task_context_manager and self.session_id:
				promoted = self.task_context_manager.drain_promoted_findings(self.session_id)
			if promoted:
				content = "\n".join(promoted)
				await memory_sync_turn(task_str, content,
				                       session_id=self.session_id, user_id=self.user_id)
				# T4-02: sync_turn curates the agent's own FUTURE recall — previously a
				# self-evolution channel with zero audit. Record a first-class
				# memory_write event (durable log → /telemetry + /activity) and mirror
				# it to the live session feed. Fail-open.
				try:
					from agents.task.telemetry.memory_events import emit_memory_event
					ev_attrs = emit_memory_event("memory_write", user_id=self.user_id or "",
					                             session_id=self.session_id, source="sync_turn",
					                             scope="cross_session", content=content,
					                             count=len(promoted))
					if ev_attrs and getattr(self, "orchestrator", None) is not None:
						try:
							await self.orchestrator.add_to_feed(
								getattr(self, "agent_id", "agent"), "memory_write", dict(ev_attrs))
						except Exception as feed_err:
							self.logger.debug(f"memory write feed mirror skipped: {feed_err}")
				except Exception as ev_err:
					self.logger.debug(f"memory write event skipped: {ev_err}")
		except Exception as e:
			self.logger.debug(f"memory_sync_turn skipped (backend hiccup): {e}")

	def _build_action_summary(self, actions: List[Dict[str, Any]]) -> str:
		"""Build human-readable action summary.

		Args:
			actions: List of actions taken

		Returns:
			Summary string
		"""
		if not actions:
			return "No actions taken"

		summaries = []
		for action in actions:
			action_type = action.get('action_type', 'unknown')
			# Get first key if action is dict
			if isinstance(action, dict) and action:
				first_key = list(action.keys())[0]
				summaries.append(first_key)
			else:
				summaries.append(action_type)

		return f"Executed: {', '.join(summaries)}"

	def _extract_finding_from_results(self, results: List[Any]) -> Optional[str]:
		"""Extract key finding from action results.

		Args:
			results: Action results

		Returns:
			Finding string or None
		"""
		if not results:
			return None

		# Look for significant results
		for result in results:
			# Handle ActionResult objects
			if hasattr(result, 'error') and result.error:
				return f"Error: {result.error[:100]}"

			# Check for extracted data
			if hasattr(result, 'extracted_content') and result.extracted_content:
				if len(result.extracted_content) > 100:
					return f"Found: {result.extracted_content[:200]}..."

		return None


