"""An unreadable message_history.json is not "no history".

A torn file (or one row without "content", which raised half way through the
restore) returned False, and the next ``save_to_disk`` replaced the only copy.
The load now drops whatever it half-restored, moves the file aside as
``<name>.corrupt-<epoch>`` (never deletes it) and raises
``UnreadableHistoryError`` naming where it is kept.
"""
import json

import pytest
from unittest.mock import MagicMock

import agents.task.agent.service  # noqa: F401 (import order)
from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.messages.persistence import UnreadableHistoryError
from agents.task.agent.prompts import SystemPrompt
from agents.task.path import get_path_manager, set_path_manager, pm
from modules.llm.messages import HumanMessage

SESSION = "test-unreadable-session"
USER = "u_test"


def _mm():
    llm = MagicMock()
    llm.model_name = "gpt-4o"
    return MessageManager(
        llm=llm, task="Test task", action_descriptions="acts",
        system_prompt_class=SystemPrompt, max_input_tokens=4000,
        session_id=SESSION,
    )


@pytest.fixture()
def history_path(tmp_path):
    set_path_manager(get_path_manager(data_root=str(tmp_path)))
    return pm().create_file_path(session_id=SESSION, subdir_name="memory",
                                 filename="message_history.json", user_id=USER)


def _corrupt_copies(path):
    return sorted(path.parent.glob(path.name + ".corrupt-*"))


def test_torn_file_is_kept_aside_and_raises(history_path):
    history_path.write_text('{"messages": [{"type": "HumanMessage", "cont')
    mm = _mm()
    with pytest.raises(UnreadableHistoryError) as ei:
        mm.load_from_disk(SESSION, USER)
    kept = _corrupt_copies(history_path)
    assert len(kept) == 1 and not history_path.exists()
    assert str(kept[0]) == str(ei.value.kept_at)
    assert kept[0].read_text().startswith('{"messages"')


def test_half_restored_history_is_dropped(history_path):
    mm = _mm()
    before = [m.message.content for m in mm.history.messages]
    tokens_before = mm.history.total_tokens
    history_path.write_text(json.dumps({"messages": [
        {"type": "HumanMessage", "content": "first row loads"},
        {"type": "HumanMessage"},  # no content: raised half way
    ]}))
    with pytest.raises(UnreadableHistoryError):
        mm.load_from_disk(SESSION, USER)
    assert [m.message.content for m in mm.history.messages] == before
    assert mm.history.total_tokens == tokens_before
    # The next save writes a fresh file; the unreadable one stays aside.
    mm.add_message(HumanMessage(content="new turn"))
    mm.save_to_disk(SESSION, USER)
    assert history_path.exists() and len(_corrupt_copies(history_path)) == 1


def test_no_file_is_still_no_history(history_path):
    assert _mm().load_from_disk(SESSION, USER) is False
    assert _corrupt_copies(history_path) == []
