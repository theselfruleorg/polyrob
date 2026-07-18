from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from modules.llm.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    MessageOrigin,
    SystemMessage,
    ToolMessage,
    make_control_message,
)

logger = logging.getLogger(__name__)


def _transient_compaction_errors() -> tuple:
	"""Exception types that mean "retry later", not "this prompt is bad" (Phase 0.3).

	A rate-limit / auth blip / connection drop during summarization should ABORT
	compaction and keep full context, not trigger permanent lossy fallback. Imported
	lazily-at-module-load and tolerant of a missing symbol so a core.exceptions
	refactor can't break import of this hot module.
	"""
	names = ("LLMRateLimitError", "LLMAuthenticationError", "LLMConnectionError")
	import core.exceptions as _exc
	types = tuple(t for t in (getattr(_exc, n, None) for n in names) if isinstance(t, type))
	# TimeoutError is always transient; include the stdlib one as a floor.
	return types + (TimeoutError,)


_TRANSIENT_COMPACTION_ERRORS = _transient_compaction_errors()

# Compaction payload tuning (Reference-parity upgrade — see
# docs/REFERENCE_VS_ROB_CONTEXT_SYSTEM_2026-06.md §9).
_PER_MSG_CAP = 3000          # head+tail char budget per ordinary message (was a hard [:500])
_TOOL_RESULT_CAP = 3000      # head+tail char budget for a tool result (was the literal "[Tool result]")
_TAIL_TOKEN_RATIO = 0.20     # protected-tail token budget = max_input_tokens * ratio (C3)
_MIN_KEEP_RECENT = 10        # floor for the protected tail
_THRASH_SAVINGS_FLOOR = 0.10 # if last 2 compactions each saved < this, stop trying (B4)
_STATIC_FALLBACK_CAP = 8000  # char ceiling for the deterministic fallback summary (A6)
_COMPACTED_MARKER = "[COMPACTED SESSION HISTORY]"
# Absolute char budget for a single summarization prompt (~150K tokens). When the middle
# exceeds this, summarize in iterative windows so a SMALL auxiliary model (A5) can't be
# overflowed — Reference-style chunked/iterative summarization. No data is dropped.
_SUMMARIZE_INPUT_BUDGET_CHARS = 600_000


