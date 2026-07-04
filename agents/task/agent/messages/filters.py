from __future__ import annotations

import copy
import json
import logging
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


def dedup_tool_results(messages: List[BaseMessage]) -> List[BaseMessage]:
	"""Replace byte-identical repeated tool outputs with a back-reference (B2).

	Reference does this (MD5-keyed) in its compaction pre-pass — cheap token savings on
	retry/polling loops where the same tool returns the same payload repeatedly. The
	FIRST occurrence is kept verbatim; later identical outputs are replaced with a
	short pointer to the first. Non-tool messages and unique outputs pass through
	unchanged. Does not mutate the input list.

	See docs/REFERENCE_VS_ROB_CONTEXT_SYSTEM_2026-06.md §9 (B2).
	"""
	import hashlib

	seen: Dict[str, int] = {}
	out: List[BaseMessage] = []
	for msg in messages:
		if isinstance(msg, ToolMessage) and isinstance(msg.content, str) and msg.content:
			digest = hashlib.md5(msg.content.encode("utf-8", "ignore")).hexdigest()[:12]
			if digest in seen:
				ref = ToolMessage(
					content=f"[duplicate of an earlier tool result #{seen[digest]} — output identical to {digest}]",
					tool_call_id=msg.tool_call_id,
				)
				out.append(ref)
				continue
			seen[digest] = len(out)
		out.append(msg)
	return out


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


def strip_historical_media(messages: List[BaseMessage]) -> List[BaseMessage]:
	"""Strip base64 images from every image-bearing turn EXCEPT the most recent (B3).

	POLYROB's blunt ``STRIP_BASE64_IMAGES`` removes *all* images at parse, which hurts
	multi-step vision tasks. Reference instead anchors on the last image-bearing turn and
	strips base64 only from turns *before* it — preserving vision continuity for the
	current turn while still bounding history growth. Does not mutate the input list.

	See docs/REFERENCE_VS_ROB_CONTEXT_SYSTEM_2026-06.md §9 (B3).
	"""
	anchor = -1
	for idx, msg in enumerate(messages):
		if _message_has_base64_image(msg):
			anchor = idx

	if anchor < 0:
		return list(messages)

	out: List[BaseMessage] = []
	for idx, msg in enumerate(messages):
		if idx < anchor and _message_has_base64_image(msg):
			new_content = []
			for block in msg.content:
				if (
					isinstance(block, dict)
					and block.get("type") == "image_url"
					and "base64" in str(block.get("image_url", {}).get("url", ""))
				):
					new_content.append({"type": "text", "text": "[historical image stripped]"})
				else:
					new_content.append(block)
			stripped = copy.copy(msg)
			stripped.content = new_content
			out.append(stripped)
		else:
			out.append(msg)
	return out


class FiltersMixin:
	# Empty slots so the composed MessageManager keeps its own __slots__ (no __dict__).
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
