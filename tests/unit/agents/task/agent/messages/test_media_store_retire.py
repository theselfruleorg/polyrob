"""The store retires image bytes with the batch rule, not only the LLM copy.

The pass worked on a copy, so the deque kept every base64 image: 20 images of 1 MB stayed
stored, and each save rewrote them. The batch rule also needs retired turns to stay retired:
over a pure pass, each new image after the first batch demoted one more turn and colded
the prefix on every step.
"""
import json
from pathlib import Path

import pytest

from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.messages.filters import (
    IMAGE_STRIPPED_MARKER,
    _message_has_base64_image,
    media_keep_max,
    strip_historical_media,
)
from agents.task.agent.prompts import SystemPrompt
from agents.task.path import get_path_manager, set_path_manager
from core.surfaces.inbound_attachments import IMAGE_PATH_MARKER
from modules.llm.messages import HumanMessage
from tests.support.prefix_cache import common_prefix_len, serialize_messages


@pytest.fixture()
def tmp_data_root(tmp_path):
    set_path_manager(get_path_manager(data_root=str(tmp_path)))
    yield tmp_path


class _Model:
    model_name = "gpt-5"


def _mm(session_id="s-media"):
    return MessageManager(llm=_Model(), task="Test task", action_descriptions="Test actions",
                          system_prompt_class=SystemPrompt, max_input_tokens=400000,
                          image_tokens=800, session_id=session_id)


def _image_turn(step, *, size=64, path=None):
    text = f"photo {step}"
    if path:
        text += f"\n{IMAGE_PATH_MARKER}{path}"
    return HumanMessage(content=[
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * size + str(step)}},
    ])


def _stored(mm):
    return [m.message for m in mm.history.messages]


def _stored_images(mm):
    return sum(1 for m in _stored(mm) if _message_has_base64_image(m))


def test_the_store_stays_at_or_under_keep_max_images():
    mm = _mm()
    for step in range(20):
        mm._add_message_with_tokens(_image_turn(step))
        mm.get_messages_for_llm(consume_ephemeral=False)
        assert _stored_images(mm) <= media_keep_max(), step


def test_store_bytes_fall_with_the_images():
    mm = _mm()
    size = 1024 * 1024
    for step in range(20):
        mm._add_message_with_tokens(_image_turn(step, size=size))
        mm.get_messages_for_llm(consume_ephemeral=False)
    assert mm.get_memory_usage_estimate() < (media_keep_max() + 1) * size * 1.5


def test_the_deque_is_not_cut():
    mm = _mm()
    for step in range(20):
        mm._add_message_with_tokens(_image_turn(step))
        mm.get_messages_for_llm(consume_ephemeral=False)
    assert len(mm.history.messages) == 20
    assert "photo 0" in str(_stored(mm)[0].content)


def test_retired_turns_are_byte_identical_across_steps():
    mm = _mm()
    retired = {}
    for step in range(30):
        mm._add_message_with_tokens(_image_turn(step, path=f"inbound/p{step}.png"))
        mm.get_messages_for_llm(consume_ephemeral=False)
        for idx, msg in enumerate(_stored(mm)):
            if _message_has_base64_image(msg):
                continue
            text = json.dumps(msg.content, sort_keys=True)
            assert retired.setdefault(idx, text) == text, (idx, step)
    assert retired


def test_the_prefix_is_cold_once_per_batch_through_the_real_assembly():
    mm = _mm()
    previous, cold = None, 0
    for step in range(30):
        mm._add_message_with_tokens(_image_turn(step))
        current = serialize_messages(mm.get_messages_for_llm(consume_ephemeral=False))
        if previous is not None and common_prefix_len(previous, current) != len(previous):
            cold += 1
        previous = current
    # ceiling 6, batch 4: the first crossing at the 7th image, then one per 4 images.
    assert cold <= 7, f"{cold} of 29 steps were cold"


def test_a_retired_turn_names_the_saved_path():
    mm = _mm()
    for step in range(7):
        mm._add_message_with_tokens(_image_turn(step, path=f"inbound/p{step}.png"))
    mm.get_messages_for_llm(consume_ephemeral=False)
    blocks = _stored(mm)[0].content
    stripped = [b["text"] for b in blocks if b.get("type") == "text" and IMAGE_STRIPPED_MARKER in b["text"]]
    assert stripped == [f"{IMAGE_STRIPPED_MARKER}\n{IMAGE_PATH_MARKER}inbound/p0.png"]


