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
from agents.task.agent.messages.foundation_layers import foundation_messages


def drop_usage_anchor(manager: Any, reason: str = "") -> None:
	"""F6 seam: drop the provider-usage anchor because the history was cut.

	A module-level delegator rather than a direct ``self.reset_usage_anchor(...)``
	call, because ``CompactorMixin``/``PersistenceMixin`` are composed STANDALONE
	by several test harnesses that do not compose ``MessageRetrievalMixin``. A
	missing anchor is not an error — the fingerprint would have failed the anchor
	closed anyway; this call just keeps the reason in the log.
	"""
	reset = getattr(manager, "reset_usage_anchor", None)
	if callable(reset):
		try:
			reset(reason)
		except Exception:  # pragma: no cover - an observation never fails a run
			pass


class MessageRetrievalMixin:
	"""Message retrieval + LLM-request assembly for MessageManager."""
	# F29: empty slots so the composed MessageManager keeps its own
	# __slots__ and never grows a __dict__. This mixin omitted it, which
	# silently defeated the whole tuple.
	__slots__ = ()


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
		
		# Build foundation messages (stored separately, never evicted). 060 WS-2:
		# the order is FOUNDATION_LAYERS — the SAME table get_messages_for_llm() and
		# the F13 replay read — so logs and guidance-position math see the wire layout.
		foundation = foundation_messages(self)

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


	def push_control_message(self, message: BaseMessage) -> None:
		"""Append a control message to the DURABLE history (061).

		The one-shot ``push_ephemeral_message`` rides exactly one LLM call; the
		owner-thread tail must survive a 2-4 step turn (the turn that answers
		"explain better" reads it on step 3), so it lands in history like any
		other message and compacts with it. Fail-open.
		"""
		try:
			self._add_message_with_tokens(message, _internal=True)
		except Exception as e:
			self.logger.debug(f"Failed to add control message: {e}")

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

	def drop_queued_state_messages(self) -> int:
		"""F16(ii): remove any QUEUED (or in-flight) state message from the rail.

		The state message describes THIS step. A call that failed transiently
		re-queues its ephemerals (``restore_ephemeral_on_failure``) so a
		correspondent reply is never lost — but a stale state message must not ride
		along beside the fresh one, or the model reads two "CURRENT STATE" blocks.
		Called by ``add_state_message`` before it queues the new one, so the
		invariant is simply: at most one state message, and it is the current one.

		Returns the number dropped (0 on the ordinary path).
		"""
		dropped = 0
		for name in ('_ephemeral_messages', '_ephemeral_pending'):
			queue = getattr(self, name, None)
			if not queue:
				continue
			kept = [m for m in queue
			        if not (getattr(m, "metadata", None) or {}).get("state_message")]
			dropped += len(queue) - len(kept)
			setattr(self, name, kept)
		if dropped:
			self.logger.debug(f"Dropped {dropped} stale state message(s) from the ephemeral rail")
		return dropped

	def commit_ephemeral_consumption(self) -> None:
		"""P2-14: drop the pending (delivered) ephemerals. Call once the LLM has
		responded — even a parse-failed response means they WERE delivered."""
		self._ephemeral_pending = []

	def restore_ephemeral_on_failure(self) -> None:
		"""P2-14: re-queue ephemerals consumed for an LLM call that ultimately failed
		(transient invoke error), so the next step re-includes them instead of losing
		a correspondent reply / RECALL forever.

		Also called for a step that produced NO action (2026-10-14 review C-1): a
		tool-free planning turn did not use the recall / ``<live-health>`` / digest
		it was shown, so the acting step must see them again.

		A per-call message (the format hint, tagged ``per_call``; the state
		message) is NOT re-queued: the next call builds a fresh one, and a
		re-queued copy would ride beside it twice."""
		pending = getattr(self, '_ephemeral_pending', None)
		if pending:
			def _per_call(m) -> bool:
				md = getattr(m, "metadata", None) or {}
				return bool(md.get("per_call") or md.get("state_message"))
			keep = [m for m in pending if not _per_call(m)]
			self._ephemeral_messages = keep + self._ephemeral_messages
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
		5. Return messages: Ready for LLM provider

		This ensures H-MEM can never be lost because it's in the protected foundation layer.
		"""
		# Build foundation (never repaired). 060 WS-2: ONE table
		# (foundation_layers.FOUNDATION_LAYERS) is the order — system prompt, runtime
		# identity, <environment>, self-context, project context, the initial task,
		# skills, <tool-catalog>. Each block stays out of the system prompt so the
		# prompt is cacheable; an unset block is skipped.
		foundation = foundation_messages(self)

		# 1.3: Hierarchical memory.
		# Placement (Phase 0.1): held here and appended as a dynamic SUFFIX after
		# the conversation (see combine step below). The H-MEM block changes every
		# step, so in the foundation prefix it broke the prompt cache for everything
		# after it; as a tail suffix the stable foundation + growing conversation
		# stay a cacheable prefix. It is NEVER subject to tool-sequence repair.
		# 060 WS-2: VOLATILE H-MEM rides the tail, outside the cached prefix.
		memory_injected = False
		hmem_msg = None  # appended after the conversation

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
					hmem_msg = context_msg  # appended after conversation below
					memory_injected = True
					context_tokens = len(context.split()) * 1.3  # Rough estimate
					self.logger.debug(f"✅ H-MEM in tail suffix (protected from repair) - ~{context_tokens:.0f} tokens")
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
		# dedup byte-identical tool outputs, and retire old base64 images in batches
		# (F7 step function: MEDIA_KEEP_MAX / MEDIA_RETIRE_BATCH / MEDIA_KEEP_FLOOR).
		# Conversation only — foundation (system/task/H-MEM/skills) is never touched.
		# Both are idempotent and fail-soft no-ops when nothing matches.
		try:
			from agents.task.agent.messages.filters import dedup_tool_results, strip_historical_media
			conversation = dedup_tool_results(conversation)
			conversation = strip_historical_media(conversation)
		except Exception as e:
			self.logger.debug(f"media-hygiene pass skipped: {e}")

		# The H-MEM tail suffix goes AFTER the repaired conversation so it never
		# participates in tool-sequence repair (same protection it had in the
		# foundation), while keeping the foundation+conversation prefix cache-stable.
		if hmem_msg is not None:
			conversation.append(hmem_msg)

		messages = foundation + conversation

		# F12: record where the foundation ENDS on the provider wire, so the
		# Anthropic breakpoint placer can pin one cache_control marker there.
		# SystemMessages are excluded because Anthropic carries the system prompt
		# in its own `system` parameter — the wire `messages` array starts at the
		# first non-system foundation row.
		self._last_foundation_len = sum(
			1 for m in foundation if not isinstance(m, SystemMessage))

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

		# F6: remember whether THIS request carried the H-MEM tail, so a usage
		# anchor taken from its prompt_tokens does not double-count the block.
		self._hmem_in_last_prompt = bool(memory_injected)

		self.logger.debug(f"Final messages for LLM: {len(messages)} messages")
		self.logger.debug("✅ System message validation passed")
		return messages


	def note_call_usage(self, *, output_tokens: int = 0, input_tokens: int = 0,
	                    cached_tokens: int = 0, cache_creation_tokens: int = 0) -> None:
		"""Record one completed LLM call's shape (057 WS-B).

		Fed from the ONE billing choke point in ``next_action_internal`` — the
		same extraction the usage record already performs, so there is no second
		read of the provider's usage block. Two facts are kept, both tiny:
		the last TWO output sizes (what :meth:`calculate_llm_timeout` scales on)
		and the last call's UNCACHED input (80% of prod input is cache-served at
		1/50th price and also ~1/50th of the latency, so counting the whole
		prompt overstates the wait).

		F6 extends the SAME write-back (there is no second reader of the usage
		block) with the USAGE ANCHOR: the provider's own ``prompt_tokens`` for the
		request that just went out, the number of history messages it covered, and
		a cheap fingerprint of those messages. ``get_actual_token_count`` then
		reports ``anchor + estimate(the delta since)`` instead of estimating the
		whole prompt from ``len(text)/N`` — an estimator with no tokenizer for
		Anthropic, which decides the 85 %/95 % compaction band and the pre-call
		safety check. The fingerprint FAILS CLOSED: any splice, eviction or
		compaction of the covered prefix invalidates the anchor and the pure
		estimate is used again, byte-identical to the pre-F6 number.

		Also records the last call's cache split so ``context_usage()`` can show
		what the provider actually served from cache.
		"""
		try:
			if output_tokens and output_tokens > 0:
				recent = list(getattr(self, "_recent_output_tokens", []))
				recent.append(int(output_tokens))
				self._recent_output_tokens = recent[-2:]
			if input_tokens and input_tokens > 0:
				self._last_uncached_input = max(0, int(input_tokens) - int(cached_tokens or 0))
				self._set_usage_anchor(int(input_tokens))
			self._last_call_usage = {
				"prompt_tokens": int(input_tokens or 0),
				"completion_tokens": int(output_tokens or 0),
				"cached_tokens": int(cached_tokens or 0),
				"cache_creation_tokens": int(cache_creation_tokens or 0),
				"uncached_tokens": max(0, int(input_tokens or 0) - int(cached_tokens or 0)),
			}
		except Exception:  # pragma: no cover - an observation never fails a run
			self.logger.debug("note_call_usage skipped", exc_info=True)

	# ------------------------------------------------------------------ #
	# F6: the provider-usage anchor
	# ------------------------------------------------------------------ #
	def _history_fingerprint(self, covered: int) -> int:
		"""A cheap content-shape hash over the first ``covered`` history messages.

		Built from each message's class, its recorded token count and its
		``tool_call_id`` — all already computed, so this costs no tokenizer work
		and no string materialization (a base64 screenshot must not be re-rendered
		once per gauge read). Any insert, removal or reorder inside the covered
		span changes it, which is the whole point: the anchor must fail closed.
		"""
		digest = 1469598103934665603  # FNV-1a 64-bit offset basis
		for managed in list(self.history.messages)[:covered]:
			message = getattr(managed, "message", managed)
			metadata = getattr(managed, "metadata", None)
			shape = (
				type(message).__name__,
				int(getattr(metadata, "input_tokens", 0) or 0),
				getattr(message, "tool_call_id", None),
			)
			digest = ((digest ^ hash(shape)) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
		return digest

	def _set_usage_anchor(self, prompt_tokens: int) -> None:
		"""Pin the provider's prompt_tokens to the history it covered."""
		covered = len(self.history.messages)
		self._usage_anchor = (
			int(prompt_tokens),
			covered,
			self._history_fingerprint(covered),
			bool(getattr(self, "_hmem_in_last_prompt", False)),
		)

	def reset_usage_anchor(self, reason: str = "") -> None:
		"""Drop the anchor — the covered prefix no longer exists.

		Called from every place that clears, rebuilds or batch-evicts the history
		(compaction, the emergency prune, the loop-recovery clear, F10 eviction).
		The fingerprint would catch these anyway; dropping it explicitly keeps the
		reason in the log instead of leaving a silent estimate swap.
		"""
		if getattr(self, "_usage_anchor", None) is not None:
			self._usage_anchor = None
			self.logger.debug(f"usage anchor dropped{f': {reason}' if reason else ''}")

	def usage_anchor(self) -> Optional[tuple]:
		"""The VALID anchor, or None.

		Returns ``(prompt_tokens, covered, hmem_included)`` when the covered prefix
		is still byte-shape-identical to what the provider billed; None otherwise.
		"""
		anchor = getattr(self, "_usage_anchor", None)
		if not anchor:
			return None
		prompt_tokens, covered, fingerprint, hmem_included = anchor
		if covered > len(self.history.messages):
			return None
		if self._history_fingerprint(covered) != fingerprint:
			return None
		return prompt_tokens, covered, hmem_included

	def _timeout_by_output(self, use_vision: bool) -> float:
		"""057 WS-B: timeout scaled by EXPECTED OUTPUT, not by input.

		Measured on prod over 24 h: <=1k output -> 13 s, 5k -> 101 s, 16k -> 272 s,
		while uncached INPUT was flat at 41-63 s across every bucket. The legacy
		ladder below scales on input, so it hands a 600 s budget to a small-output
		call in a big context and a 180 s budget to a 16k-token file write — which
		is the one that actually needs 270 s.

		base 90 s + expected_output/40 tok-s + 1 s per 10k uncached input tokens,
		floored at 120 s. Expected output = the larger of the last two calls'
		output, so one big write widens the next step's budget and a run of small
		steps narrows it back; capped at this session's own output ceiling, so the
		budget can never exceed what the provider is allowed to emit.
		"""
		recent = [t for t in (getattr(self, "_recent_output_tokens", []) or []) if t]
		expected = max(recent) if recent else 1024
		try:
			from modules.llm.output_budget import output_token_cap
			ceiling = output_token_cap(getattr(self.llm, "_client", None) or self.llm, 16384)
		except Exception:
			ceiling = 16384
		expected = min(expected, max(1, ceiling))
		uncached = int(getattr(self, "_last_uncached_input", 0) or 0)
		timeout = 90.0 + (expected / 40.0) + (uncached / 10000.0)
		if use_vision:
			timeout += 20.0
		timeout = max(timeout, 120.0)
		self.logger.info(
			f"Timeout by output: expected_out={expected:,} (cap {ceiling:,}), "
			f"uncached_in={uncached:,} -> {timeout:.0f}s")
		return timeout

	def calculate_llm_timeout(self, tool_count: int = 0, use_vision: bool = False) -> float:
		"""Calculate the per-call LLM timeout.

		Two formulas. Under ``LLM_TIMEOUT_BY_OUTPUT`` (057 WS-B) the budget
		scales on EXPECTED OUTPUT — see :meth:`_timeout_by_output`. Default OFF
		keeps the legacy input-token ladder below, byte-identical.

		Args:
			tool_count: Number of tools available to the LLM
			use_vision: Whether vision is enabled

		Returns:
			Timeout in seconds, scaled based on complexity
		"""
		import os

		from core.env import bool_env as _bool_env
		if _bool_env("LLM_TIMEOUT_BY_OUTPUT", False):
			timeout = self._timeout_by_output(use_vision)
			env_timeout = os.getenv('AUTOV2_LLM_TIMEOUT_OVERRIDE')
			if env_timeout:
				try:
					timeout = float(env_timeout)
					self.logger.info(f"Using environment timeout override: {timeout:.0f}s")
				except ValueError:
					self.logger.warning(f"Invalid timeout override value: {env_timeout}")
			return timeout

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
		"""No-op, kept for its callers in ``core/step.py``.

		F16(ii): the per-step state message rides the one-shot ephemeral rail and
		never enters ``history``, so there is nothing to remove. (The old splice
		rebuilt the whole deque and colded every cached byte behind it.)
		"""
		return None

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


	



