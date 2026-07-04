
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
from dataclasses import dataclass, field
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
	MessageOrigin,
	make_control_message,
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
from agents.task.agent.core.llm_runner import LLMRunnerMixin
from agents.task.agent.core.memory_writer import MemoryWriterMixin
from agents.task.agent.core.step import StepMixin
from agents.task.agent.core.history_io import HistoryIOMixin
from agents.task.agent.core.logging_io import LoggingIOMixin
from agents.task.agent.core.safety_lifecycle import SafetyLifecycleMixin
from agents.task.agent.core.user_ingress import UserIngressMixin
from agents.task.agent.core.llm_provisioning import LLMProvisioningMixin
from agents.task.agent.core.model_introspection import ModelIntrospectionMixin
from agents.task.agent.core.loop_detection import LoopDetectionMixin
from agents.task.agent.core.resources import ResourceMixin
from agents.task.agent.core.session_metadata import SessionMetadataMixin
from agents.task.agent.core.error_recovery import ErrorRecoveryMixin
from agents.task.agent.core.step_telemetry import StepTelemetryMixin
from agents.task.agent.core.output_validation import OutputValidationMixin
from agents.task.agent.core.result_processing import ResultProcessingMixin
from agents.task.agent.core.next_action_internal import NextActionInternalMixin
from agents.task.agent.core.step_execution import StepExecutionMixin
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

load_dotenv()

# Generic logger for the module itself (not instances)
logger = get_task_logger('agent')