def test_a_turn_with_two_images_names_each_path_in_order():
    turn = HumanMessage(content=[
        {"type": "text", "text": f"{IMAGE_PATH_MARKER}a.png\n{IMAGE_PATH_MARKER}b.png"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,BBBB"}},
    ])
    out = strip_historical_media([turn] + [_image_turn(i) for i in range(6)])[0].content
    assert out[1]["text"].endswith("a.png")
    assert out[2]["text"].endswith("b.png")


def test_an_image_with_no_saved_path_keeps_the_plain_marker():
    out = strip_historical_media([_image_turn(i) for i in range(7)])[0].content
    assert {"type": "text", "text": IMAGE_STRIPPED_MARKER} in out


def test_token_counts_follow_the_retirement():
    mm = _mm()
    for step in range(6):
        mm._add_message_with_tokens(_image_turn(step))
    before = mm.history.total_tokens
    mm._add_message_with_tokens(_image_turn(6))
    mm._set_usage_anchor(5000)
    mm.get_messages_for_llm(consume_ephemeral=False)
    assert mm.history.total_tokens < before
    assert mm.history.total_tokens == sum(m.metadata.input_tokens for m in mm.history.messages)
    assert mm.usage_anchor() is None


def test_an_image_in_the_tail_counts_and_the_store_does_not_flap():
    """The screenshot rides the ephemeral rail. With one, the wire crosses the ceiling; the
    store must follow, or the next step without a screenshot brings the images back."""
    mm = _mm()
    for step in range(6):
        mm._add_message_with_tokens(_image_turn(step))
    mm.push_ephemeral_message(_image_turn(99))
    with_tail = mm.get_messages_for_llm(consume_ephemeral=True)
    mm.commit_ephemeral_consumption()
    without_tail = mm.get_messages_for_llm(consume_ephemeral=True)
    assert _stored_images(mm) == 2
    head = len(serialize_messages(without_tail)) - 1
    assert serialize_messages(with_tail)[:head] == serialize_messages(without_tail)[:head]


def test_retirement_reaches_the_saved_history(tmp_data_root):
    from agents.task.path import pm
    mm = _mm()
    size = 256 * 1024
    for step in range(20):
        mm._add_message_with_tokens(_image_turn(step, size=size))
        mm.get_messages_for_llm(consume_ephemeral=False)
    mm.save_to_disk("session-media", "user-a")
    saved = pm().create_file_path(session_id="session-media", subdir_name="memory",
                                  filename="message_history.json", user_id="user-a")
    assert Path(saved).stat().st_size < (media_keep_max() + 1) * size * 1.5


def test_a_saved_image_file_is_not_removed(tmp_path):
    saved = tmp_path / "p0.png"
    saved.write_bytes(b"\x89PNG")
    mm = _mm()
    for step in range(7):
        mm._add_message_with_tokens(_image_turn(step, path=str(saved)))
    mm.get_messages_for_llm(consume_ephemeral=False)
    assert saved.read_bytes() == b"\x89PNG"


def test_a_model_with_no_vision_keeps_its_rows():
	"""106 C9: the adapter sends an image as ``[IMAGE]`` to a model with no vision.
	Retirement frees no wire bytes there and rewrites a row the provider cached."""
	from tests.support.prefix_cache import wire_adapter

	adapter = wire_adapter()
	adapter._supports_vision_cached = False
	mm = MessageManager(llm=adapter, task="t", action_descriptions="a",
	                    system_prompt_class=SystemPrompt, max_input_tokens=128000,
	                    image_tokens=800, session_id="s-no-vision")
	for i in range(9):
		mm._add_message_with_tokens(_image_turn(i), _internal=True)
	before = [m.message for m in mm.history.messages]
	wire = mm.get_messages_for_llm(consume_ephemeral=False)
	assert [m.message for m in mm.history.messages] == before
	assert all(a is b for a, b in zip(before, [m.message for m in mm.history.messages]))
	assert sum("base64" in str(m.content) for m in wire) == 9