def _head_tail(text: str, cap: int) -> str:
	"""Truncate ``text`` to ``cap`` chars keeping the head AND tail (Reference-style).

	A flat ``[:cap]`` throws away the end of a message, which is often where the
	decision/result lives. Head+tail preserves both ends with an elision marker.
	"""
	if len(text) <= cap:
		return text
	half = max(1, cap // 2)
	return f"{text[:half]}\n…[{len(text) - cap} chars elided]…\n{text[-half:]}"


class CompactorMixin:
	# Empty slots so the composed MessageManager keeps its own __slots__ (no __dict__).
	__slots__ = ()

	def clear_history_keep_system(self, keep_last_n: int = 2) -> None:
		"""Clear message history but keep system messages and last N messages.

		This is used for loop detection recovery - removes context while preserving
		essential system setup and recent conversation flow.

		Args:
			keep_last_n: Number of most recent messages to keep (default 2)
		"""
		# SECURITY FIX: Use _history_lock to protect deque operations
		with self._history_lock:
			messages_to_keep = []

			# Keep all SystemMessage instances
			for managed_msg in self.history.messages:
				msg = managed_msg.message
				if isinstance(msg, SystemMessage):
					messages_to_keep.append(managed_msg)

			# Add last N messages if available
			if len(self.history.messages) >= keep_last_n:
				messages_to_keep.extend(list(self.history.messages)[-keep_last_n:])

			# Clear and re-add kept messages
			self.history.messages.clear()
			for managed_msg in messages_to_keep:
				self.history.messages.append(managed_msg)

			# Recalculate token count
			self.history.total_tokens = sum(m.metadata.input_tokens + (m.metadata.output_tokens or 0) for m in self.history.messages)
			self.logger.info(f"Cleared history, kept {len(messages_to_keep)} messages ({self.history.total_tokens} tokens)")

	def emergency_context_prune(self) -> None:
		"""Emergency pruning of message history to recover from context overflow.

		Keeps only:
		1. System message
		2. Initial task (first human message)
		3. Last N messages with complete tool call pairs

		CRITICAL: Preserves complete AIMessage+ToolMessage pairs to prevent sequence validation errors.
		Hierarchical memory will be re-injected on next LLM call.
		"""
		self.logger.warning("🚨 EMERGENCY CONTEXT PRUNING INITIATED")

		messages = list(self.history.messages)
		original_count = len(messages)
		original_tokens = self.history.total_tokens

		if original_count <= 5:
			self.logger.info("Message count already minimal, skipping prune")
			return

		preserved = []

		# Keep system message (if exists) - but it's stored separately, not in history.messages
		# This is just for safety in case old code added it
		remaining_messages = messages
		if messages and isinstance(messages[0].message if hasattr(messages[0], 'message') else messages[0], SystemMessage):
			preserved.append(messages[0])
			remaining_messages = messages[1:]

		# Keep initial task (first human message) - but it's also stored separately
		# This is for safety in case old code added it
		for i, managed_msg in enumerate(remaining_messages):
			msg = managed_msg.message if hasattr(managed_msg, 'message') else managed_msg
			if isinstance(msg, HumanMessage):
				preserved.append(managed_msg)
				remaining_messages = remaining_messages[i+1:]
				break

		# ✅ FIX: Keep last N messages with complete tool call pairs
		# Start with last 3 messages and expand if needed to complete pairs
		min_recent_messages = 3
		recent_messages = []

		# Work backwards from the end to collect complete tool call pairs
		idx = len(remaining_messages) - 1
		messages_collected = 0
		pending_tool_call_ids = set()  # Tool calls waiting for responses

		while idx >= 0 and messages_collected < min_recent_messages + len(pending_tool_call_ids) * 2:
			managed_msg = remaining_messages[idx]
			msg = managed_msg.message if hasattr(managed_msg, 'message') else managed_msg

			# Check if this is a ToolMessage
			if isinstance(msg, ToolMessage) and hasattr(msg, 'tool_call_id'):
				recent_messages.insert(0, managed_msg)
				pending_tool_call_ids.add(msg.tool_call_id)  # Mark that we need the corresponding AIMessage
				messages_collected += 1

			# Check if this is an AIMessage with tool_calls
			elif isinstance(msg, AIMessage) and hasattr(msg, 'tool_calls') and msg.tool_calls:
				recent_messages.insert(0, managed_msg)
				messages_collected += 1

				# Remove tool calls that we've already seen responses for
				for tc in msg.tool_calls:
					tc_id = tc.get('id') if isinstance(tc, dict) else getattr(tc, 'id', None)
					if tc_id in pending_tool_call_ids:
						pending_tool_call_ids.discard(tc_id)
					else:
						# This tool call doesn't have a response yet - we need to keep searching
						# But we've already added this AIMessage, so just track it
						pass

			else:
				# Regular message (HumanMessage, SystemMessage without tool calls, etc.)
				recent_messages.insert(0, managed_msg)
				messages_collected += 1

			idx -= 1

		# If we still have pending tool call IDs, log a warning
		if pending_tool_call_ids:
			self.logger.warning(
				f"⚠️ Emergency prune: Could not find AIMessages for tool calls: {pending_tool_call_ids}. "
				f"Message sequence may be invalid."
			)

		preserved.extend(recent_messages)

		# Reconstruct history
		self.history.messages.clear()
		self.history.total_tokens = 0

		for managed_msg in preserved:
			msg = managed_msg.message if hasattr(managed_msg, 'message') else managed_msg
			# Re-add each message to recalculate tokens
			self._add_message_with_tokens(msg, _internal=True)

		new_count = len(self.history.messages)
		new_tokens = self.history.total_tokens

		# B4 recovery: an emergency prune materially changes the history, so any prior
		# "compaction isn't helping" (anti-thrash) signal is now stale. Reset it so LLM
		# compaction gets a fresh chance once usage climbs back into the 85-95% band.
		self._compaction_savings = []

		self.logger.warning(
			f"✅ Emergency pruning complete:\n"
			f"   Messages: {original_count} → {new_count} (removed {original_count - new_count})\n"
			f"   Tokens: {original_tokens:,} → {new_tokens:,} (freed {original_tokens - new_tokens:,})\n"
			f"   Usage: {self.get_context_usage_percent():.1f}%"
		)

	# ------------------------------------------------------------------ #
	# Compaction tail sizing (C3) + anti-thrash (B4)
	# ------------------------------------------------------------------ #
	def _compaction_keep_recent(self, messages: list, min_keep: int = _MIN_KEEP_RECENT) -> int:
		"""How many recent messages to keep intact (token-budget OR count, generous).

		C3: instead of a flat ``keep_recent=10``, protect the tail by a token budget
		(``max_input_tokens * ratio``) when that protects MORE than the floor — so a
		token-dense history keeps the meaningful recent turns, not just the last 10.
		"""
		max_in = int(getattr(self, "max_input_tokens", 0) or 0)
		budget = int(max_in * _TAIL_TOKEN_RATIO)
		if budget <= 0:
			return min_keep
		count = 0
		used = 0
		for msg in reversed(messages):
			tokens = max(1, len(str(getattr(msg, "content", ""))) // 4)
			if used + tokens > budget:
				break
			used += tokens
			count += 1
		return max(count, min_keep)

	def _compaction_is_thrashing(self) -> bool:
		"""B4: if the last two compactions each saved < floor, stop calling the LLM.

		The ≥95% emergency prune (a non-LLM net) still runs from ``step.py`` — this
		only suppresses the *expensive* LLM compaction when it has stopped helping.
		"""
		savings = getattr(self, "_compaction_savings", [])
		recent = savings[-2:]
		return len(recent) == 2 and all(s < _THRASH_SAVINGS_FLOOR for s in recent)

	def _record_compaction_savings(self, original_tokens: int, new_tokens: int) -> None:
		saved = (original_tokens - new_tokens) / max(original_tokens, 1)
		savings = list(getattr(self, "_compaction_savings", []))
		savings.append(max(0.0, saved))
		self._compaction_savings = savings[-2:]

	# ------------------------------------------------------------------ #
	# Pre-compaction checkpoint (C2 — scoped-down lineage)
	# ------------------------------------------------------------------ #
	def _resolve_checkpoint_dir(self) -> Optional[str]:
		explicit = getattr(self, "_compaction_checkpoint_dir", None)
		if explicit:
			return explicit
		session_id = getattr(self, "session_id", None)
		if not session_id:
			return None
		try:
			from agents.task.path import pm
			return str(pm().get_history_dir(session_id))
		except Exception:
			return None

	def _write_compaction_checkpoint(self, messages: list) -> None:
		"""Persist the raw pre-compaction trajectory so it stays recoverable (C2)."""
		checkpoint_dir = self._resolve_checkpoint_dir()
		if not checkpoint_dir:
			return
		try:
			import os
			import json
			os.makedirs(checkpoint_dir, exist_ok=True)
			n = getattr(self, "_compaction_count", 0)
			path = os.path.join(checkpoint_dir, f"compaction_{n}.json")
			payload = [
				{"role": m.__class__.__name__, "content": str(getattr(m, "content", ""))}
				for m in messages
			]
			with open(path, "w", encoding="utf-8") as fh:
				json.dump(payload, fh, ensure_ascii=False)
			self._compaction_count = n + 1
		except Exception as exc:  # never let audit I/O block compaction
			self.logger.debug(f"compaction checkpoint skipped: {exc}")

	# ------------------------------------------------------------------ #
	# LLM compaction
	# ------------------------------------------------------------------ #
	async def _run_pre_compress_hook(self) -> None:
		"""Invoke the active memory provider's on_pre_compress hook (P1-4).

		Lets a provider persist anything in the volatile message tail that hasn't been
		promoted to durable memory before compaction summarizes it away. Fail-open: a
		hook error never blocks compaction. No-op under the default NullMemoryProvider.
		"""
		try:
			from modules.memory.registry import get_memory_registry
			provider = get_memory_registry().active()
			if provider is None or not getattr(provider, "is_external", False):
				return
			await provider.on_pre_compress(session_id=getattr(self, "session_id", "") or "")
		except Exception as e:
			self.logger.debug(f"on_pre_compress hook skipped: {e}")

	async def llm_compact_history(self) -> bool:
		"""LLM-driven compaction of message history.

		Reference-parity upgrade (see docs/REFERENCE_VS_ROB_CONTEXT_SYSTEM_2026-06.md):
		- A1: feed the WHOLE middle to the summarizer (no silent [-50:] drop).
		- A2: serialize tool results + raise the per-message cap (no [Tool result] literal).
		- A3: structured, sectioned summary template scaled to the volume compacted.
		- A4: iterative update — feed any prior compacted summary back in to update.
		- A5: route to a cheap auxiliary model (``aux_llm``) when configured.
		- A6: deterministic static fallback when the summarizer errors.
		- B4: anti-thrash back-off; C2: pre-compaction checkpoint; C3: token-budget tail.

		Returns:
			True if compaction (or static fallback) was performed, False otherwise.
		"""
		original_messages = [m.message for m in self.history.messages]
		original_count = len(original_messages)
		original_tokens = self.history.total_tokens

		if original_count < 15:
			self.logger.debug("Not enough messages to compact")
			return False

		# B4: stop burning an LLM call when the last two compactions barely helped.
		if self._compaction_is_thrashing():
			self.logger.info("Skipping LLM compaction (anti-thrash: last 2 saved < 10%)")
			return False

		# C3: protect the tail by token budget OR count, whichever is more generous.
		keep_recent = self._compaction_keep_recent(original_messages)
		# CX-H2: never cut inside an AIMessage(tool_calls)→ToolMessage pair — a tail
		# that starts with orphan ToolMessages gets them silently dropped by
		# repair_tool_message_pairs on every subsequent LLM call. Pull leading
		# orphans back into the summarized span.
		while keep_recent < len(original_messages):
			# D2: original_messages is already unwrapped to BaseMessage above
			# (`[m.message for m in self.history.messages]`), so head IS the
			# message — the old `hasattr(head, 'message')` unwrap branch was inert.
			head_msg = original_messages[-keep_recent]
			if isinstance(head_msg, ToolMessage):
				keep_recent += 1  # extend the tail to include the owning AIMessage
			else:
				break
		messages_to_summarize = original_messages[:-keep_recent]
		recent_messages = original_messages[-keep_recent:]

		if len(messages_to_summarize) < 5:
			self.logger.debug("Not enough messages to summarize")
			return False

		# C2: snapshot the raw trajectory before we mutate it.
		self._write_compaction_checkpoint(original_messages)

		# A4: separate any prior compacted summary so it is UPDATED, not re-summarized.
		prior_summary: Optional[str] = None
		to_summarize: list = []
		for msg in messages_to_summarize:
			text = str(getattr(msg, "content", ""))
			origin = getattr(msg, "origin", "")
			# Envelope-safe + reload-safe: match the typed origin OR the marker anywhere
			# in the leading slice (the marker now sits just inside <compacted-history>).
			if origin == MessageOrigin.COMPACTION_SUMMARY or _COMPACTED_MARKER in text[:160]:
				prior_summary = text  # keep the most recent prior summary
			else:
				to_summarize.append(msg)

		# P1-4: give the active memory provider a chance to flush salient context
		# before we summarize the middle away. Fail-open and inert by default
		# (NullMemoryProvider's on_pre_compress is a no-op).
		await self._run_pre_compress_hook()

		# A5: prefer a cheap auxiliary model for compaction; fall back to the main LLM.
		compaction_llm = getattr(self, "aux_llm", None) or self.llm

		try:
			summary_text = await self._run_summarization(to_summarize, prior_summary, compaction_llm)
			if not (summary_text and summary_text.strip()):
				# An empty LLM summary would clear the history and insert an empty
				# digest — permanent DATA LOSS. Fall back to the deterministic static
				# summary; if even that is empty, abort and keep the FULL context (the
				# >=95% emergency prune is the overflow backstop).
				self.logger.warning("LLM compaction returned an empty summary; using static fallback")
				static = self._build_static_fallback_summary(to_summarize)
				if static and prior_summary:
					static = f"{prior_summary}\n\n{static}"
				if not (static and static.strip()):
					self.logger.warning("Static fallback also empty; keeping full context, will retry")
					return False
				summary_text = static
			self._rebuild_with_summary(summary_text, len(to_summarize), recent_messages)
			self._record_compaction_savings(original_tokens, self.history.total_tokens)

			self.logger.info(
				f"✅ LLM compaction complete: {original_count} → {len(self.history.messages)} messages, "
				f"{original_tokens:,} → {self.history.total_tokens:,} tokens"
			)
			return True

		except _TRANSIENT_COMPACTION_ERRORS as e:
			# Phase 0.3: a transient/credential failure must NOT cause
			# permanent lossy compaction. Abort, keep the FULL context, and retry next
			# step. The >=95% mechanical emergency prune is the overflow backstop, so
			# this is safe even if the failure persists.
			self.logger.warning(
				f"LLM compaction aborted (transient: {type(e).__name__}: {e}); "
				"keeping full context, will retry next step"
			)
			return False

		except Exception as e:
			# A6: build a deterministic summary locally instead of dropping straight to prune.
			self.logger.error(f"LLM compaction failed: {e}, building static fallback summary")
			static = self._build_static_fallback_summary(to_summarize)
			# BUG 4 fix: preserve the prior compacted summary in the fallback so
			# iterative compaction does not drop the earlier digest on a non-transient
			# error.  The happy path already passes prior_summary into _run_summarization;
			# the except branch must do the same by prepending it to the static output.
			if static and prior_summary:
				static = f"{prior_summary}\n\n{static}"
			if static:
				self._rebuild_with_summary(static, len(to_summarize), recent_messages)
				self._record_compaction_savings(original_tokens, self.history.total_tokens)
				self.logger.warning("Static fallback summary applied (summarizer unavailable)")
				return True
			self.logger.error("Static fallback empty; falling back to emergency prune")
			self.emergency_context_prune()
			return True

	def _window_messages(self, messages: list, budget_chars: int) -> list:
		"""Split messages into ordered windows whose capped char-size each fit a budget."""
		windows: list = []
		current: list = []
		size = 0
		for msg in messages:
			raw = str(getattr(msg, "content", ""))
			cap = _TOOL_RESULT_CAP if "Tool" in msg.__class__.__name__ else _PER_MSG_CAP
			msg_chars = min(len(raw), cap) + 20  # +role/label overhead
			if current and size + msg_chars > budget_chars:
				windows.append(current)
				current = []
				size = 0
			current.append(msg)
			size += msg_chars
		if current:
			windows.append(current)
		return windows or [[]]

	async def _run_summarization(self, messages: list, prior_summary: Optional[str], compaction_llm) -> str:
		"""Summarize ``messages`` in one shot, or in iterative windows when huge (A1/A4).

		Iterative windowing means a SMALL auxiliary model can summarize an arbitrarily
		large middle without overflowing: each window is summarized with the running
		summary fed back in (Reference-style), so nothing is dropped.
		"""
		windows = self._window_messages(messages, _SUMMARIZE_INPUT_BUDGET_CHARS)
		if len(windows) > 1:
			self.logger.info(f"Compaction input large; summarizing in {len(windows)} iterative windows")
		running = prior_summary
		for window in windows:
			prompt = self._build_compaction_prompt(window, prior_summary=running)
			import time as _time
			_t0 = _time.time()
			response = await compaction_llm.ainvoke([HumanMessage(content=prompt)])
			# A3: meter this aux LLM call through the single deduction path (fail-open).
			from agents.task.agent.core.aux_metering import meter_aux_llm
			await meter_aux_llm(
				usage_tracker=getattr(self, "usage_tracker", None),
				user_id=getattr(self, "metering_user_id", None),
				session_id=getattr(self, "session_id", ""),
				agent_id=getattr(self, "metering_agent_id", "") or "",
				llm=compaction_llm, response=response, duration_seconds=_time.time() - _t0,
				component="compaction", purpose="compaction",
			)
			running = response.content if hasattr(response, 'content') else str(response)
		return running or ""

	def _rebuild_with_summary(self, summary_text: str, summarized_count: int, recent_messages: list) -> None:
		"""Replace the summarized middle with one compacted message + recent tail.

		The summary is a LOSSY, LLM-synthesized digest — NOT a user turn. Tag it with
		MessageOrigin.COMPACTION_SUMMARY and an <compacted-history> envelope so the model
		reads it as derived context, not an instruction (the system prompt's source-precedence
		rule ranks it below recent messages / current files).
		"""
		body = (f"{_COMPACTED_MARKER}\n\n"
		        f"The following is a structured summary of {summarized_count} earlier messages:\n\n"
		        f"{summary_text}\n\n"
		        f"[END COMPACTED HISTORY - Recent conversation follows]")
		compacted_msg = make_control_message(body, MessageOrigin.COMPACTION_SUMMARY)
		self.history.messages.clear()
		self.history.total_tokens = 0
		self._add_message_with_tokens(compacted_msg, _internal=True)
		for msg in recent_messages:
			self._add_message_with_tokens(msg, _internal=True)

	def _build_static_fallback_summary(self, messages: list) -> str:
		"""A6: deterministic, no-LLM summary from tool names / files / errors / asks."""
		import re

		files: list = []
		errors: list = []
		user_asks: list = []
		tool_calls = 0
		for msg in messages:
			role = msg.__class__.__name__.replace("Message", "")
			content = str(getattr(msg, "content", ""))
			if "Tool" in role:
				tool_calls += 1
				for token in re.findall(r"[\w./\\-]+\.[A-Za-z0-9]{1,6}", content):
					if token not in files:
						files.append(token)
				for line in content.splitlines():
					low = line.lower()
					if "error" in low or "exception" in low or "traceback" in low:
						errors.append(line.strip()[:200])
			elif role == "Human":
				snippet = content.strip()
				if snippet:
					user_asks.append(snippet[:300])

		files = files[:40]
		errors = errors[:20]
		sections = [
			"## Active Task",
			user_asks[0] if user_asks else "(unknown — static fallback)",
			"## In Progress",
			f"(static fallback after summarizer failure; {tool_calls} tool calls across {len(messages)} messages)",
			"## Resolved Questions",
			"(not derivable without the summarizer)",
			"## Pending User Asks",
			"\n".join(f"- {u}" for u in user_asks[-3:]) or "(none)",
			"## Relevant Files & Data",
			"\n".join(f"- {f}" for f in files) or "(none)",
			"## Remaining Work",
			("Errors seen:\n" + "\n".join(f"- {e}" for e in errors)) if errors else "(none recorded)",
		]
		return "\n".join(sections)[:_STATIC_FALLBACK_CAP]

	def _build_compaction_prompt(self, messages: list, prior_summary: Optional[str] = None) -> str:
		"""Build a STRUCTURED summarization prompt from the full middle (A1/A2/A3/A4).

		Args:
			messages: messages to summarize (the whole middle, not the last 50).
			prior_summary: an existing compacted summary to UPDATE in place (A4).
		"""
		conversation_text = []
		for msg in messages:
			role = msg.__class__.__name__.replace("Message", "")
			raw = str(msg.content) if hasattr(msg, 'content') else str(msg)
			if 'Tool' in role:
				# A2: serialize the tool output (head+tail) instead of a bare marker.
				conversation_text.append(f"[Tool result] {_head_tail(raw, _TOOL_RESULT_CAP)}")
			else:
				conversation_text.append(f"{role}: {_head_tail(raw, _PER_MSG_CAP)}")

		# A1: include the ENTIRE middle, oldest first.
		conversation = "\n".join(conversation_text)

		# A3: scale the target length to the volume compacted (~5 chars/word, 20% ratio).
		total_chars = sum(len(t) for t in conversation_text)
		budget_words = max(400, min(int(total_chars * _TAIL_TOKEN_RATIO / 5), 2400))

		# A4: feed the prior summary back so it is merged/updated, not re-summarized blind.
		prior_block = ""
		if prior_summary:
			prior_block = (
				"## PRIOR SUMMARY (update this — PRESERVE all existing information, "
				"merge in new facts, drop nothing):\n"
				f"{prior_summary}\n\n"
			)

		return f"""Summarize the conversation below into a STRUCTURED running memory.
Fill every section; write "(none)" where empty. Preserve concrete data, IDs, file
paths, tool outcomes, and decisions verbatim where short.

## Active Task
## Goal & Constraints
## Completed Actions (with outcomes)
## In Progress
## Blocked / Open Questions
## Key Decisions
## Resolved Questions
## Pending User Asks
## Relevant Files & Data
## Remaining Work

Target length: ~{budget_words} words. Focus on WHAT was accomplished, not HOW.

TEMPORAL ANCHORING: record finished work as DONE (past tense, with its outcome) under
Completed Actions. Do NOT list an already-completed action under In Progress or
Remaining Work — after this summary replaces the raw history, anything left phrased as
a pending "to-do" will be RE-RUN. Only genuinely-unfinished work belongs in Remaining
Work.

RESOLVED/PENDING TRACKING: under "## Resolved Questions", record every question the
user asked and its answer, and every decision made and why — so it is not re-asked or
re-litigated after compaction. Under "## Blocked / Open Questions" and "## Pending
User Asks", record every question or thread still awaiting a reply — worded so it
survives compaction instead of being silently dropped once the raw history is gone.

{prior_block}Conversation to summarize:
{conversation}

STRUCTURED SUMMARY:"""

	# (check_and_compact_if_needed removed 2026-06-29 dead-loop prune: it had ZERO
	#  callers. The live compaction policy is the hardcoded 85/95/70 thresholds in
	#  agent/core/step.py, not CompactionManager.should_compact_messages — this method
	#  was a dead alternate path that never ran.)