class RunLoopMixin:
	"""The agent run loop (run), split whole out of Agent so service.py drops toward
	its constructor core (P9). Agent composes RunLoopMixin; callers use agent.run()
	unchanged via MRO. Imports above are service.py's (incl. module-level logger)."""

	async def run(self, max_steps: int = 100, _continue_session: bool = False) -> AgentHistoryList:
			"""Run agent for the task.

			Runs the agent to complete the task set during initialization.

			Args:
				max_steps: Maximum number of steps to take

			Returns:
				List of history items
			"""
			# Log a truncated version of the task to avoid verbose logging
			truncated_task = self.task[:50] + "..." if len(self.task) > 50 else self.task
			self.logger.info(f'Running agent for task: {truncated_task}')
			
			# Track start time for metrics
			start_time = time.time()
			
			# Initialize step counter - will be managed by step() method
			self.state.n_steps = 0  # Start at 0, will be incremented by step() method
			
			# Session-scoped setup (session-start telemetry + hierarchical memory)
			# runs ONCE per session. Conversational turns (Conversation.respond with
			# _continue_session=True) reuse the same agent and skip it to avoid
			# telemetry spam + redundant H-MEM reloads. Tasks/HITL resume are unchanged.
			if not _continue_session:
				# Capture session start telemetry using TelemetryManager
				try:
					self.telemetry_manager.capture_session_start(
						task=self.task,
						model=self.model_name,
						agent_type=self.__class__.__name__,
						use_vision=self.use_vision,
						internal_planning=False
					)
				except Exception as e:
					self.logger.debug(f"Failed to capture session start telemetry: {e}")

				# Initialize and create/load hierarchical memory session (MANDATORY)
				try:
					# Initialize TaskContextManager if we created it
					if self._created_task_context_manager:
						await self.task_context_manager._initialize()
						self.logger.info("✅ Initialized dedicated TaskContextManager")

					# Try to load existing session first
					memory = self.task_context_manager.load_session(self.session_id, self.user_id)

					if not memory:
						# Create new session
						memory = self.task_context_manager.create_session(
							session_id=self.session_id,
							task=self.task,
							user_id=self.user_id
						)
						self.logger.info(f"📚 Created hierarchical memory session: {self.session_id}")
					else:
						self.logger.info(f"📚 Loaded hierarchical memory session: {self.session_id}")

				except Exception as e:
					# FAIL HARD - hierarchical memory is mandatory
					error_msg = f"CRITICAL: Cannot initialize hierarchical memory: {e}"
					self.logger.error(error_msg)
					raise RuntimeError(error_msg)

			try:
				# Do initial actions if provided
				if self.initial_actions:
					try:
						# Get fresh browser context for initial actions
						browser_context = await self.get_browser_context()
						result = await self.controller.multi_act(
							self.initial_actions,
							browser_context=browser_context,
							_page_extraction_llm=self.page_extraction_llm
						)
						self._last_result = result
						self.logger.info(f'Executed initial actions')
					except Exception as e:
						self.logger.error(f'Failed to execute initial actions: {str(e)}', exc_info=True)

				# Create gif if required
				if self.generate_gif:
					if isinstance(self.generate_gif, str):
						output_path = self.generate_gif
					else:
						if not self.save_conversation_path:
							# Use proper path handling for conversation path
							try:
								# Create proper path in the results directory
								from agents.task.path import pm
								self.save_conversation_path = str(pm().create_file_path(
									self.session_id,
									"logs",
									f"conversation_{str(time.time())}"
									))
							except Exception as e:
								self.logger.warning(f"Failed to create conversation path: {e}")

								# Fallback to direct utility functions
								try:
									from agents.task.path import pm
									self.save_conversation_path = str(pm().create_file_path(
										self.session_id,
										"logs",
										f"conversation_{str(time.time())}"
									))
								except ImportError:
									# Last resort - still use central PathManager to avoid nested path issues
									from agents.task.path import pm
									self.save_conversation_path = str(pm().create_file_path(
										self.session_id,
										"logs",
										f"conversation_{str(time.time())}"
									))
						else:
							# Fallback path handling using centralized PathManager to avoid directory issues
							from agents.task.path import pm
							self.save_conversation_path = str(pm().create_file_path(
								self.session_id,
								"logs",
								f"conversation_{str(time.time())}"
							))
						
						output_path = f'{self.save_conversation_path}.gif'
					self.logger.debug(f'Will create gif at {output_path}')

				# Loop until done or max steps reached
				max_failures_reached = False

				# Log the starting point
				self.logger.info(f"Starting task execution with max {max_steps} steps")

				# FIX 10: Add progress monitoring with heartbeat
				last_progress_time = time.time()
				PROGRESS_INTERVAL = 30  # Report progress every 30 seconds
				last_activity_time = time.time()
				# Use configurable stall timeout or default to 600 seconds
				STALL_TIMEOUT = self.stall_timeout_seconds if self.stall_timeout_seconds else 600

				# Import AgentStepInfo for proper step tracking
				from agents.task.agent.views import AgentStepInfo
				
				# Start stall monitor task
				if self.stall_timeout_seconds:
					self._stall_check_task = asyncio.create_task(self._stall_monitor_loop())
					self.logger.debug("Started stall monitor task")

					# Track task in orchestrator for proper cleanup
					if hasattr(self, 'orchestrator') and self.orchestrator:
						if hasattr(self.orchestrator, '_execution_tasks'):
							self.orchestrator._execution_tasks.append(self._stall_check_task)
							self.logger.debug("Registered stall monitor task with orchestrator for cleanup")

				# CRITICAL: Check for queued messages BEFORE starting execution loop
				# This enables continuous chat - when agent restarts after completion,
				# it picks up messages that were queued while it was idle
				initial_messages = await self._drain_user_messages()
				self.logger.info(f"Checked for queued messages at start: found {len(initial_messages)} messages")
				if initial_messages:
					self.logger.info(f"Processing {len(initial_messages)} queued messages:")
					for i, msg in enumerate(initial_messages):
						msg_text = msg.get('text', '')[:100]
						self.logger.info(f"  Message {i+1}: {msg_text}...")
					
					# ✅ FIX: Pass continuation context that was set by _drain_user_messages()
					# This triggers the strong "PRIORITY INPUT" signal in inject_user_guidance()
					session_context = getattr(self, '_session_continuation_context', None)
					
					# Inject messages into history so LLM sees them
					self.message_manager.inject_user_guidance(initial_messages, session_context=session_context)
					
					self.logger.info(
						f"✅ Injected {len(initial_messages)} messages with continuation context "
						f"(continuation={session_context.get('continuation') if session_context else False})"
					)

				# R1: count consecutive reply-only steps. A turn ends after a run of
				# them (see conversational_exit) so a chat answer doesn't loop and
				# re-greet; any productive step resets the run, so a real task is never
				# cut short. See agent/core/conversational_exit.py for the policy.
				from agents.task.agent.core.conversational_exit import (
					is_reply_only_step, should_conversational_exit,
				)
				consecutive_reply_steps = 0

				for step_num in range(max_steps):
					# Check for cancellation first
					if self._cancelled:
						self.logger.warning(f"❌ Agent execution cancelled at step {step_num}")
						break

					# Create step info with correct step number
					step_info = AgentStepInfo(step_number=step_num, max_steps=max_steps)

					# The agent's step() method will use this to update its internal counter
					self.logger.debug(f"Starting step {step_num + 1}/{max_steps}")

					# P6 session-registry heartbeat — refresh last_seen_at each step so the
					# periodic reaper (SESSION_REGISTRY_BACKEND=sqlite) spares this live
					# session. Best-effort / fail-open: a no-op for the in-process registry,
					# and must never break the run loop. Resolves the TaskAgent through the
					# existing container accessor (no new globals).
					try:
						from core.container import DependencyContainer
						_task_agent = DependencyContainer.get_instance().get_agent("task_agent")
						if _task_agent is not None and hasattr(_task_agent, "heartbeat_session"):
							_task_agent.heartbeat_session(self.session_id)
					except Exception:
						pass  # best-effort; never break the loop on heartbeat

					# FIX 10: Periodic progress reporting
					current_time = time.time()
					if current_time - last_progress_time > PROGRESS_INTERVAL:
						elapsed = current_time - start_time
						progress_pct = (step_num / max_steps) * 100
						self.logger.info(f"Progress: Step {step_num}/{max_steps} ({progress_pct:.1f}%), elapsed: {elapsed:.1f}s")

						# Also emit telemetry for progress monitoring
						if hasattr(self, 'session_data'):
							self.session_data['last_progress_update'] = {
								'timestamp': current_time,
								'step': step_num,
								'max_steps': max_steps,
								'elapsed_seconds': elapsed
							}
						last_progress_time = current_time

					# FIX 10: Check for stalled execution and take action
					if current_time - last_activity_time > STALL_TIMEOUT:
						self.logger.error(f"Agent appears stalled - no activity for {STALL_TIMEOUT} seconds", exc_info=True)
						# Emit stall warning telemetry
						if hasattr(self, 'session_data'):
							if 'stall_warnings' not in self.session_data:
								self.session_data['stall_warnings'] = []
							self.session_data['stall_warnings'].append({
								'timestamp': current_time,
								'step': step_num,
								'stall_duration': current_time - last_activity_time
							})

						# CRITICAL: Actually stop execution when stalled
						self.logger.error("Breaking execution due to stall timeout", exc_info=True)
						# Set error state before breaking
						self.state.consecutive_failures = self.max_failures
						# Mark as failed and break
						break

					if self._too_many_failures():
						max_failures_reached = True
						self.logger.warning(f"Too many consecutive failures ({self.state.consecutive_failures}/{self.max_failures})")
						break

					if await self._handle_control_flags():
						self.logger.info("Agent execution stopped by control flags")
						break

					# Check for context overflow before executing step
					if self._check_context_overflow():
						self.logger.info("Context overflow detected and mitigated - continuing")

					# Execute one step with the correct step info
					try:
						await self.step(step_info=step_info)
					except asyncio.CancelledError:
						self.logger.warning(f"❌ Step execution cancelled at step {step_num + 1}")
						break

					# FIX 10: Update last activity time after successful step
					last_activity_time = time.time()

					# UPGRADE (Dec 2025): Reset failure counter on successful step
					# This prevents accumulated failures from previous steps from affecting new steps
					if self._last_result and not any(r.error for r in self._last_result):
						if self.state.consecutive_failures > 0:
							self.logger.info(f"✅ Step succeeded, resetting failure counter (was {self.state.consecutive_failures})")
							self.state.consecutive_failures = 0
							self.state.reset_llm_errors()

					# Log step completion
					self.logger.debug(f"Completed step {step_num + 1}")

					# Note: n_steps is now managed by the step() method using step_info

					# Check for messages that arrived during step
					user_messages = await self._drain_user_messages()
					if user_messages:
						self.logger.info(f"Received {len(user_messages)} messages during step - injecting immediately")
						# Inject messages with context (set by _drain_user_messages)
						session_context = getattr(self, '_session_continuation_context', None)
						self.message_manager.inject_user_guidance(user_messages, session_context=session_context)
						self.logger.info(f"✅ Injected {len(user_messages)} messages into conversation")

					# R1: track consecutive reply-only steps. A reply-only step is one
					# whose every result is a non-blocking user-facing reply (no tool
					# ran); anything else (productive tool, planning turn, error, done)
					# resets the run.
					results = self._last_result or []
					if is_reply_only_step(results):
						consecutive_reply_steps += 1
					else:
						consecutive_reply_steps = 0

					# Check if the agent is done
					if results and any(result.is_done for result in results):
						# CO-F1: judge the FINAL answer, not an intermediate step. This
						# replaces the old top-of-loop check (which ran before step() on
						# the previous step's result and could never see the true final
						# answer, since a done() result broke the loop before that check
						# ever re-ran). Gated by AgentConfig.validate_output (env
						# VALIDATE_OUTPUT, default off) — off is byte-identical to legacy.
						# Bound: `continue` re-enters the SAME `for step_num in
						# range(max_steps)` loop, so a judge that always says "invalid"
						# still terminates at max_steps (no unbounded retry loop); the
						# consecutive_failures bump also lets `_too_many_failures()`
						# (checked at the top of the loop) cut it short sooner.
						if self.validate_output and not await self._validate_output():
							self.logger.warning('❌ Final output failed judge validation - continuing task')
							self.state.consecutive_failures += 1
							continue

						self.logger.info('✅ Task completed - agent called done()')

						# W2-C: at the turn boundary, maybe fork a background reviewer to
						# distil a durable skill from the run just completed. Productive =
						# the run actually did work (the final step wasn't reply-only).
						# Non-blocking + fail-open + gated BACKGROUND_REVIEW_ENABLED (off).
						# E6 (deliberate scope): this fires ONLY on the done()-completion
						# path — NOT on conversational-exit (reply-only = not productive,
						# correctly skipped) nor max-steps exhaustion (a non-converging /
						# incomplete run is poor skill material and would dilute the
						# "every N productive turns" cadence with failures). The cadence is
						# intentionally measured over COMPLETED productive runs only.
						try:
							self._maybe_spawn_background_review(
								turn_was_productive=not is_reply_only_step(results)
							)
						except Exception:
							pass

						# CHAT MODE: Break immediately, let session finalize
						# Continuous chat works via:
						# 1. Session status → "completed"
						# 2. User sends new message → queued in HITL manager
						# 3. User initiates new run → agent picks up queued messages from step 3896
						# 4. Agent continues with new task phase
						#
						# This eliminates race conditions from the old 1.5s wait pattern
						break

					# R1 conversational exit: the agent has produced a run of reply-only
					# steps without calling done() — it's chatting, not working. End the
					# turn so a greeting/chat answer doesn't loop and re-greet. A blocking
					# send or done() already ended the turn via is_done above.
					if should_conversational_exit(
						consecutive_reply_steps, getattr(self, '_is_sub_agent', False)
					):
						self.logger.info(
							f'💬 {consecutive_reply_steps} consecutive reply-only steps — '
							f'ending turn (R1 conversational exit)'
						)
						break
					

				if max_failures_reached:
					self.logger.warning(f'Too many consecutive failures')

				# Create output gif (PIL decode + GIF assembly are blocking — off-loop)
				if self.generate_gif and len(self.history.history) > 0:
					try:
						await asyncio.to_thread(self.create_history_gif, output_path)
					except Exception as e:
						self.logger.warning(f'Failed to create history gif: {str(e)}')

				# Call done callback if provided
				if self.register_done_callback:
					if callable(self.register_done_callback):
						self.register_done_callback(self.history)
					else:
						self.logger.warning("register_done_callback is set but not callable")
						
				# RAM optimization: Final memory cleanup after session completion
				try:
					import gc

					# Clean up telemetry buffers if available
					self.telemetry_manager.flush_buffers()
					
					# Force garbage collection after session completion
					gc.collect()
					
					self.logger.debug("Completed final memory cleanup")
				except Exception as cleanup_error:
					self.logger.debug(f"Error during final memory cleanup: {cleanup_error}")
						
				# Capture session completion telemetry
				try:
					end_time = time.time()
					duration = end_time - start_time
					
					# Determine if the session was successful
					success = False
					if self._last_result and any(result.is_done for result in self._last_result):
						success = True
						
					# Extract metrics about the session
					metrics = {
						"steps_completed": self.state.n_steps - 1,  # -1 because n_steps is incremented at the end of step
						"consecutive_failures": self.state.consecutive_failures,
						"max_failures_reached": max_failures_reached,
						"token_count": self.message_manager.get_token_count() if hasattr(self.message_manager, "get_token_count") else None,
						"model_name": self.model_name,
						# Single agent handles everything
					}
					
					# Get error message if any
					error_message = None
					if self._last_result and any(result.error for result in self._last_result):
						error_messages = [result.error for result in self._last_result if result.error]
						if error_messages:
							error_message = "; ".join(error_messages)
					
					# Get final result content if successful
					if success and self._last_result:
						for result in self._last_result:
							if result.is_done and result.extracted_content:
								metrics["final_result"] = result.extracted_content
								break
					
					# Capture completion event using TelemetryManager
					self.telemetry_manager.capture_session_end(
						success=success,
						steps=self.state.n_steps - 1,
						duration=duration,
						error_message=error_message,
						metrics=metrics
					)
				except Exception as e:
					self.logger.debug(f"Failed to capture session completion telemetry: {e}")
					
				# Log final step count and result
				if self._last_result and any(result.is_done for result in self._last_result):
					self.logger.info(f"Task completed successfully in {self.state.n_steps-1} steps")
				else:
					self.logger.warning(f"Task not completed after {self.state.n_steps-1} steps")

				# Save hierarchical memory session
				if self.task_context_manager:
					try:
						self.task_context_manager.save_session(
							session_id=self.session_id,
							user_id=self.user_id
						)
						self.logger.info(f"💾 Saved hierarchical memory for session: {self.session_id}")
					except Exception as e:
						self.logger.warning(f"Failed to save hierarchical memory: {e}")

				# Session cost tracking - credits already deducted per-call by usage_tracker
				# (usage_meter, the pre-usage_tracker fallback, was retired in C5 — see
				# docs/superpowers/plans/2026-07-02-polyrob-console-web-app-finalization-C-payments.md)
				if self.user_id:
					try:
						if self.usage_tracker:
							breakdown = await self.usage_tracker.get_session_breakdown(self.session_id)
							self.logger.info(
								f"💰 Session {self.session_id} summary: "
								f"{breakdown['total_credits_charged']} credits charged, "
								f"${breakdown['total_user_cost_usd']:.4f} user cost"
							)
					except Exception as e:
						self.logger.error(f"Failed to get session cost summary: {e}")

				# CO-F7: the once-per-SESSION bootstrap flag (episodic digest +
				# continuity bridge, see memory_prefetch.py) is now set from inside
				# step.py, right after the injectors are actually invoked on
				# n_steps == 1 (see the call site there). It is deliberately NOT
				# set here at the end of run() — a turn-1 run() that breaks BEFORE
				# step() ever executes (cancellation, resumed-from-done/stopped,
				# too-many-failures) must leave the flag False so a later real
				# turn still gets its one shot at the digest + continuity bridge.

				return self.history
			finally:
				# Clean up stall monitor task
				if hasattr(self, '_stall_check_task') and self._stall_check_task:
					self._stall_check_task.cancel()
					try:
						await self._stall_check_task
					except asyncio.CancelledError:
						pass
					self.logger.debug("Cancelled stall monitor task")

				# Release browser context for this agent
				if self.orchestrator and self.orchestrator.browser_manager:
					try:
						await self.orchestrator.browser_manager.release_context(self.agent_id, close=True)
						self.logger.info(f"Released browser context for {self.agent_id}")
					except Exception as e:
						self.logger.debug(f"Error releasing browser context: {e}")

				self.telemetry_manager.capture_event(
					AgentEndTelemetryEvent(
						agent_id=self.agent_id,
						success=self.history.is_done(),
						steps=self.state.n_steps,
						max_steps_reached=self.state.n_steps >= max_steps,
						errors=self.history.errors(),
					)
				)


