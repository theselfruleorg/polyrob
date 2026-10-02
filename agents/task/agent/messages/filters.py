from __future__ import annotations

import copy
import json
import logging
import re
from typing import Any, Dict, List, Optional, Type

from modules.llm.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from agents.task.agent.message_manager.tool_call_builder import (
    ToolCallBuilder,
    detect_and_remove_duplicate_tool_calls,
    repair_and_normalize,
    validate_tool_message_pairs,
)

logger = logging.getLogger(__name__)


# A tool result shorter than this is never replaced by a back-reference: the
# pointer text is ~80 chars, so there is nothing to save, and a short result is
# usually a CONFIRMATION ("Edited kb-root-position-ledger.md") the agent needs
# verbatim — prod 2026-09-19 15:35Z: three identical `coding_str_replace`
# confirmations in one step came back as "[duplicate of …]" and the agent
# reported "3 that returned the dedup artifact" instead of three landed edits.
DEDUP_MIN_CHARS = 160


def _dedup_reference(digest: str) -> str:
	"""The back-reference text for a repeated tool result (F16(i)).

	CONTENT-ADDRESSED ON PURPOSE. The text used to carry the first occurrence's
	POSITION in the assembled list (``#7``), which renumbers whenever anything
	lands ahead of it — a left-eviction, a restore, an insert — so the same
	unchanged conversation re-serialized to different bytes and every provider
	cache re-billed the prefix from that message onward. The digest identifies
	the earlier result without depending on where it sits.
	"""
	return f"[duplicate of an earlier tool result in this conversation — output identical to {digest}]"


def dedup_tool_results(messages: List[BaseMessage]) -> List[BaseMessage]:
	"""Replace byte-identical repeated LONG tool outputs with a back-reference (B2).

	Reference does this (MD5-keyed) in its compaction pre-pass — cheap token savings on
	retry/polling loops where the same tool returns the same payload repeatedly. The
	FIRST occurrence is kept verbatim; later identical outputs are replaced with a
	short pointer to the first. Non-tool messages and unique outputs pass through
	unchanged. Does not mutate the input list.

	See docs/REFERENCE_VS_ROB_CONTEXT_SYSTEM_2026-06.md §9 (B2).
	"""
	import hashlib

	seen: set[str] = set()
	out: List[BaseMessage] = []
	for msg in messages:
		if (isinstance(msg, ToolMessage) and isinstance(msg.content, str)
				and len(msg.content) >= DEDUP_MIN_CHARS):
			digest = hashlib.md5(msg.content.encode("utf-8", "ignore")).hexdigest()[:12]
			if digest in seen:
				out.append(ToolMessage(content=_dedup_reference(digest),
				                       tool_call_id=msg.tool_call_id))
				continue
			seen.add(digest)
		out.append(msg)
	return out


# F15 — deterministic ageing of old tool results (after a reference agent's context compressor).
# A 40k-char web_fetch from 30 steps ago used to sit in the context verbatim, at
# cache-read price, until the 85 % band fired the expensive LLM compaction. This
# pass demotes it to its first line plus a NAMED pointer, with no LLM call. It runs
# only at a compaction boundary — already a cold prefix — so it costs no extra
# cache miss, and it never touches the protected tail.
TOOL_RESULT_AGE_KEEP_RECENT = 6
#: how much of the first line survives, so ageing a single-line 40k JSON blob is
#: still idempotent (the aged body must land under the threshold).
TOOL_RESULT_AGE_HEAD_CHARS = 240
#: the marker that says "already aged" — the pass must never age its own output.
AGED_RESULT_MARKER = "[…aged:"
#: a pointer the offload path (``result_offload.py``) already wrote into the
#: content: ``read_file`` + ``file_path="<name>"``. Kept VERBATIM when present —
#: inventing a second phrasing of the same pointer is how an agent learns to
#: guess file names.
_OFFLOAD_POINTER = re.compile(r"^.*read_file.*file_path.*$", re.MULTILINE)


#: D4 (review 2026-09-29): a ``load_skill`` result (``<skill id="...">`` body)
#: is never aged. It is an instruction the agent follows for the rest of the
#: session, not bulk data; ageing it left one line of doctrine while the
#: controller still answered "already active" to a reload.
SKILL_RESULT_PREFIX = '<skill id="'


