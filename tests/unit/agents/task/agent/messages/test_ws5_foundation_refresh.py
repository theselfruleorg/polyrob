"""2026-10-13 review A2: a reload (restart / eviction) must not bring back stale
owner rules, a stale system prompt, skills or <environment>. A layer whose fresh
render differs keeps the fresh render; byte-identical layers still replay."""
from unittest.mock import MagicMock

import pytest

import agents.task.agent.service  # noqa: F401 (import order)
from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt
from agents.task.path import get_path_manager, set_path_manager

SESSION = "ws5-a2-session"
USER = "u_ws5"


def _mm():
    llm = MagicMock()
    llm.model_name = "gpt-4o"
    return MessageManager(
        llm=llm, task="Test task", action_descriptions="acts",
        system_prompt_class=SystemPrompt, max_input_tokens=8000,
        session_id=SESSION,
    )


@pytest.fixture()
def tmp_data_root(tmp_path):
    set_path_manager(get_path_manager(data_root=str(tmp_path)))
    yield tmp_path


def _contents(mm, attr):
    v = getattr(mm, attr)
    if isinstance(v, tuple):
        return [m.content for m in v]
    return v.content if v is not None else None


def test_a_rule_approved_after_the_save_reaches_the_reloaded_chat(tmp_data_root):
    before = _mm()
    before.set_self_context_blocks((("owner_rules", "09:00 rules"),))
    before.set_skill_message("skills v1")
    before.set_environment_message("autonomy loops ENABLED")
    before.set_tool_catalog_message("- done [ready]")
    before.add_human_message("hi")
    before.save_to_disk(SESSION, USER)

    after = _mm()
    after.set_self_context_blocks((("owner_rules", "14:00 never post to X without asking"),))
    after.set_skill_message("skills v2")
    after.set_environment_message("autonomy loops PAUSED")
    after.set_tool_catalog_message("- done [ready] (rebuilt)")
    assert after.load_from_disk(SESSION, USER) is True

    assert any("14:00 never post" in c for c in _contents(after, "_self_context_blocks"))
    assert "skills v2" in _contents(after, "_skill_message")
    assert "PAUSED" in _contents(after, "_environment_message")
    # A non-rule layer still replays the saved bytes (the F13 cache rationale).
    assert "(rebuilt)" not in _contents(after, "_tool_catalog_message")


def test_identical_rule_layers_replay_byte_identically(tmp_data_root):
    before = _mm()
    before.set_self_context_blocks((("owner_rules", "same rules"),))
    before.set_skill_message("same skills")
    before.add_human_message("hi")
    before.save_to_disk(SESSION, USER)
    saved_rules = _contents(before, "_self_context_blocks")

    after = _mm()
    after.set_self_context_blocks((("owner_rules", "same rules"),))
    after.set_skill_message("same skills")
    assert after.load_from_disk(SESSION, USER) is True
    assert _contents(after, "_self_context_blocks") == saved_rules


def test_an_unlabelled_saved_first_message_is_labelled_again_on_reload(tmp_data_root, monkeypatch):
    """WS1 E-A3 x A2: a pre-label save replays its unlabelled first message;
    the reload path labels it again (owner chat), dated from session creation."""
    import types
    from agents.task.task_agent_delivery import _relabel_first_chat_message
    before = _mm()
    before.add_human_message("hi")
    before.save_to_disk(SESSION, USER)
    after = _mm()
    after.mark_initial_task_as_first_chat_message("2026-10-13 09:00 UTC")
    assert after.load_from_disk(SESSION, USER) is True
    prefix = after.FIRST_CHAT_MESSAGE_LABEL.split("{when}", 1)[0]
    assert not after._initial_task_message.content.startswith(prefix)
    agent = types.SimpleNamespace(message_manager=after, _is_sub_agent=False,
                                  orchestrator=None, session_id=None)
    _relabel_first_chat_message(agent, {"created_at": "2026-10-12T08:00:00"})
    assert after._initial_task_message.content.startswith(prefix)
    assert "2026-10-12" in after._initial_task_message.content
