from __future__ import annotations

import copy
import json
import logging
import re
from typing import Any, Dict, List, Optional

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


#: E-A5 (coherency review 2026-10-13): a wrapped result opens with a frame tag
#: alone on its first line (``<untrusted_tool_result source="…">``). Keeping only
#: that line kept ZERO data and left an open tag mid-history.
_OPEN_FRAME_LINE = re.compile(r"^<([A-Za-z][\w\-]*)(\s[^<>]*)?>$")
#: the fixed DATA preamble ``wrap_untrusted`` writes under the open tag.
_FRAME_PREAMBLE_PREFIX = "The following content was retrieved from an external source."


def _aged_head(content: str) -> tuple:
	"""``(open_tag_line, close_tag, first_content_line)`` of a result.

	When the result is framed (an open tag alone on line 1 and the matching close
	tag at its end), skip the tag line, the DATA preamble and blank lines, and keep
	the first content line; the caller re-closes the frame. Unframed: no tag, and
	the first line that holds a letter or digit.
	"""
	lines = content.split("\n")
	first = lines[0].strip()
	match = _OPEN_FRAME_LINE.match(first)
	if match:
		close = f"</{match.group(1)}>"
		if content.rstrip().endswith(close):
			for line in lines[1:]:
				text = line.strip()
				if not text or text == close or text.startswith(_FRAME_PREAMBLE_PREFIX):
					continue
				return first, close, text[:TOOL_RESULT_AGE_HEAD_CHARS]
			return first, close, ""
	# A JSON result often opens with a bare "{" / "[": keep the first line that
	# carries data instead.
	for line in lines:
		text = line.strip()
		if any(ch.isalnum() for ch in text):
			return "", "", text[:TOOL_RESULT_AGE_HEAD_CHARS]
	return "", "", first[:TOOL_RESULT_AGE_HEAD_CHARS]


def _aged_body(content: str) -> str:
	"""The demoted form: the first content line (capped) + ONE named pointer,
	inside the original frame when the result was framed (E-A5)."""
	open_tag, close_tag, head = _aged_head(content)
	pointer = _OFFLOAD_POINTER.search(content)
	if pointer:
		note = (f"{AGED_RESULT_MARKER} {len(content):,} chars dropped from the context to make "
		        f"room; the full result is on disk — {pointer.group(0).strip()}]")
	else:
		note = (f"{AGED_RESULT_MARKER} {len(content):,} chars dropped from the context to make "
		        f"room; this result was never offloaded to the workspace, so there is no file to "
		        f"read — re-run the tool if you still need it]")
	body = f"{head}\n{note}" if head else note
	if open_tag:
		return f"{open_tag}\n{body}\n{close_tag}"
	return body


#: 079 W7 (R8): a MONEY verb's result (a swap receipt, a transfer hash, a quote)
#: keeps its full text for this long. "Money verb" is NOT a list here — it is
#: ``core.money.classify.money_action``, i.e. the ``money`` dimension of the ONE
#: capability table (``core/tool_capabilities.py``).
MONEY_RESULT_PROTECT_SECONDS = 24 * 3600


def _tool_call_names(messages: List[BaseMessage]) -> Dict[str, str]:
	"""``tool_call_id`` -> the action name the AIMessage declared for it."""
	names: Dict[str, str] = {}
	for msg in messages:
		if not isinstance(msg, AIMessage):
			continue
		for call in getattr(msg, "tool_calls", None) or []:
			cid = call.get("id") if isinstance(call, dict) else getattr(call, "id", None)
			name = call.get("name") if isinstance(call, dict) else getattr(call, "name", None)
			if cid and name:
				names[str(cid)] = str(name)
	return names


def _is_young(stamp: Optional[str], now) -> bool:
	"""True when *stamp* (ISO) is younger than the money window — or unknown.

	An unknown age is treated as YOUNG: a missing timestamp must never be the
	reason a money receipt loses its text."""
	from datetime import datetime, timezone
	if not stamp:
		return True
	try:
		when = datetime.fromisoformat(str(stamp))
	except (TypeError, ValueError):
		return True
	ref = now or datetime.now(timezone.utc if when.tzinfo else None)
	if (ref.tzinfo is None) != (when.tzinfo is None):
		ref = ref.replace(tzinfo=None) if when.tzinfo is None else ref.replace(tzinfo=when.tzinfo)
	return (ref - when).total_seconds() < MONEY_RESULT_PROTECT_SECONDS


def _money_protected(msg: BaseMessage, names: Dict[str, str], stamp: Optional[str], now) -> bool:
	name = names.get(str(getattr(msg, "tool_call_id", "") or ""))
	if not name:
		return False
	try:
		from core.money.classify import money_action
		is_money = money_action(name)
	except Exception:
		logger.debug("money-result classification failed", exc_info=True)
		return False
	return is_money and _is_young(stamp, now)


def age_old_tool_results(messages: List[BaseMessage],
                         keep_recent: int = TOOL_RESULT_AGE_KEEP_RECENT,
                         max_chars: Optional[int] = None,
                         timestamps: Optional[List[Optional[str]]] = None,
                         now=None) -> List[BaseMessage]:
	"""Demote long tool results OUTSIDE the protected tail to one line + a pointer.

	Pairing is never at risk: the ``ToolMessage`` stays exactly where it is and
	keeps its ``tool_call_id`` — only its CONTENT shrinks — so no
	``AIMessage(tool_calls)`` is ever left without its answer.

	A ``load_skill`` result (``SKILL_RESULT_PREFIX``) is never aged (D4).

	A money verb's result younger than ``MONEY_RESULT_PROTECT_SECONDS`` keeps its
	full text (079 W7). *timestamps* runs parallel to *messages* (ISO strings,
	the ``ManagedMessage`` metadata); a missing one counts as young.

	IDEMPOTENT: an aged body carries ``AGED_RESULT_MARKER`` and is skipped, and the
	surviving head is capped so a single-line blob lands under the threshold too.
	Does not mutate the input list or the input messages.
	"""
	limit = tool_result_age_chars() if max_chars is None else int(max_chars)
	if limit <= 0 or not messages:
		return list(messages)
	protected = max(0, len(messages) - max(0, int(keep_recent)))
	names = _tool_call_names(messages)
	out: List[BaseMessage] = []
	for idx, msg in enumerate(messages):
		content = getattr(msg, "content", None)
		stamp = timestamps[idx] if timestamps is not None and idx < len(timestamps) else None
		if (idx < protected and isinstance(msg, ToolMessage) and isinstance(content, str)
				and len(content) > limit and AGED_RESULT_MARKER not in content
				and not content.startswith(SKILL_RESULT_PREFIX)
				and not _money_protected(msg, names, stamp, now)):
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
