"""Task 18: Checkpoint fidelity — origin round-trip, deque restore, keep the task pinned.

Three independent bugs (CX-M1/M2/M3):

- CX-M1: ``checkpoint_history``/``restore_from_checkpoint_file`` do NOT serialize/
  restore ``BaseMessage.origin`` (nor the ``msg_metadata`` tag dict), unlike
  ``save_to_disk``/``load_from_disk`` which do. A file-checkpoint restore silently
  turns every SELF_WAKE/CORRESPONDENT/COMPACTION_SUMMARY turn into a plain USER
  message.
- CX-M2: ``restore_from_checkpoint`` assigns ``self.history.messages`` to the raw
  checkpoint list (aliased, and not a ``deque``), so later appends mutate the
  checkpoint and eviction bookkeeping (``maxlen``) is lost.
- CX-M3: the image-guidance path in ``inject_user_guidance`` un-pins
  ``_initial_task_message`` (sets it to ``None``) whenever images are present on
  the first (non-continuation) guidance turn, breaking the "initial task is never
  evicted" invariant.
"""
import json
from collections import deque

import pytest
from unittest.mock import MagicMock

from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt
from agents.task.path import get_path_manager, set_path_manager
from modules.llm.messages import HumanMessage, MessageOrigin, make_control_message

SESSION = "test-checkpoint-fidelity"

FAKE_IMG = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}


class _FakeChatModel:
	"""Minimal LLM stand-in — MessageManager only reads ``model_name`` off it."""

	def __init__(self, model_name: str = "gpt-5"):
		self.model_name = model_name


def _mm(session_id=SESSION):
	llm = MagicMock()
	llm.model_name = "gpt-4o"
	return MessageManager(
		llm=llm, task="Test task", action_descriptions="acts",
		system_prompt_class=SystemPrompt, max_input_tokens=4000,
		session_id=session_id,
	)


@pytest.fixture()
def tmp_data_root(tmp_path):
	set_path_manager(get_path_manager(data_root=str(tmp_path)))
	yield tmp_path


def test_checkpoint_file_roundtrip_preserves_origin(tmp_data_root):
	"""CX-M1: a SELF_WAKE-origin message must survive checkpoint→restore-from-file."""
	mm = _mm()
	mm.add_message(HumanMessage(content="real owner question", origin=MessageOrigin.USER))
	mm.add_message(make_control_message("forged wake turn", MessageOrigin.SELF_WAKE))

	checkpoint_path = tmp_data_root / "checkpoint.json"
	mm.checkpoint_history(filepath=checkpoint_path)

	mm2 = _mm()
	assert mm2.restore_from_checkpoint_file(checkpoint_path) is True

	origins = [m.message.origin for m in mm2.history.messages]
	assert MessageOrigin.SELF_WAKE in origins, (
		"checkpoint restore must preserve message.origin, not silently default to USER"
	)
	assert origins[-2:] == [MessageOrigin.USER, MessageOrigin.SELF_WAKE]


def test_checkpoint_file_roundtrip_preserves_msg_metadata(tmp_data_root):
	"""CX-M1 (metadata parity with save_to_disk/load_from_disk)."""
	mm = _mm()
	msg = HumanMessage(content="tagged state msg")
	msg.metadata = {"tag": "state_message"}
	mm.add_message(msg)

	checkpoint_path = tmp_data_root / "checkpoint.json"
	mm.checkpoint_history(filepath=checkpoint_path)

	mm2 = _mm()
	assert mm2.restore_from_checkpoint_file(checkpoint_path) is True
	restored = mm2.history.messages[-1].message
	assert getattr(restored, "metadata", None) == {"tag": "state_message"}


def test_restore_from_checkpoint_is_a_fresh_deque_not_aliased():
	"""CX-M2: in-memory restore must rewrap into a deque(maxlen=...), not alias the list."""
	mm = _mm()
	mm.add_message(HumanMessage(content="msg one", origin=MessageOrigin.USER))
	mm.add_message(HumanMessage(content="msg two", origin=MessageOrigin.USER))

	mm.checkpoint_history()  # in-memory only, no filepath
	checkpoint_ref = mm._history_checkpoint
	checkpoint_len_before = len(checkpoint_ref)

	assert mm.restore_from_checkpoint() is True

	assert isinstance(mm.history.messages, deque), (
		"restored history.messages must be a deque, not a plain list"
	)
	assert mm.history.messages.maxlen == mm.history.max_messages

	# Mutate the restored history — the original checkpoint list must not change.
	mm.add_message(HumanMessage(content="msg three", origin=MessageOrigin.USER))
	assert len(checkpoint_ref) == checkpoint_len_before, (
		"appending after restore must not mutate the stored checkpoint (no aliasing)"
	)
	assert len(mm.history.messages) == checkpoint_len_before + 1


def _stringify(content) -> str:
	return content if isinstance(content, str) else json.dumps(content)


def test_image_guidance_keeps_initial_task_pinned():
	"""CX-M3: the first image-bearing guidance turn must not un-pin the task,
	AND (B3) the task must still lead the guidance in the emitted message list,
	with the image attachment preserved on the guidance turn."""
	mm = _mm()
	assert mm._initial_task_message is not None, "sanity: task should start pinned"

	mm.inject_user_guidance([
		{"text": "look at this", "kind": "comment", "metadata": {"image_attachments": [FAKE_IMG]}}
	])

	assert mm._initial_task_message is not None, (
		"initial task message must remain pinned even when the first guidance "
		"turn carries images"
	)

	for getter in (mm.get_messages, mm.get_messages_for_llm):
		texts = [_stringify(m.content) for m in getter()]
		blob = "\n".join(texts)

		# Foundation ordering: the pinned task precedes the guidance turn.
		task_idx = next((i for i, t in enumerate(texts) if "Test task" in t), None)
		guidance_idx = next((i for i, t in enumerate(texts) if "look at this" in t), None)
		assert task_idx is not None, f"{getter.__name__}: pinned task missing from output"
		assert guidance_idx is not None, f"{getter.__name__}: guidance turn missing from output"
		assert task_idx < guidance_idx, (
			f"{getter.__name__}: pinned task must lead the guidance turn "
			f"(task@{task_idx}, guidance@{guidance_idx})"
		)

		# The image attachment survived onto the guidance message.
		assert "data:image/png;base64,AAAA" in blob, (
			f"{getter.__name__}: image attachment must survive on the guidance turn"
		)