def tool_result_age_chars() -> int:
	"""``TOOL_RESULT_AGE_CHARS`` (default 2000) — the size past which an old tool
	result is demoted. ``0`` disables the pass entirely."""
	from core.env import int_env
	return int_env("TOOL_RESULT_AGE_CHARS", 2000)


def _aged_body(content: str) -> str:
	"""The demoted form: the first line (capped) + ONE named pointer."""
	head = content.split("\n", 1)[0].strip()[:TOOL_RESULT_AGE_HEAD_CHARS]
	pointer = _OFFLOAD_POINTER.search(content)
	if pointer:
		note = (f"{AGED_RESULT_MARKER} {len(content):,} chars dropped from the context to make "
		        f"room; the full result is on disk — {pointer.group(0).strip()}]")
	else:
		note = (f"{AGED_RESULT_MARKER} {len(content):,} chars dropped from the context to make "
		        f"room; this result was never offloaded to the workspace, so there is no file to "
		        f"read — re-run the tool if you still need it]")
	return f"{head}\n{note}" if head else note


def age_old_tool_results(messages: List[BaseMessage],
                         keep_recent: int = TOOL_RESULT_AGE_KEEP_RECENT,
                         max_chars: Optional[int] = None) -> List[BaseMessage]:
	"""Demote long tool results OUTSIDE the protected tail to one line + a pointer.

	Pairing is never at risk: the ``ToolMessage`` stays exactly where it is and
	keeps its ``tool_call_id`` — only its CONTENT shrinks — so no
	``AIMessage(tool_calls)`` is ever left without its answer.

	A ``load_skill`` result (``SKILL_RESULT_PREFIX``) is never aged (D4).

	IDEMPOTENT: an aged body carries ``AGED_RESULT_MARKER`` and is skipped, and the
	surviving head is capped so a single-line blob lands under the threshold too.
	Does not mutate the input list or the input messages.
	"""
	limit = tool_result_age_chars() if max_chars is None else int(max_chars)
	if limit <= 0 or not messages:
		return list(messages)
	protected = max(0, len(messages) - max(0, int(keep_recent)))
	out: List[BaseMessage] = []
	for idx, msg in enumerate(messages):
		content = getattr(msg, "content", None)
		if (idx < protected and isinstance(msg, ToolMessage) and isinstance(content, str)
				and len(content) > limit and AGED_RESULT_MARKER not in content
				and not content.startswith(SKILL_RESULT_PREFIX)):
			aged = copy.copy(msg)
			aged.content = _aged_body(content)
			out.append(aged)
			continue
		out.append(msg)
	return out


# F10: the batch size the bounded history deque evicts in when it saturates.
# One message per append is the pathological case — it shifts the whole
# conversation left on EVERY step, so no provider ever sees the same prefix
# twice; a batch pays the cold prefix once and then runs warm for a while.
EVICTION_MIN_BATCH = 8
EVICTION_BATCH_DIVISOR = 10


def unwrap_message(item: Any) -> Any:
	"""A ``ManagedMessage`` or a bare ``BaseMessage`` -> the bare message."""
	return getattr(item, "message", item)


def pair_safe_left_cut(messages: Any, cut: int) -> int:
	"""Move a LEFT cut point forward so the survivors never START with a ToolMessage.

	A history slice that begins with a ``ToolMessage`` has lost the
	``AIMessage(tool_calls)`` that declared it; ``repair_tool_message_pairs``
	then silently drops the orphan on every subsequent request (the same hazard
	the compactor guards at ``compactor.py:366-377``, from the other side — it
	extends the kept TAIL backwards to pick the owning AIMessage up, while an
	eviction has already dropped that AIMessage and so must drop the orphan too).

	Returns the adjusted cut (clamped to ``len(messages)``).
	"""
	n = len(messages)
	cut = max(0, min(int(cut), n))
	while cut < n and isinstance(unwrap_message(messages[cut]), ToolMessage):
		cut += 1
	return cut


def _message_has_base64_image(message: BaseMessage) -> bool:
	content = getattr(message, "content", None)
	if not isinstance(content, list):
		return False
	for block in content:
		if (
			isinstance(block, dict)
			and block.get("type") == "image_url"
			and "base64" in str(block.get("image_url", {}).get("url", ""))
		):
			return True
	return False


