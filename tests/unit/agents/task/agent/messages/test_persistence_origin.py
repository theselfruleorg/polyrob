"""P4 (2026-07-02 architecture fix): persist message origin + timestamps.

Bug: ``save_to_disk`` persisted only type/content/tokens — ``BaseMessage.origin``
was stripped, and ``load_from_disk`` reconstructed every message with the default
``origin=USER``. A rehydrated session therefore could not tell a forged/self-wake/
system turn from a genuine owner message (prod session fa1212de: 100+ messages,
all indistinguishable). ``MessageOrigin`` also had no SELF_WAKE value, and
``MessageMetadata.timestamp`` was never populated, so nothing downstream could
reason about message age.
"""
import json

import pytest
from unittest.mock import MagicMock

import agents.task.agent.service  # noqa: F401 (import order)
from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt
from agents.task.path import get_path_manager, set_path_manager, pm
from modules.llm.messages import (
    HumanMessage,
    MessageOrigin,
    make_control_message,
)

SESSION = "test-origin-session"
USER = "u_test"


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


def test_self_wake_origin_exists():
    assert MessageOrigin.SELF_WAKE == "self_wake"


def test_origin_survives_save_load_roundtrip(tmp_data_root):
    mm = _mm()
    mm.add_message(HumanMessage(content="real owner question", origin=MessageOrigin.USER))
    mm.add_message(make_control_message("forged wake turn", MessageOrigin.SELF_WAKE))
    mm.add_message(make_control_message("guidance text", MessageOrigin.GUIDANCE))
    mm.save_to_disk(SESSION, USER)

    mm2 = _mm()
    assert mm2.load_from_disk(SESSION, USER) is True
    origins = [m.message.origin for m in mm2.history.messages]
    assert MessageOrigin.SELF_WAKE in origins
    assert MessageOrigin.GUIDANCE in origins
    assert MessageOrigin.USER in origins
    # order preserved: user turn first, then self-wake, then guidance
    assert origins[-3:] == [
        MessageOrigin.USER, MessageOrigin.SELF_WAKE, MessageOrigin.GUIDANCE
    ]


def test_legacy_file_without_origin_defaults_to_user(tmp_data_root):
    mm = _mm()
    mm.add_message(HumanMessage(content="hello"))
    mm.save_to_disk(SESSION, USER)

    # Simulate a pre-P4 file: strip the origin key from every message
    path = pm().create_file_path(
        session_id=SESSION, subdir_name="memory",
        filename="message_history.json", user_id=USER,
    )
    data = json.loads(path.read_text())
    for m in data["messages"]:
        m.pop("origin", None)
    path.write_text(json.dumps(data))

    mm2 = _mm()
    assert mm2.load_from_disk(SESSION, USER) is True
    assert all(m.message.origin == MessageOrigin.USER for m in mm2.history.messages)


def test_timestamp_populated_on_add_and_roundtrips(tmp_data_root):
    mm = _mm()
    mm.add_message(HumanMessage(content="timestamped turn"))
    original_ts = mm.history.messages[-1].metadata.timestamp
    assert original_ts, "metadata.timestamp must be populated at add time"

    mm.save_to_disk(SESSION, USER)
    mm2 = _mm()
    assert mm2.load_from_disk(SESSION, USER) is True
    assert mm2.history.messages[-1].metadata.timestamp == original_ts
