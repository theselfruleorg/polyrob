from __future__ import annotations

import copy
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from modules.llm.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    MessageOrigin,
    SystemMessage,
    ToolMessage,
)

from agents.task.agent.message_manager.views import MessageHistory, MessageMetadata, ManagedMessage

logger = logging.getLogger(__name__)

# UP-10 2.3: one-time guard so the write-only sqlite mirror warning fires once per
# process, not on every save_to_disk.
_warned_sqlite_mirror = False


class PersistenceMixin:
	# Empty slots so the composed MessageManager keeps its own __slots__ (no __dict__).
	__slots__ = ()

	def checkpoint_history(self, filepath: Optional[Path] = None) -> None:
		"""Create a checkpoint of current message history.

		Args:
			filepath: Path to save checkpoint file
		"""
		try:
			from datetime import datetime
			# Message types imported at module level from modules.llm.messages

			# Create checkpoint data
			checkpoint_data = {
				'version': '1.0',
				'timestamp': datetime.now().isoformat(),
				'session_id': self.session_id,
				'total_tokens': self.history.total_tokens,
				'max_messages': self.history.max_messages,
				'messages': []
			}

			# Serialize messages
			for managed_msg in self.history.messages:
				msg = managed_msg.message

				# Serialize based on message type
				msg_data = {
					'type': msg.__class__.__name__,
					'metadata': managed_msg.metadata.model_dump(),
					# CX-M1: mirror save_to_disk — persist the true source so a
					# checkpoint restore doesn't silently demote every
					# SELF_WAKE/CORRESPONDENT/COMPACTION_SUMMARY turn to USER.
					'origin': getattr(msg, 'origin', MessageOrigin.USER),
					# CX-M1: mirror save_to_disk — the state-message tag dict
					# stamped by add_state_message (distinct from the typed
					# ManagedMessage.metadata above).
					'msg_metadata': getattr(msg, 'metadata', None) or {},
				}

				# Handle different message types
				if isinstance(msg, SystemMessage):
					msg_data['content'] = msg.content
				elif isinstance(msg, HumanMessage):
					msg_data['content'] = msg.content if isinstance(msg.content, str) else str(msg.content)
				elif isinstance(msg, AIMessage):
					msg_data['content'] = msg.content
					if hasattr(msg, 'tool_calls') and msg.tool_calls:
						msg_data['tool_calls'] = [
							{
								'id': tc.get('id') if isinstance(tc, dict) else getattr(tc, 'id', None),
								'name': tc.get('name') if isinstance(tc, dict) else getattr(tc, 'name', None),
								'args': tc.get('args', {}) if isinstance(tc, dict) else getattr(tc, 'args', {})
							}
							for tc in msg.tool_calls
						]
				elif isinstance(msg, ToolMessage):
					msg_data['content'] = msg.content
					msg_data['tool_call_id'] = getattr(msg, 'tool_call_id', None)
				else:
					msg_data['content'] = str(msg.content) if hasattr(msg, 'content') else str(msg)

				checkpoint_data['messages'].append(msg_data)

			# Store checkpoint in memory
			self._history_checkpoint = [
				ManagedMessage(
					message=copy.deepcopy(m.message),
					metadata=copy.deepcopy(m.metadata)
				)
				for m in self.history.messages
			]
			self._checkpoint_token_count = self.history.total_tokens

			# Save to file if path provided
			if filepath:
				# Atomic write
				import tempfile
				import os

				filepath = Path(filepath)
				filepath.parent.mkdir(parents=True, exist_ok=True)

				temp_fd, temp_path = tempfile.mkstemp(
					dir=filepath.parent,
					prefix='.message_checkpoint_',
					suffix='.tmp'
				)

				try:
					with os.fdopen(temp_fd, 'w') as f:
						json.dump(checkpoint_data, f, indent=2)
						f.flush()
						os.fsync(f.fileno())

					os.replace(temp_path, str(filepath))
					self.logger.info(f"Saved message checkpoint to {filepath}")

				except Exception:
					if os.path.exists(temp_path):
						os.unlink(temp_path)
					raise
			else:
				self.logger.debug(f"Created history checkpoint with {len(self._history_checkpoint)} messages")

		except Exception as e:
			self.logger.warning(f"Failed to create checkpoint: {e}")

	def restore_from_checkpoint(self) -> bool:
		"""Restore message history from checkpoint if available."""
		if hasattr(self, '_history_checkpoint') and self._history_checkpoint:
			try:
				# CX-M2: rewrap into a FRESH deque(maxlen=...) rather than aliasing
				# the checkpoint list directly. Assigning the list itself let later
				# appends mutate `_history_checkpoint` in place (no true rollback
				# point) and dropped the deque's maxlen eviction behaviour.
				from collections import deque
				self.history.messages = deque(
					list(self._history_checkpoint),
					maxlen=self.history.max_messages,
				)
				self.history.total_tokens = self._checkpoint_token_count
				self.logger.info(f"Restored history from checkpoint with {len(self.history.messages)} messages")
				return True
			except Exception as e:
				self.logger.warning(f"Failed to restore from checkpoint: {e}")
		return False

	def restore_from_checkpoint_file(self, filepath: Path) -> bool:
		"""Restore message history from checkpoint file.

		Args:
			filepath: Path to checkpoint file

		Returns:
			True if restoration successful
		"""
		try:
			# Message types imported at module level from modules.llm.messages

			if not filepath.exists():
				self.logger.debug(f"Checkpoint file not found: {filepath}")
				return False

			with open(filepath, 'r') as f:
				checkpoint_data = json.load(f)

			# Validate version
			if checkpoint_data.get('version') != '1.0':
				self.logger.warning(f"Checkpoint version mismatch: {checkpoint_data.get('version')}")
				return False

			# Validate session_id
			if self.session_id and checkpoint_data.get('session_id') != self.session_id:
				self.logger.warning(f"Checkpoint session_id mismatch")
				return False

			# SECURITY FIX: Protect clear + restore operations with lock
			with self._history_lock:
				# Clear current history
				self.history.messages.clear()
				self.history.total_tokens = 0

				# Restore messages
				message_classes = {
					'SystemMessage': SystemMessage,
					'HumanMessage': HumanMessage,
					'AIMessage': AIMessage,
					'ToolMessage': ToolMessage
				}

				for msg_data in checkpoint_data.get('messages', []):
					msg_type = msg_data.get('type')
					msg_class = message_classes.get(msg_type)

					if not msg_class:
						self.logger.warning(f"Unknown message type: {msg_type}")
						continue

					# Reconstruct message
					try:
						if msg_type == 'AIMessage' and 'tool_calls' in msg_data:
							msg = AIMessage(
								content=msg_data['content'],
								tool_calls=msg_data['tool_calls']
							)
						elif msg_type == 'ToolMessage':
							msg = ToolMessage(
								content=msg_data['content'],
								tool_call_id=msg_data.get('tool_call_id')
							)
						else:
							msg = msg_class(content=msg_data['content'])

						# CX-M1: mirror load_from_disk — restore the true source
						# (pre-fix checkpoint files have no key -> USER, the
						# historical/legacy behaviour).
						msg.origin = msg_data.get('origin', MessageOrigin.USER)
						# CX-M1: mirror load_from_disk — restore the state-message
						# tag dict so a checkpoint-restored session can still
						# target it by identity.
						if msg_data.get('msg_metadata'):
							msg.metadata = msg_data['msg_metadata']

						# Restore metadata
						metadata_dict = msg_data.get('metadata', {})
						metadata = MessageMetadata(**metadata_dict)

						# Add to history
						self.history.messages.append(ManagedMessage(
							message=msg,
							metadata=metadata
						))
						self.history.total_tokens += metadata.input_tokens

					except Exception as e:
						self.logger.warning(f"Failed to restore message: {e}")
						continue

				# Restore totals
				if 'total_tokens' in checkpoint_data:
					# Verify token count
					calculated = sum(m.metadata.input_tokens for m in self.history.messages)
					saved = checkpoint_data['total_tokens']
					if abs(calculated - saved) > 100:  # Allow small variance
						self.logger.warning(f"Token count mismatch: calculated={calculated}, saved={saved}")
					self.history.total_tokens = saved

				self.logger.info(f"Restored {len(self.history.messages)} messages from checkpoint")
			return True

		except Exception as e:
			self.logger.error(f"Failed to restore from checkpoint file: {e}")
			return False

	def save_to_disk(self, session_id: str, user_id: Optional[str] = None) -> None:
		"""Save message history to disk for persistence across session eviction/restart.

		Args:
			session_id: Session identifier
			user_id: Optional user identifier for path construction
		"""
		from agents.task.path import pm

		try:
			# Create path
			save_path = pm().create_file_path(
				session_id=session_id,
				subdir_name="memory",
				filename="message_history.json",
				user_id=user_id
			)

			# Serialize messages
			history_data = {
				"session_id": session_id,
				"messages": [],
				"total_tokens": self.history.total_tokens,
				"saved_at": datetime.now().isoformat()
			}

			# Serialize each message in history
			for msg_wrapper in self.history.messages:
				msg = msg_wrapper.message
				# metadata.timestamp is an ISO string (MessageMetadata.timestamp: Optional[str]);
				# tolerate a datetime for defensive back-compat.
				ts = msg_wrapper.metadata.timestamp
				if ts is not None and not isinstance(ts, str):
					ts = ts.isoformat()
				msg_data = {
					"type": msg.__class__.__name__,
					"content": msg.content,
					# P4: persist the true source so a rehydrated session can tell a
					# forged/self-wake/system turn from a genuine user message.
					"origin": getattr(msg, "origin", MessageOrigin.USER),
					# CX-H1: persist the state-message tag (metadata dict stamped by
					# add_state_message) so remove_last_state_message can find it by
					# identity after a session is reloaded from disk, not just via
					# the legacy shape heuristic.
					"msg_metadata": getattr(msg, "metadata", None) or {},
					"metadata": {
						"input_tokens": msg_wrapper.metadata.input_tokens,
						"output_tokens": msg_wrapper.metadata.output_tokens,
						"source": msg_wrapper.metadata.source,
						"timestamp": ts,
					}
				}

				# Include tool call data if present
				if hasattr(msg, 'tool_calls') and msg.tool_calls:
					msg_data["tool_calls"] = msg.tool_calls
				if hasattr(msg, 'tool_call_id') and msg.tool_call_id:
					msg_data["tool_call_id"] = msg.tool_call_id

				history_data["messages"].append(msg_data)

			# Save to disk
			save_path.parent.mkdir(parents=True, exist_ok=True)
			with open(save_path, 'w') as f:
				json.dump(history_data, f, indent=2)

			self.logger.info(f"💾 Saved {len(self.history.messages)} messages to {save_path}")

			# Opt-in durable mirror (MESSAGE_STORE_BACKEND=sqlite). Additive: JSON stays
			# the source of truth. UP-10 2.3: the read path is intentionally NOT wired —
			# load_from_disk only reads message_history.json (see below), so this mirror
			# is write-only and can silently diverge. A correct read path needs payload
			# parity (tool_call_id + metadata) and a JSON/DB reconciliation policy; that is
			# scoped to a dedicated durable-store proposal. We keep the pre-staged write but
			# emit a one-time WARN so enabling the flag is honest about being write-only.
			import os
			if os.getenv("MESSAGE_STORE_BACKEND", "").lower() == "sqlite":
				global _warned_sqlite_mirror
				if not _warned_sqlite_mirror:
					_warned_sqlite_mirror = True
					self.logger.warning(
						"MESSAGE_STORE_BACKEND=sqlite is a write-only mirror; reads still come "
						"from message_history.json. The sqlite DB is pre-staged but never read "
						"(no read path wired yet — see UP-10 2.3). It may diverge from JSON."
					)
				try:
					from agents.task.agent.messages.sqlite_persistence import SqliteMessageStore
					store = SqliteMessageStore(os.path.join(str(getattr(pm(), "data_root", "data")), "messages.db"))
					# MED-4: atomic single-transaction swap (no clear()+append() race/churn).
					store.replace_all(session_id, [
						{
							"type": managed.message.__class__.__name__,
							"content": getattr(managed.message, "content", ""),
							"tool_calls": getattr(managed.message, "tool_calls", None),
						}
						for managed in self.history.messages
					])
				except Exception as e:
					self.logger.debug(f"sqlite message mirror skipped: {e}")

		except Exception as e:
			self.logger.error(f"Failed to save message history: {e}", exc_info=True)

	def load_from_disk(self, session_id: str, user_id: Optional[str] = None) -> bool:
		"""Load message history from disk to restore session state.

		Args:
			session_id: Session identifier
			user_id: Optional user identifier for path construction

		Returns:
			True if messages were loaded, False if no saved history exists
		"""
		from agents.task.path import pm

		load_path = pm().create_file_path(
			session_id=session_id,
			subdir_name="memory",
			filename="message_history.json",
			user_id=user_id
		)

		if not load_path.exists():
			self.logger.info(f"No saved message history found for session {session_id}")
			return False

		try:
			with open(load_path, 'r') as f:
				history_data = json.load(f)

			# Reconstruct messages
			message_classes = {
				"HumanMessage": HumanMessage,
				"AIMessage": AIMessage,
				"SystemMessage": SystemMessage,
				"ToolMessage": ToolMessage
			}

			messages_loaded = 0
			for msg_data in history_data["messages"]:
				msg_class = message_classes.get(msg_data["type"])
				if not msg_class:
					self.logger.warning(f"Unknown message type: {msg_data['type']}, skipping")
					continue

				# Reconstruct message
				message = msg_class(content=msg_data["content"])
				# P4: restore the true source (pre-P4 files have no key → USER, the
				# historical behaviour).
				message.origin = msg_data.get("origin", MessageOrigin.USER)
				# CX-H1: restore the state-message tag so a rehydrated session can
				# still target it by identity (pre-tag files have no key → {}, the
				# legacy shape-heuristic fallback in remove_last_state_message
				# still applies to those).
				if msg_data.get("msg_metadata"):
					message.metadata = msg_data["msg_metadata"]

				# Add tool call data if present
				if msg_data.get("tool_calls"):
					message.tool_calls = msg_data["tool_calls"]
				if msg_data.get("tool_call_id"):
					message.tool_call_id = msg_data["tool_call_id"]

				# Add to history (this will recalculate tokens). Restoring persisted
				# AI/Tool messages is a trusted internal path, so bypass the single-writer
				# guard (which otherwise rejects re-adding AIMessage/ToolMessage and aborts
				# the whole restore → a warm session resume gets stuck 'initializing').
				self.add_message(message, _internal=True)
				messages_loaded += 1
				# P4: keep the ORIGINAL turn timestamp/source (add_message stamps
				# "now", which would erase message age on every rehydrate).
				saved_meta = msg_data.get("metadata") or {}
				restored = self.history.messages[-1].metadata
				if saved_meta.get("timestamp"):
					restored.timestamp = saved_meta["timestamp"]
				if saved_meta.get("source"):
					restored.source = saved_meta["source"]

			self.logger.info(f"📂 Loaded {messages_loaded} messages from {load_path}")
			return True

		except Exception as e:
			self.logger.error(f"Failed to load message history: {e}", exc_info=True)
			return False
