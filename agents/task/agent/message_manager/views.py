from __future__ import annotations

import logging
from typing import List, Optional, Iterator, Dict, Any
from collections import deque

# Native message types
from modules.llm.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field, ConfigDict

logger = logging.getLogger(__name__)


class MessageMetadata(BaseModel):
	"""Metadata for a message including token counts and context"""
	model_config = ConfigDict(slots=True)  # Memory optimization

	input_tokens: int = 0
	output_tokens: Optional[int] = None
	source: Optional[str] = None  # e.g., "context", "user", "system", "artifact"
	timestamp: Optional[str] = None  # ISO format timestamp
	extracted_content: Optional[str] = None  # For storing extracted/processed content
	model: Optional[str] = None  # Model used to generate this message
	request_id: Optional[str] = None  # For deduplication and tracking
	# Removed: protected_cycles (trimming disabled), tool_call_mapping and tool_call_ids (use ToolCallTracker)


class ManagedMessage(BaseModel):
	"""A message with its metadata"""
	model_config = ConfigDict(slots=True)  # Memory optimization
	
	message: BaseMessage
	metadata: MessageMetadata = Field(default_factory=MessageMetadata)


class MessageHistory(BaseModel):
	"""Container for message history with memory optimization"""
	model_config = ConfigDict(slots=True)  # Memory optimization

	messages: deque[ManagedMessage] = Field(default_factory=lambda: deque(maxlen=5000))  # Large initial capacity
	total_tokens: int = Field(default=0)
	max_messages: int = Field(default=5000)  # Adaptive based on context size

	def __init__(self, **data):
		super().__init__(**data)
		# Set maxlen on the deque after creation
		if hasattr(self.messages, 'maxlen') and self.messages.maxlen != self.max_messages:
			# Recreate deque with correct maxlen if needed
			old_messages = list(self.messages)
			self.messages = deque(old_messages, maxlen=self.max_messages)

	def add_message(self, message: BaseMessage, metadata: MessageMetadata, position: Optional[int] = None) -> None:
		"""Add a message with metadata.

		Note: Token counting is now handled by MessageManager._increment_token_count()
		to avoid recalculation and maintain consistency.
		"""
		managed_msg = ManagedMessage(message=message, metadata=metadata)
		
		if position is None:
			self.messages.append(managed_msg)
		else:
			# FIXED: deque with maxlen doesn't support insert() when full
			# Convert to list, insert, then back to deque
			messages_list = list(self.messages)
			messages_list.insert(position, managed_msg)
			self.messages = deque(messages_list, maxlen=self.max_messages)
		# NOTE: total_tokens increment removed - now handled by MessageManager._increment_token_count()

		# Trimming removed - relying on large context windows

	def remove_message(self, index: int = -1) -> None:
		"""Remove message from history with proper error handling"""
		if not self.messages:
			logger.debug("Attempted to remove message from empty history")
			return
		
		# Validate index
		if index < 0:
			actual_index = len(self.messages) + index
		else:
			actual_index = index
			
		if actual_index < 0 or actual_index >= len(self.messages):
			logger.warning(f"Invalid message index {index} (resolved to {actual_index}) for history with {len(self.messages)} messages")
			return
		
		try:
			# FIXED: Convert deque to list for indexed removal, then back to deque
			messages_list = list(self.messages)
			msg = messages_list.pop(actual_index)  # Use actual_index instead of index
			
			# Convert back to deque with proper maxlen
			self.messages = deque(messages_list, maxlen=self.max_messages)
			
			self.total_tokens -= msg.metadata.input_tokens
			logger.debug(f"Removed message at index {index}, saved {msg.metadata.input_tokens} tokens")
		except IndexError as e:
			logger.warning(f"IndexError removing message at index {index}: {e}")

	# Helper methods to reduce boilerplate
	def __len__(self) -> int:
		"""Return number of messages in history"""
		return len(self.messages)
		
	def __iter__(self) -> Iterator[ManagedMessage]:
		"""Iterate over messages in history"""
		return iter(self.messages)
		
	def __bool__(self) -> bool:
		"""Return True if history contains messages"""
		return len(self.messages) > 0
		
	def is_empty(self) -> bool:
		"""Check if history is empty"""
		return len(self.messages) == 0
		
	def clear(self) -> None:
		"""Clear all messages and reset token count"""
		self.messages.clear()
		self.total_tokens = 0
		
	def get_message_count_by_type(self) -> dict:
		"""Get count of messages by type"""
		counts = {
			'system': 0,
			'human': 0,
			'ai': 0,
			'tool': 0,
			'other': 0
		}
		
		for msg in self.messages:
			if isinstance(msg.message, SystemMessage):
				counts['system'] += 1
			elif isinstance(msg.message, HumanMessage):
				counts['human'] += 1
			elif isinstance(msg.message, AIMessage):
				counts['ai'] += 1
			else:
				counts['other'] += 1
				
		return counts
