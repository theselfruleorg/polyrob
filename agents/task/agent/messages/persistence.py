from __future__ import annotations

import copy
import json
import logging
import os
import shutil
import threading
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

from modules.llm.call_result import replay_fields
from agents.task.agent.message_manager.views import MessageHistory, MessageMetadata, ManagedMessage

from agents.task.agent.messages.retrieval import drop_usage_anchor

logger = logging.getLogger(__name__)

# UP-10 2.3: one-time guard so the write-only sqlite mirror warning fires once per
# process, not on every save_to_disk.
_warned_sqlite_mirror = False


class UnreadableHistoryError(Exception):
	"""A saved message history exists and could not be read.

	Not "no history": the file is moved aside (``kept_at``), never deleted, so the
	next ``save_to_disk`` cannot replace the only copy."""

	def __init__(self, kept_at: Path, cause: BaseException):
		self.kept_at = kept_at
		super().__init__(f"message history unreadable ({cause}); kept at {kept_at}")


def _keep_unreadable(path: Path, log: logging.Logger) -> Path:
	"""Move an unreadable history to ``<name>.corrupt-<epoch>``; never delete it."""
	import time
	target = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
	n = 1
	while target.exists():
		target = path.with_name(f"{path.name}.corrupt-{int(time.time())}-{n}")
		n += 1
	try:
		os.replace(path, target)
	except OSError as e:
		log.error(f"Unreadable message history {path} could not be moved aside ({e}); "
		          "the next save will replace it")
		return path
	log.warning(f"Unreadable message history moved aside to {target}")
	return target


class PersistenceMixin:
	# F29: empty slots so the composed MessageManager keeps its own __slots__ and
	# never grows a __dict__. This is only TRUE while EVERY class in the MRO
	# declares one — three mixins omitted it until 2026-09-22, so the claim in
	# this comment was false for as long as it had been written.
	__slots__ = ()

	def checkpoint_history(self) -> None:
		"""Keep an in-memory copy of the message history for :meth:`restore_from_checkpoint`."""
		try:
			self._history_checkpoint = [
				ManagedMessage(
					message=copy.deepcopy(m.message),
					metadata=copy.deepcopy(m.metadata)
				)
				for m in self.history.messages
			]
			self._checkpoint_token_count = self.history.total_tokens
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
				msg_data.update(replay_fields(getattr(msg, 'additional_kwargs', None)))

				history_data["messages"].append(msg_data)

			# B2 (2026-07-13 correspondent review): persist UNCONSUMED one-shot
			# ephemerals (correspondent replies / recall notes). They are not part of
			# committed history, but losing them on eviction/restart silently dropped
			# a correspondent reply that arrived while the session was idle. In-flight
			# (`_ephemeral_pending`) entries come first — they are older.
			try:
				pending_ephemeral = []
				for eph in (list(getattr(self, '_ephemeral_pending', None) or [])
							+ list(getattr(self, '_ephemeral_messages', None) or [])):
					if (getattr(eph, "metadata", None) or {}).get("per_call"):
						continue  # rebuilt by each call: a restored copy would ride twice
					pending_ephemeral.append({
						"type": eph.__class__.__name__,
						"content": eph.content,
						"origin": getattr(eph, "origin", MessageOrigin.USER),
						"msg_metadata": getattr(eph, "metadata", None) or {},
					})
				if pending_ephemeral:
					history_data["pending_ephemeral"] = pending_ephemeral
			except Exception as e:
				self.logger.debug(f"ephemeral persistence skipped: {e}")

			# F13: persist the RENDERED foundation (system prompt, runtime identity,
			# <environment>, self/project context, initial task, skill + tool catalogs)
			# and the emitted tool order alongside the history, so a restart can
			# re-issue the same prefix bytes instead of re-rendering every block from
			# the live environment. One file — no sidecar. Fail-soft: without the key
			# load_from_disk simply rebuilds, which is the pre-F13 behaviour.
			try:
				from agents.task.agent.messages.foundation_replay import capture_foundation
				history_data["foundation"] = capture_foundation(self)
			except Exception as e:
				self.logger.debug(f"foundation snapshot skipped: {e}")

			# Save to disk
			save_path.parent.mkdir(parents=True, exist_ok=True)
			# 102 C-10: a crash in a direct dump truncated the only history copy.
			tmp_path = save_path.with_name(f"{save_path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
			try:
				with open(tmp_path, 'w') as f:
					json.dump(history_data, f, indent=2)
				if save_path.exists():
					shutil.copymode(save_path, tmp_path)
				os.replace(tmp_path, save_path)
			except BaseException:
				tmp_path.unlink(missing_ok=True)
				raise

			self.logger.info(f"💾 Saved {len(self.history.messages)} messages to {save_path}")

			# Opt-in durable mirror (MESSAGE_STORE_BACKEND=sqlite). Additive: JSON stays
			# the source of truth. UP-10 2.3: the read path is intentionally NOT wired —
			# load_from_disk only reads message_history.json (see below), so this mirror
			# is write-only and can silently diverge. A correct read path needs payload
			# parity (tool_call_id + metadata) and a JSON/DB reconciliation policy; that is
			# scoped to a dedicated durable-store proposal. We keep the pre-staged write but
			# emit a one-time WARN so enabling the flag is honest about being write-only.
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
					from core.runtime_paths import sidecar_db_path
					store = SqliteMessageStore(str(sidecar_db_path("messages.db")))
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

		Raises:
			UnreadableHistoryError: the file exists and could not be read. Whatever
				the load half-restored is dropped and the file is moved aside.
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

		from collections import deque
		kept_messages = list(self.history.messages)
		kept_tokens = self.history.total_tokens
		kept_ephemeral = len(self._ephemeral_messages)
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
				message.additional_kwargs.update(replay_fields(msg_data))

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

			# B2: restore unconsumed ephemerals (never into committed history; no
			# token accounting — same contract as push_ephemeral_message). Appended
			# directly: they were already sensitive-data-filtered before saving.
			try:
				restored_ephemeral = 0
				for eph_data in history_data.get("pending_ephemeral") or []:
					eph_class = message_classes.get(eph_data.get("type"))
					if not eph_class:
						continue
					eph = eph_class(content=eph_data.get("content", ""))
					eph.origin = eph_data.get("origin", MessageOrigin.USER)
					if eph_data.get("msg_metadata"):
						eph.metadata = eph_data["msg_metadata"]
					self._ephemeral_messages.append(eph)
					restored_ephemeral += 1
				if restored_ephemeral:
					self.logger.info(
						f"📂 Restored {restored_ephemeral} pending ephemeral message(s)")
			except Exception as e:
				self.logger.debug(f"ephemeral restore skipped: {e}")

			# F13: prefer the persisted foundation bytes over the freshly rendered
			# ones, so the prompt prefix survives the restart. No-op (and the fresh
			# render stands) when the file predates F13, when the blob is
			# incomplete, or when model/provider/cwd changed.
			try:
				from agents.task.agent.messages.foundation_replay import replay_foundation
				replay_foundation(self, history_data.get("foundation"), log=self.logger)
			except Exception as e:
				self.logger.debug(f"foundation replay skipped: {e}")
			return True

		except Exception as e:
			self.logger.error(f"Failed to load message history: {e}", exc_info=True)
			# A half-restored history is not a history: drop it, then keep the file.
			self.history.messages = deque(kept_messages, maxlen=self.history.max_messages)
			self.history.total_tokens = kept_tokens
			del self._ephemeral_messages[kept_ephemeral:]
			raise UnreadableHistoryError(_keep_unreadable(load_path, self.logger), e) from e