# F7: how many image-bearing turns the history may hold before any are retired,
# how many are retired in one step, and how many are never retired. The old rule
# anchored on the NEWEST image and stripped everything before it, so the
# previously-newest turn was rewritten on EVERY step — a browser session
# screenshots every step, so a 30-step run never reused its prefix past the
# last-but-one screenshot. A step function pays that rewrite once per batch.
IMAGE_STRIPPED_MARKER = "[historical image stripped]"


def media_keep_max() -> int:
	"""``MEDIA_KEEP_MAX`` (default 6) — image turns held before any is retired."""
	from core.env import int_env
	return int_env("MEDIA_KEEP_MAX", 6)


def media_retire_batch() -> int:
	"""``MEDIA_RETIRE_BATCH`` (default 4) — image turns retired in ONE step."""
	from core.env import int_env
	return int_env("MEDIA_RETIRE_BATCH", 4)


def media_keep_floor() -> int:
	"""``MEDIA_KEEP_FLOOR`` (default 2) — newest image turns never retired."""
	from core.env import int_env
	return int_env("MEDIA_KEEP_FLOOR", 2)


def _strip_images_from(message: BaseMessage) -> BaseMessage:
	"""A copy of ``message`` with every base64 image block replaced by the marker."""
	new_content = []
	for block in message.content:
		if (
			isinstance(block, dict)
			and block.get("type") == "image_url"
			and "base64" in str(block.get("image_url", {}).get("url", ""))
		):
			new_content.append({"type": "text", "text": IMAGE_STRIPPED_MARKER})
		else:
			new_content.append(block)
	stripped = copy.copy(message)
	stripped.content = new_content
	return stripped


def strip_historical_media(messages: List[BaseMessage]) -> List[BaseMessage]:
	"""Retire old base64 images in BATCHES, never by re-anchoring every step (F7).

	POLYROB's blunt ``STRIP_BASE64_IMAGES`` removes *all* images at parse, which
	hurts multi-step vision tasks. The first fix anchored on the newest
	image-bearing turn and stripped every turn before it — correct for size, fatal
	for the prompt cache: each new screenshot demoted the previous one, rewriting a
	message the provider had already cached and colding everything behind it on
	EVERY step of a browser run.

	The rule is now a step function:

	* ``<= MEDIA_KEEP_MAX`` (6) image turns — nothing is touched, so consecutive
	  assemblies are byte-identical up to the new turn.
	* over it — retire the OLDEST turns in one batch (at least
	  ``MEDIA_RETIRE_BATCH``, and always enough to land back at or under
	  ``MEDIA_KEEP_MAX``), while never retiring the newest ``MEDIA_KEEP_FLOOR``
	  (2) turns. One cold step, then a warm run until the next batch.

	IDEMPOTENT: a retired turn no longer carries base64, so it is no longer
	counted, and the batch is sized to land at or under the ceiling — re-running
	this on its own output returns the same bytes. Does not mutate the input list.
	"""
	indices = [idx for idx, msg in enumerate(messages) if _message_has_base64_image(msg)]
	if not indices:
		return list(messages)

	keep_max = max(0, media_keep_max())
	if len(indices) <= keep_max:
		return list(messages)

	floor = max(0, media_keep_floor())
	batch = max(1, media_retire_batch())
	# At least one batch, and enough to land back at/under the ceiling so a second
	# pass is a no-op; never past the floor of newest turns.
	retire = min(max(batch, len(indices) - keep_max), max(0, len(indices) - floor))
	if retire <= 0:
		return list(messages)

	doomed = set(indices[:retire])
	return [_strip_images_from(msg) if idx in doomed else msg
	        for idx, msg in enumerate(messages)]


