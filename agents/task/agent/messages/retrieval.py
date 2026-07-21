"""Message-retrieval / LLM-request-assembly mixin (roadmap P9; from message_manager/service.py).

get_messages / get_messages_for_llm (foundation + history assembly with the pinned
system/task/skill/H-MEM messages), ephemeral push, LLM timeout/param helpers, and
message-structure logging. Split out so MessageManager service.py drops under 700L.
MessageManager composes MessageRetrievalMixin; call sites (step.py, llm_runner.py,
logging_io, output_validation, tests) unchanged via MRO.
"""
from __future__ import annotations

import json
import logging
from collections import deque
from typing import Any, Dict, List, Optional

from modules.llm.messages import (
    AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage,
    MessageOrigin, make_control_message,
)
from agents.task.constants import hmem_tail_placement


class MessageRetrievalMixin:
	"""Message retrieval + LLM-request assembly for MessageManager."""

	def get_messages(self) -> List[BaseMessage]:
		"""Get current message list with foundation messages always prepended.
		
		CRITICAL FIX: SystemMessage and InitialTask are stored separately to prevent deque eviction.
		This method always returns [SystemMessage, InitialTask (if exists), ...conversation] to ensure
		the LLM always receives brain state instructions and knows the original task.
		
		FOUNDATION (never evicted):
		  [0] SystemMessage - Instructions, format, workflow
		  [1] Initial Task - Original user request (if new session)
		
		CONVERSATION (deque, last 20 messages):
		  [2+] Recent messages (auto-evicts oldest)
		"""
		
		# Build foundation messages (stored separately, never evicted)
		foundation = [self._system_message]

		# Runtime identity (model/provider actually running), pinned right after the
		# system message so the agent answers "what model are you" from this line, not
		# by reading env/config. Empty/unset => skipped. Matches get_messages_for_llm().
		if getattr(self, '_runtime_identity_message', None) is not None:
			foundation.append(self._runtime_identity_message)

		# 014-C1: <environment> block (where the agent lives), pinned after runtime
		# identity and before self-context, matching get_messages_for_llm().
		if getattr(self, '_environment_message', None) is not None:
			foundation.append(self._environment_message)

		# polyrob Phase C: SOUL/IDENTITY self-context, pinned right after the system
		# message (identity precedes task), matching get_messages_for_llm().
		if getattr(self, '_self_context_message', None) is not None:
			foundation.append(self._self_context_message)

		# C9: project context (CLAUDE.md/AGENTS.md/.cursorrules) pinned AFTER
		# self-context and BEFORE the initial task, matching get_messages_for_llm().
		if getattr(self, '_project_context_message', None) is not None:
			foundation.append(self._project_context_message)

		if self._initial_task_message is not None:
			foundation.append(self._initial_task_message)

		# Skills (PR13) - pinned in the foundation after the task, matching
		# get_messages_for_llm() so logs and guidance-position math see the same layout.
		if getattr(self, '_skill_message', None) is not None:
			foundation.append(self._skill_message)

		# S1 (dynamic tool rig): <tool-catalog> pinned after skills, matching
		# get_messages_for_llm().
		if getattr(self, '_tool_catalog_message', None) is not None:
			foundation.append(self._tool_catalog_message)

		# Get conversation messages from history (recent messages in deque)
		conversation_messages = [m.message for m in self.history.messages]

		# Assemble: Foundation + Conversation
		msg = foundation + conversation_messages

		# Debug logging with correct indexing
		total_input_tokens = self._system_message_tokens + self._initial_task_tokens
		
		foundation_count = len(foundation)
		self.logger.debug(f'Messages: {foundation_count} foundation (separate) + {len(self.history.messages)} conversation (deque)')
		self.logger.debug(f'[0] SystemMessage - {self._system_message_tokens} tokens (separate)')
		
		if self._initial_task_message:
			self.logger.debug(f'[1] InitialTask - {self._initial_task_tokens} tokens (separate)')
		
		for i, m in enumerate(self.history.messages):
			total_input_tokens += m.metadata.input_tokens
			msg_type = m.message.__class__.__name__
			msg_index = foundation_count + i  # Correct index accounting for foundation
			
			# Add detailed logging for tool calls and responses
			if isinstance(m.message, AIMessage) and hasattr(m.message, 'tool_calls') and m.message.tool_calls:
				tool_call_ids = [tc.get('id') for tc in m.message.tool_calls if 'id' in tc]
				self.logger.debug(f'[{msg_index}] {msg_type} - {m.metadata.input_tokens} tokens - tool_calls: {tool_call_ids}')
			elif isinstance(m.message, ToolMessage) and hasattr(m.message, 'tool_call_id'):
				self.logger.debug(f'[{msg_index}] {msg_type} - {m.metadata.input_tokens} tokens - tool_call_id: {m.message.tool_call_id}')
			else:
				self.logger.debug(f'[{msg_index}] {msg_type} - {m.metadata.input_tokens} tokens')
				
		self.logger.debug(f'Total tokens: {total_input_tokens} (foundation: {self._system_message_tokens + self._initial_task_tokens}, conversation: {total_input_tokens - self._system_message_tokens - self._initial_task_tokens})')

		return msg


	def push_ephemeral_message(self, message: BaseMessage) -> None:
		"""Queue a one-shot message to be included on the next LLM call only.

		This does not modify history or token counts and is cleared after use.
		"""
		try:
			# Apply sensitive data filtering if configured, or the HISTORY_SECRET_SCRUB
			# backstop (default ON) which scrubs unregistered secrets with no allowlist.
			if self.sensitive_data or getattr(self, "_history_secret_scrub", True):
				message = self._filter_sensitive_data(message)
			# B3 (2026-07-13 correspondent review): bound the one-shot queue — the HITL
			# queue has backpressure (MAX_QUEUED_MESSAGES) but this rail had none, so a
			# correspondent flood could inflate the next prompt without limit. Overflow
			# drops the OLDEST entry (the newest message is the most relevant).
			import os
			try:
				cap = int(os.environ.get('MAX_EPHEMERAL_MESSAGES', '30'))
			except (TypeError, ValueError):
				cap = 30
			while cap > 0 and len(self._ephemeral_messages) >= cap:
				dropped = self._ephemeral_messages.pop(0)
				self.logger.warning(
					f"Ephemeral queue full ({cap}); dropping oldest "
					f"{type(dropped).__name__} to admit the new message")
			self._ephemeral_messages.append(message)
			self.logger.debug(f"Queued ephemeral message of type {type(message).__name__}")
		except Exception as e:
			self.logger.debug(f"Failed to queue ephemeral message: {e}")

	def commit_ephemeral_consumption(self) -> None:
		"""P2-14: drop the pending (delivered) ephemerals. Call once the LLM has
		responded — even a parse-failed response means they WERE delivered."""
		self._ephemeral_pending = []

	def restore_ephemeral_on_failure(self) -> None:
		"""P2-14: re-queue ephemerals consumed for an LLM call that ultimately failed
		(transient invoke error), so the next step re-includes them instead of losing
		a correspondent reply / RECALL forever."""
		pending = getattr(self, '_ephemeral_pending', None)
		if pending:
			self._ephemeral_messages = pending + self._ephemeral_messages
			self._ephemeral_pending = []

	def get_messages_for_llm(self, consume_ephemeral: bool = True) -> List[BaseMessage]:
		"""Get messages ready for LLM call with all processing applied.

		Args:
			consume_ephemeral: when True (default, the real send-to-LLM path), any
				queued one-shot ephemeral messages are cleared after being included
				exactly once. Diagnostic/observational callers (e.g. context-usage
				logging) must pass False so they peek without draining a one-shot
				message (correspondent reply, deferral notice, memory-writer note)
				before the real LLM call ever sees it (CX-H3).

		✅ FIX (Nov 5, 2025): Refactored to protect H-MEM from repair

		ARCHITECTURE:
		1. Foundation (NEVER repaired): System message, Task message, H-MEM
		2. Conversation (subject to repair): Recent messages, ephemeral messages
		3. Repair ONLY conversation (foundation untouched)
		4. Combine: foundation + repaired conversation
		5. Model-specific handling (e.g., deepseek merging)
		6. Return messages: Ready for LLM provider

		This ensures H-MEM can never be lost because it's in the protected foundation layer.
		"""
		# Build foundation (never repaired): system, task, memory
		foundation = []

		# 1.1: System message (always first - instructions)
		foundation.append(self._system_message)

		# 1.1a: Runtime identity (model/provider actually running), pinned right after
		# the system prompt so the agent answers its model from THIS line, not by
		# reading env/config. Kept out of the system prompt (cacheable). Unset => skip.
		if getattr(self, '_runtime_identity_message', None) is not None:
			foundation.append(self._runtime_identity_message)

		# 1.1a-bis (014-C1): <environment> block — where the agent lives. Pinned
		# after runtime identity, before self-context. Unset (server) => skipped.
		if getattr(self, '_environment_message', None) is not None:
			foundation.append(self._environment_message)

		# 1.1b: polyrob Phase C - SOUL/IDENTITY self-context, pinned in the foundation
		# right after the system prompt (NOT embedded in it, so the system prompt stays
		# stable/cacheable). Identity precedes the task. Empty/unset => skipped.
		if getattr(self, '_self_context_message', None) is not None:
			foundation.append(self._self_context_message)

		# 1.1c: C9 - project context (CLAUDE.md/AGENTS.md/.cursorrules), pinned AFTER
		# self-context and BEFORE the initial task. CLI-only; empty/unset => skipped.
		if getattr(self, '_project_context_message', None) is not None:
			foundation.append(self._project_context_message)

		# 1.2: Initial task message (if exists - defines what user wants)
		if self._initial_task_message is not None:
			foundation.append(self._initial_task_message)

		# 1.2b: Skills (PR13) - pinned in the foundation, NOT the system prompt, so the
		# system prompt stays stable/cacheable and skills read as a distinct block.
		if getattr(self, '_skill_message', None) is not None:
			foundation.append(self._skill_message)

		# 1.2c: S1 dynamic tool rig - the honest <tool-catalog> block, pinned after
		# skills for the same cacheability rationale. Empty/unset => skipped.
		if getattr(self, '_tool_catalog_message', None) is not None:
			foundation.append(self._tool_catalog_message)

		# 1.3: Hierarchical memory.
		# Placement (Phase 0.1): legacy = pinned in the foundation ahead of the
		# conversation; tail = held here and appended as a dynamic SUFFIX after the
		# conversation (see combine step below). The H-MEM block changes every step,
		# so in the foundation prefix it broke the prompt cache for everything after
		# it; as a tail suffix the stable foundation + growing conversation stay a
		# cacheable prefix. Either way it is NEVER subject to tool-sequence repair.
		memory_injected = False
		hmem_msg = None  # held for tail placement
		tail_placement = hmem_tail_placement()

		# FIX #1: Explicit warnings when H-MEM cannot be injected (was silently skipped)
		if not self.task_context_manager:
			self.logger.warning(
				"⚠️ H-MEM DISABLED: TaskContextManager not available. "
				"Agent will run without hierarchical memory context."
			)
		elif not self.session_id:
			self.logger.warning(
				"⚠️ H-MEM DISABLED: session_id not set. "
				"Agent will run without hierarchical memory context."
			)
		else:
			try:
				context = self.task_context_manager.get_context_injection(self.session_id)
				if context:
					# Chat-schema: tag injected hierarchical memory as MEMORY control
					# content (origin + <session-memory> envelope), not a user turn.
					context_msg = make_control_message(context, MessageOrigin.MEMORY)
					if tail_placement:
						hmem_msg = context_msg  # appended after conversation below
					else:
						foundation.append(context_msg)
					memory_injected = True
					context_tokens = len(context.split()) * 1.3  # Rough estimate
					where = "tail suffix" if tail_placement else "foundation"
					self.logger.debug(f"✅ H-MEM in {where} (protected from repair) - ~{context_tokens:.0f} tokens")
				else:
					self.logger.debug("H-MEM returned empty context (session may be new)")
			except Exception as e:
				self.logger.warning(f"Failed to inject hierarchical memory: {e}")


		# Build conversation (may be repaired)
		conversation = []

		# 2.1: Recent conversation (from history deque)
		# Use all available messages (up to MAX_RECENT_MESSAGES=10)
		conversation_messages = [m.message for m in self.history.messages]
		conversation.extend(conversation_messages)

		# 2.2: Include any queued ephemeral messages (one-shot)
		if hasattr(self, '_ephemeral_messages') and self._ephemeral_messages:
			conversation.extend(self._ephemeral_messages)
			self.logger.debug(f"Included {len(self._ephemeral_messages)} ephemeral messages")
			if consume_ephemeral:
				# P2-14: don't drop the one-shot ephemerals (correspondent replies,
				# RECALL) at assembly time — the LLM call hasn't happened yet, so a
				# transient invoke failure would lose them forever (correspondent data
				# has no other delivery path). MOVE them to a pending buffer instead:
				# commit_ephemeral_consumption() drops them once the LLM has responded,
				# restore_ephemeral_on_failure() re-queues them if the call ultimately
				# failed. Wired in the next_action wrapper (output_validation.py).
				self._ephemeral_pending = list(self._ephemeral_messages)
				self._ephemeral_messages.clear()

		self.logger.debug(
			f"Message composition: foundation={len(foundation)} (system=1, task={1 if self._initial_task_message else 0}, "
			f"h_mem={1 if memory_injected else 0}), conversation={len(conversation)}"
		)

		# Repair tool sequences in conversation
		conversation = self._validate_and_repair_tool_sequences(conversation)
		self.logger.debug(f"After repair: foundation={len(foundation)}, conversation={len(conversation)}")

		# B2/B3 (Reference-parity media hygiene, see docs/REFERENCE_VS_ROB_CONTEXT_SYSTEM_2026-06.md §9):
		# dedup byte-identical tool outputs, and strip base64 from every image-bearing turn
		# EXCEPT the most recent. Conversation only — foundation (system/task/H-MEM/skills)
		# is never touched. Both are idempotent and fail-soft no-ops when nothing matches.
		try:
			from agents.task.agent.messages.filters import dedup_tool_results, strip_historical_media
			conversation = dedup_tool_results(conversation)
			conversation = strip_historical_media(conversation)
		except Exception as e:
			self.logger.debug(f"media-hygiene pass skipped: {e}")

		# Combine foundation + conversation (+ H-MEM tail suffix when relocated).
		# The tail suffix goes AFTER the repaired conversation so it never participates
		# in tool-sequence repair (same protection it had in the foundation), while
		# keeping the foundation+conversation prefix cache-stable across steps.
		messages = foundation + conversation
		if hmem_msg is not None:
			messages.append(hmem_msg)

		# Model-specific handling
		# Message types imported at module level from modules.llm.messages
		if self.model_name == 'deepseek-reasoner':
			messages = self.merge_successive_messages(messages, HumanMessage)
			messages = self.merge_successive_messages(messages, AIMessage)
			self.logger.debug("Applied deepseek-reasoner message merging")

		# Step 5: Log the final message structure for debugging
		if self.use_native_tools:
			self._log_message_structure(messages)

		# Step 6: Apply non-native conversion if not using native tools
		if not self.use_native_tools:
			messages = self.convert_messages_for_non_function_calling_models(messages)

		# ✅ FIX #1.1: CRITICAL ASSERTION - Ensure system message is present
		# This validates that the system message with brain state instructions reaches the LLM
		# SystemMessage imported at module level from modules.llm.messages
		if not messages or not isinstance(messages[0], SystemMessage):
			error_msg = (
				"CRITICAL ERROR: System message missing from message history! "
				"This will cause the LLM to not return brain state JSON. "
				f"Message count: {len(messages)}, "
				f"First message type: {type(messages[0]).__name__ if messages else 'None'}"
			)
			self.logger.error(error_msg)

			# Check if system message exists anywhere in history
			system_messages = [m for m in messages if isinstance(m, SystemMessage)]
			if system_messages:
				self.logger.error(f"System message found at index {messages.index(system_messages[0])}, should be at index 0")
			else:
				self.logger.error("No system message found anywhere in message list")

			# FAIL FAST - don't continue with broken state
			from core.exceptions import AgentError
			raise AgentError(error_msg)

		# Verify system message contains brain state instructions
		system_content = str(messages[0].content)
		if "brain state" not in system_content.lower() and "text content" not in system_content.lower():
			self.logger.warning(
				"System message present but may not contain brain state instructions. "
				f"Content preview: {system_content[:200]}"
			)

		self.logger.debug(f"Final messages for LLM: {len(messages)} messages")
		self.logger.debug("✅ System message validation passed")
		return messages


	def calculate_llm_timeout(self, tool_count: int = 0, use_vision: bool = False) -> float:
		"""Calculate dynamic timeout based on CURRENT token count and complexity.

		Args:
			tool_count: Number of tools available to the LLM
			use_vision: Whether vision is enabled

		Returns:
			Timeout in seconds, scaled based on complexity
		"""
		import os

		# Use current token count (no parameter needed!)
		estimated_tokens = self.history.total_tokens + self._system_message_tokens + self._initial_task_tokens

		# Base timeout on token count
		# INCREASED: Complex code generation can take 120-180s even with small contexts
		# The bottleneck is LLM generation time, not just token processing
		if estimated_tokens < 5000:
			timeout = 120.0  # 2 minutes for small contexts (code gen can be slow)
		elif estimated_tokens < 10000:
			timeout = 150.0  # 2.5 minutes for medium contexts
		elif estimated_tokens < 30000:
			timeout = 180.0  # 3 minutes for large contexts
		elif estimated_tokens < 50000:
			timeout = 240.0  # 4 minutes for very large contexts
		elif estimated_tokens < 100000:
			timeout = 360.0  # 6 minutes for huge contexts
		elif estimated_tokens < 500000:
			timeout = 480.0  # 8 minutes for massive contexts
		else:
			timeout = 600.0  # 10 minutes max for 500K+ contexts

		# Adjust for tool count (large tool sets add processing time)
		if tool_count > 20:
			timeout += 30.0  # Extra 30s for 20+ tools
			self.logger.debug(f"Added 30s timeout for {tool_count} tools")
		elif tool_count > 10:
			timeout += 15.0  # Extra 15s for 10-20 tools
			self.logger.debug(f"Added 15s timeout for {tool_count} tools")

		# Adjust for vision (image processing adds time)
		if use_vision:
			timeout += 20.0  # Extra 20s for vision processing
			self.logger.debug(f"Added 20s timeout for vision")

		# Minimum timeout for first call (model loading, code generation, etc.)
		# Complex code generation tasks can take 120-180s even with small prompts
		timeout = max(timeout, 120.0)  # At least 2 minutes

		# Add environment override for debugging
		env_timeout = os.getenv('AUTOV2_LLM_TIMEOUT_OVERRIDE')
		if env_timeout:
			try:
				timeout = float(env_timeout)
				self.logger.info(f"Using environment timeout override: {timeout:.0f}s")
			except ValueError:
				self.logger.warning(f"Invalid timeout override value: {env_timeout}")

		self.logger.info(f"Context: {estimated_tokens:,} tokens, {tool_count} tools, vision={use_vision} → {timeout:.0f}s timeout")
		return timeout

	def remove_last_state_message(self) -> None:
		"""Remove the last state message from history to avoid bloat.

		State messages are typically HumanMessages containing browser state or
		other contextual information. This method removes them after processing
		to prevent history bloat.
		"""
		# SECURITY FIX: Protect iteration and removal with lock
		with self._history_lock:
			if not self.history.messages:
				return

			# CX-H1: tag-first removal. A shape-based scan ("list content OR
			# contains 'Current url:'") can't tell a minimal (non-browser)
			# state message apart from a user's multimodal image turn — both
			# can be the nearest match, and the wrong one gets deleted.
			# add_state_message now stamps every state message it builds with
			# metadata["state_message"]=True, so scan for the tag first across
			# the WHOLE history. Only if no tagged message exists anywhere
			# (e.g. history loaded from disk that predates the tag) fall back
			# once to the old shape heuristic.
			for i in range(len(self.history.messages) - 1, -1, -1):
				msg = self.history.messages[i].message if hasattr(self.history.messages[i], 'message') else self.history.messages[i]
				if isinstance(msg, HumanMessage) and (getattr(msg, "metadata", None) or {}).get("state_message"):
					self.history.remove_message(i)
					self.logger.debug(f"Removed tagged state message at index {i}")
					return

			# Legacy fallback: no tagged message found anywhere in history.
			for i in range(len(self.history.messages) - 1, -1, -1):
				msg = self.history.messages[i].message if hasattr(self.history.messages[i], 'message') else self.history.messages[i]
				if isinstance(msg, HumanMessage):
					# Check if it looks like a state message (pre-tag heuristic)
					if isinstance(msg.content, list) or (isinstance(msg.content, str) and 'Current url:' in msg.content):
						self.history.remove_message(i)
						self.logger.debug(f"Removed state message (legacy shape fallback) at index {i}")
						return
	

	def get_llm_parameters(self) -> dict:
		"""Extract LLM parameters for logging and telemetry.

		Returns:
			Dictionary of LLM parameters
		"""
		params = {}

		# Get class name for type detection
		class_name = self.llm.__class__.__name__

		# Extract common parameters based on LLM type
		if 'OpenAI' in class_name:
			# OpenAI parameters
			for attr in ['model_name', 'temperature', 'max_tokens', 'top_p',
			             'frequency_penalty', 'presence_penalty', 'timeout', 'seed']:
				if hasattr(self.llm, attr):
					params[attr if attr != 'model_name' else 'model'] = getattr(self.llm, attr)

		elif 'Anthropic' in class_name:
			# Anthropic parameters
			for attr in ['model_name', 'temperature', 'max_tokens', 'top_k', 'top_p', 'timeout']:
				if hasattr(self.llm, attr):
					params[attr if attr != 'model_name' else 'model'] = getattr(self.llm, attr)

		elif 'Google' in class_name or 'Gemini' in class_name:
			# Google parameters
			for attr in ['model_name', 'temperature', 'max_tokens', 'top_k', 'top_p']:
				if hasattr(self.llm, attr):
					params[attr if attr != 'model_name' else 'model'] = getattr(self.llm, attr)

		else:
			# Generic parameters
			for attr in ['model_name', 'model', 'temperature', 'max_tokens']:
				if hasattr(self.llm, attr):
					value = getattr(self.llm, attr)
					if value is not None:
						params[attr] = value

		# Add MessageManager-specific parameters
		params['use_native_tools'] = self.use_native_tools
		params['max_input_tokens'] = self.max_input_tokens
		params['provider'] = self.provider_name

		return params


	def _log_message_structure(self, messages: List[BaseMessage]) -> None:
		"""Log message structure for debugging native tools."""
		if self.logger.level > logging.DEBUG:
			return

		self.logger.debug("[NATIVE_TOOLS] Final message structure:")
		for i, msg in enumerate(messages[-10:]):  # Last 10 messages
			if isinstance(msg, SystemMessage):
				self.logger.debug(f"  [{i}] SystemMessage")
			elif isinstance(msg, HumanMessage):
				self.logger.debug(f"  [{i}] HumanMessage: {msg.content[:50]}...")
			elif isinstance(msg, AIMessage):
				if hasattr(msg, 'tool_calls') and msg.tool_calls:
					self.logger.debug(f"  [{i}] AIMessage with {len(msg.tool_calls)} tool_calls")
				else:
					self.logger.debug(f"  [{i}] AIMessage: {msg.content[:50]}...")
			elif isinstance(msg, ToolMessage):
				self.logger.debug(f"  [{i}] ToolMessage for {msg.tool_call_id}: {msg.content[:50]}...")


	# === Context overflow recovery ===


	



