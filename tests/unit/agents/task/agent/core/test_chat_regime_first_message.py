"""Coherency review 2026-10-13 E-A3: an owner chat pinned its FIRST message as the
undated, authoritative task for days, and matched skills only on it."""
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import agents.task.agent.service  # noqa: F401 (import order)
from agents.task.agent.core import chat_regime
from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt
from core.autonomy_marker import mark_autonomous


def _mm(sid):
    llm = MagicMock()
    llm.model_name = "gpt-4o"
    return MessageManager(llm=llm, task="bridge 1 ETH to Base", action_descriptions="acts",
                          system_prompt_class=SystemPrompt, max_input_tokens=8000,
                          session_id=sid)


def _agent(sid, **kw):
    return SimpleNamespace(session_id=sid, orchestrator=SimpleNamespace(user_id="u1"),
                           message_manager=_mm(sid), logger=logging.getLogger("t"), **kw)


def test_owner_chat_first_message_is_dated_and_labelled():
    agent = _agent("cr-chat")
    assert chat_regime.label_first_chat_message(agent) is True
    content = agent.message_manager._initial_task_message.content
    assert content.startswith("[First message of this chat, received ")
    assert "UTC" in content.split("\n", 1)[0]
    assert "the latest owner message wins" in content
    assert content.endswith("bridge 1 ETH to Base")
    assert agent.message_manager._initial_task_message.origin == "user"
    assert agent.message_manager._initial_task_tokens == \
        agent.message_manager._count_message_tokens(agent.message_manager._initial_task_message)
    # idempotent
    assert chat_regime.label_first_chat_message(agent) is False


@pytest.mark.parametrize("sid,mark", [("cr-cron", {"cron_job_id": "j1"}), ("cr-goal", {})])
def test_cron_and_goal_keep_the_authoritative_task(sid, mark):
    mark_autonomous(sid, None if mark else "g1", **mark)
    agent = _agent(sid)
    assert chat_regime.label_first_chat_message(agent) is False
    assert agent.message_manager._initial_task_message.content == "bridge 1 ETH to Base"


def test_a_child_keeps_the_authoritative_task():
    agent = _agent("cr-child", _is_sub_agent=True)
    assert chat_regime.label_first_chat_message(agent) is False


@pytest.mark.parametrize("orch_kw", [{"_public_session": True},
                                     {"_correspondent_session": True}])
def test_a_room_or_correspondent_first_message_is_not_relabelled(orch_kw):
    """pin_initial_task is False there too, but no owner turn wins
    (latest_owner_turn_wins=False): the 'latest owner message wins' label is
    an owner-chat label only."""
    agent = _agent("cr-room")
    for k, v in orch_kw.items():
        setattr(agent.orchestrator, k, v)
    assert chat_regime.label_first_chat_message(agent) is False
    assert agent.message_manager._initial_task_message.content == "bridge 1 ETH to Base"


def test_reload_relabel_skips_a_room_session():
    from agents.task.task_agent_delivery import _relabel_first_chat_message
    agent = _agent("cr-room-reload")
    agent.orchestrator._public_session = True
    _relabel_first_chat_message(agent, {"created_at": "2026-10-12T04:09:00+00:00"})
    assert agent.message_manager._initial_task_message.content == "bridge 1 ETH to Base"


def test_an_owner_turn_is_not_rematched():
    """081 Q2: the owner chat loads skills from the index; no per-turn matcher."""
    assert not hasattr(chat_regime, "rematch_skills_for_turn")
    from agents.task.agent.core import step
    import inspect
    assert "rematch_skills_for_turn" not in inspect.getsource(step)
