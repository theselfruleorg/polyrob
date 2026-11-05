"""An owner's chat message gets an answer: one correction at done(), then a line."""
from types import SimpleNamespace

import pytest

from agents.task.runtime import owner_answer
from core.surfaces.turn_reply import mark_reply_published, reset_turn


def _agent(words="what is going on?", bound=True, sub=False):
    injected = []
    orch = SimpleNamespace(_owner_turn_text=words, _message_router=object() if bound else None,
                           _chat_session_key="agent:main:telegram:dm:1" if bound else None)
    manager = SimpleNamespace(
        inject_runtime_guidance=lambda text, **kw: injected.append((text, kw["source"])))
    return SimpleNamespace(orchestrator=orch, message_manager=manager, _is_sub_agent=sub,
                           session_id="s"), injected


@pytest.fixture
def told(monkeypatch):
    lines = []

    async def tell(_agent, _orch, line):
        lines.append(line)
    monkeypatch.setattr(owner_answer, "_tell_user", tell)
    monkeypatch.setattr("core.surfaces.room_policy.is_public_session",
                        lambda orch: getattr(orch, "public", False))
    monkeypatch.setattr("core.surfaces.user_delivery.record_interactive_reply", lambda *_: None)
    return lines


@pytest.mark.asyncio
async def test_a_silent_done_gets_one_correction_that_quotes_the_owner_then_a_line(told):
    agent, injected = _agent()
    assert await owner_answer.check_owner_answered(agent) is True
    assert "what is going on?" in injected[0][0] and injected[0][1] == "owner_answer"
    assert told == []
    assert await owner_answer.check_owner_answered(agent) is False
    assert told == [owner_answer.NOT_ANSWERED_LINE] and len(injected) == 1


@pytest.mark.asyncio
async def test_the_last_step_has_no_room_for_a_correction(told):
    agent, injected = _agent()
    assert await owner_answer.check_owner_answered(agent, final=True) is False
    assert injected == [] and told == [owner_answer.NOT_ANSWERED_LINE]


@pytest.mark.asyncio
async def test_a_reply_passes_and_the_next_turn_is_checked_again(told):
    agent, injected = _agent()
    mark_reply_published(agent.orchestrator, "here is the answer")
    assert await owner_answer.check_owner_answered(agent) is False
    reset_turn(agent.orchestrator)
    agent.orchestrator._owner_turn_text = "and now?"
    assert await owner_answer.check_owner_answered(agent) is True
    assert len(injected) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs", [{"words": ""}, {"bound": False}, {"sub": True}])
async def test_no_owner_words_an_unbound_seat_and_a_sub_agent_are_not_checked(told, kwargs):
    agent, injected = _agent(**kwargs)
    assert await owner_answer.check_owner_answered(agent) is False
    assert injected == [] and told == []


@pytest.mark.asyncio
async def test_a_room_may_stay_silent(told):
    agent, injected = _agent()
    agent.orchestrator.public = True
    assert await owner_answer.check_owner_answered(agent) is False
    assert injected == [] and told == []


@pytest.mark.asyncio
async def test_the_same_words_sent_again_earn_a_correction_again(told):
    agent, injected = _agent()
    assert await owner_answer.check_owner_answered(agent) is True
    assert await owner_answer.check_owner_answered(agent) is False
    assert told == [owner_answer.NOT_ANSWERED_LINE]
    assert await owner_answer.check_owner_answered(agent) is True
    assert len(injected) == 2