class FiltersMixin:
	# F29: empty slots so the composed MessageManager keeps its own __slots__ and
	# never grows a __dict__. This is only TRUE while EVERY class in the MRO
	# declares one — three mixins omitted it until 2026-09-22, so the claim in
	# this comment was false for as long as it had been written.
	__slots__ = ()

	def merge_successive_messages(self, messages: list[BaseMessage], class_to_merge: Type[BaseMessage]) -> list[BaseMessage]:
		"""Merge successive messages without mutating original content
		
		IMPORTANT: Messages with tool_calls are never merged to preserve integrity
		"""
		merged_messages = []
		streak = 0
		
		for message in messages:
			if isinstance(message, class_to_merge):
				# Never merge AI messages with tool_calls to preserve integrity
				if isinstance(message, AIMessage) and hasattr(message, 'tool_calls') and message.tool_calls:
					merged_messages.append(message)
					streak = 0
					continue
				
				# Check if previous message has tool_calls (should not merge into it)
				if (streak > 0 and merged_messages and 
					isinstance(merged_messages[-1], AIMessage) and 
					hasattr(merged_messages[-1], 'tool_calls') and 
					merged_messages[-1].tool_calls):
					merged_messages.append(message)
					streak = 1
					continue
				
				streak += 1
				if streak > 1:
					# Create new message instead of mutating
					if isinstance(message.content, list) and isinstance(merged_messages[-1].content, list):
						# FIXED: Properly handle multimodal content by extending lists instead of concatenating strings
						new_content = merged_messages[-1].content + message.content
					elif isinstance(message.content, list):
						# Previous message was text, current is multimodal
						text_content = merged_messages[-1].content
						new_content = [{"type": "text", "text": text_content}] + message.content
					elif isinstance(merged_messages[-1].content, list):
						# Previous message was multimodal, current is text
						text_part = {"type": "text", "text": message.content}
						new_content = merged_messages[-1].content + [text_part]
					else:
						# Both are text — join with a newline so the two messages' content
						# keeps its boundary (bare concatenation glued "...end""start..." together).
						prev_text = merged_messages[-1].content
						sep = "\n" if prev_text and not prev_text.endswith("\n") else ""
						new_content = prev_text + sep + message.content
					
					# Preserve all other fields from the first message when merging
					# This ensures we don't lose important metadata
					first_msg = merged_messages[-1]
					
					# Create new message with known supported fields
					# Most chat message classes support these fields
					kwargs = {'content': new_content}
					
					# Preserve common optional fields if they exist
					if hasattr(first_msg, 'additional_kwargs') and first_msg.additional_kwargs:
						kwargs['additional_kwargs'] = first_msg.additional_kwargs
					if hasattr(first_msg, 'response_metadata') and first_msg.response_metadata:
						kwargs['response_metadata'] = first_msg.response_metadata
					if hasattr(first_msg, 'id') and first_msg.id:
						kwargs['id'] = first_msg.id
					
					# For AI messages, preserve tool_calls (though we shouldn't merge those)
					if isinstance(first_msg, AIMessage):
						if hasattr(first_msg, 'tool_calls') and first_msg.tool_calls:
							kwargs['tool_calls'] = first_msg.tool_calls
						if hasattr(first_msg, 'invalid_tool_calls') and first_msg.invalid_tool_calls:
							kwargs['invalid_tool_calls'] = first_msg.invalid_tool_calls
					
					try:
						merged_messages[-1] = class_to_merge(**kwargs)
					except Exception as e:
						self.logger.warning(f"Failed to merge messages with kwargs, falling back to content only: {e}")
						# Fallback to just content if the class doesn't support the kwargs
						merged_messages[-1] = class_to_merge(content=new_content)
				else:
					merged_messages.append(message)
			else:
				merged_messages.append(message)
				streak = 0
		
		return merged_messages

	def _filter_sensitive_data(self, message: BaseMessage) -> BaseMessage:
		"""Filter out sensitive data from the message, tool calls, and metadata.

		Creates a copy of the message to avoid mutating the original.
		"""
		import copy
		from core.env import bool_env
		from core.secret_scrub import scrub_secret_shapes

		# Phase 0.5: pattern backstop for UNregistered secrets. The allowlist
		# (self.sensitive_data) only redacts explicitly-registered values; an sk-/
		# AKIA/Bearer/PEM leaking through a tool result would otherwise persist to
		# message_history.json + compaction checkpoints in the clear. Default ON;
		# HISTORY_SECRET_SCRUB=off restores allowlist-only. Conservative shapes only
		# (no hex/base64 catch-all) so legitimate working content is never corrupted.
		pattern_scrub_on = bool_env("HISTORY_SECRET_SCRUB", True)

		def replace_sensitive(value: str) -> str:
			if not isinstance(value, str):
				return value
			if self.sensitive_data:
				for key, val in self.sensitive_data.items():
					value = value.replace(val, f'<secret>{key}</secret>')
			if pattern_scrub_on:
				value = scrub_secret_shapes(value)
			return value

		def scrub_dict(data: dict) -> dict:
			"""Recursively scrub sensitive data from dictionaries"""
			if not isinstance(data, dict):
				return data
			scrubbed = {}
			for k, v in data.items():
				if isinstance(v, str):
					scrubbed[k] = replace_sensitive(v)
				elif isinstance(v, dict):
					scrubbed[k] = scrub_dict(v)
				elif isinstance(v, list):
					scrubbed[k] = [scrub_dict(item) if isinstance(item, dict) else
								   replace_sensitive(item) if isinstance(item, str) else item
								   for item in v]
				else:
					scrubbed[k] = v
			return scrubbed

		# Create a deep copy of the message to avoid mutating the original
		filtered_message = copy.deepcopy(message)

		# Filter content (string or multimodal)
		if isinstance(filtered_message.content, str):
			filtered_message.content = replace_sensitive(filtered_message.content)
		elif isinstance(filtered_message.content, list):
			for i, item in enumerate(filtered_message.content):
				if isinstance(item, dict) and 'text' in item:
					item['text'] = replace_sensitive(item['text'])
					filtered_message.content[i] = item

		# Filter tool calls in AIMessage
		if hasattr(filtered_message, 'tool_calls') and filtered_message.tool_calls:
			for tool_call in filtered_message.tool_calls:
				# Scrub args/arguments field
				if isinstance(tool_call, dict):
					if 'args' in tool_call:
						tool_call['args'] = scrub_dict(tool_call['args'])
					if 'arguments' in tool_call:
						# OpenAI format uses 'arguments' as JSON string
						try:
							import json
							args = json.loads(tool_call['arguments'])
							scrubbed_args = scrub_dict(args)
							tool_call['arguments'] = json.dumps(scrubbed_args)
						except:
							# If not JSON, treat as string
							tool_call['arguments'] = replace_sensitive(tool_call['arguments'])
					# Also check nested function field (OpenAI format)
					if 'function' in tool_call and isinstance(tool_call['function'], dict):
						if 'arguments' in tool_call['function']:
							try:
								import json
								args = json.loads(tool_call['function']['arguments'])
								scrubbed_args = scrub_dict(args)
								tool_call['function']['arguments'] = json.dumps(scrubbed_args)
							except:
								tool_call['function']['arguments'] = replace_sensitive(tool_call['function']['arguments'])

		# Filter ToolMessage content (already handled above for string content)
		# Filter additional_kwargs which might contain sensitive metadata
		if hasattr(filtered_message, 'additional_kwargs') and filtered_message.additional_kwargs:
			filtered_message.additional_kwargs = scrub_dict(filtered_message.additional_kwargs)

		return filtered_message

	def _validate_and_repair_tool_sequences(self, messages: List[BaseMessage]) -> List[BaseMessage]:
		"""Validate and repair tool_calls/ToolMessage sequences using centralized logic.

		✅ FIX (Nov 5, 2025): Now called with conversation-only (no SystemMessage)
		
		Delegates to the centralized repair function in tool_call_builder module
		for consistency across the codebase. This is now the SINGLE AUTHORITY
		for all tool message repair, normalization, and validation.
		
		NOTE: This is called from get_messages_for_llm() AFTER foundation is built,
		so it only receives conversation messages (no SystemMessage).
		"""
		if not messages:
			return messages

		# Use the new single-authority repair_and_normalize method
		# expect_system_message=False because we're only repairing conversation
		repaired = repair_and_normalize(messages, logger=self.logger, expect_system_message=False)

		# Log if message count changed
		if len(repaired) != len(messages):
			self.logger.info(
				f"Tool sequence repair: {len(messages)} messages -> {len(repaired)} messages"
			)

		return repaired

	def convert_messages_for_non_function_calling_models(self, messages: List[BaseMessage]) -> List[BaseMessage]:
		"""Convert messages for models that don't support native function calling.

		Converts AIMessage with tool_calls into plain text format.
		"""
		# Message types imported at module level from modules.llm.messages
		converted = []
		
		for msg in messages:
			if isinstance(msg, AIMessage) and hasattr(msg, 'tool_calls') and msg.tool_calls:
				# Convert tool calls to text description
				tool_desc = f"Planning to execute: {', '.join([tc.get('name') if isinstance(tc, dict) else tc.name for tc in msg.tool_calls])}"
				converted.append(AIMessage(content=tool_desc))
			elif isinstance(msg, ToolMessage):
				# Convert tool message to human message
				converted.append(HumanMessage(content=f"Result: {msg.content}"))
			else:
				converted.append(msg)
		
		return converted
