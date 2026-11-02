"""Review 2026-10-15 rec. 2 + 095 W4: the ``<operations>`` note reaches the
owner-memory regimes only (owner chat, cron, goal, autonomous) — never a room,
a correspondent or a child — through the ONE ``<state-delta>`` channel: present
at call 1, absent at call 2 when nothing changed, present again at call 3 after
a change; a builder failure says "could not read"."""
import asyncio
import logging
import types
from unittest.mock import MagicMock

import pytest

import agents.task.agent.service  # noqa: F401 (import order)
from agents.task.agent.core import live_health
from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt


def _mm() -> MessageManager:
    llm = MagicMock()
    llm.model_name = "gpt-4o"
    return MessageManager(llm=llm, task="Test task", action_descriptions="acts",
                          system_prompt_class=SystemPrompt, max_input_tokens=4000)


class _Agent(live_health.LiveHealthMixin):
    def __init__(self, *, public=False, correspondent=False, child=False, tainted=False):
        self.orchestrator = types.SimpleNamespace(
            _public_session=public, _correspondent_session=correspondent,
            _correspondent_tainted=tainted, container=None, task_agent=None)
        self.state = types.SimpleNamespace(n_steps=1)
        self._is_sub_agent = child
        self.session_id = "s"
        self.user_id = "u"
        self.controller = None
        self.logger = logging.getLogger("test.operations")
        self.message_manager = _mm()


OPS = {"text": "OPS_SENTINEL"}


@pytest.fixture(autouse=True)
def _builder(monkeypatch):
    OPS["text"] = "OPS_SENTINEL"
    monkeypatch.setattr(live_health, "build_operations_text", lambda *a, **k: OPS["text"])
    monkeypatch.setenv("LIVE_HEALTH_CONTEXT", "false")  # this file is about <operations>


def _deltas(agent):
    return [m.message.content for m in agent.message_manager.history.messages
            if "<state-delta " in str(m.message.content)]


def _step(agent):
    asyncio.run(agent._maybe_update_state_delta())
    return _deltas(agent)


def test_owner_chat_gets_the_block():
    out = _step(_Agent())
    assert len(out) == 1
    assert "<operations>\nOPS_SENTINEL\n</operations>" in out[0]


def test_present_at_call_1_absent_at_call_2_present_at_call_3_after_a_change():
    agent = _Agent()
    assert len(_step(agent)) == 1          # call 1: pushed
    assert len(_step(agent)) == 1          # call 2: unchanged -> nothing new
    OPS["text"] = "Jobs: 1 (nightly digest)"
    out = _step(agent)                     # call 3: changed -> the full new text
    assert len(out) == 2
    assert "<operations>\nJobs: 1 (nightly digest)\n</operations>" in out[1]


def test_autonomous_run_gets_the_block(monkeypatch):
    from agents.task import session_class
    monkeypatch.setattr(session_class, "is_autonomous_session", lambda sid: True)
    assert len(_step(_Agent())) == 1


@pytest.mark.parametrize("kw", [{"public": True}, {"correspondent": True}, {"child": True}])
def test_room_correspondent_and_child_get_nothing(kw):
    assert _step(_Agent(**kw)) == []


def test_taint_mid_session_scrubs_operations_from_history():
    """SK-18: the regime predicate is read on every call; a seat tainted after
    the push loses every earlier copy of <operations>."""
    agent = _Agent()
    assert "OPS_SENTINEL" in _step(agent)[0]
    agent.orchestrator._correspondent_tainted = True
    _step(agent)
    joined = "\n".join(str(m.message.content) for m in agent.message_manager.history.messages)
    assert "OPS_SENTINEL" not in joined and "<operations>" not in joined


def test_builder_failure_is_named_not_dropped(monkeypatch):
    def boom(*a, **k):
        raise OSError("database is locked")
    monkeypatch.setattr(live_health, "build_operations_text", boom)
    out = _step(_Agent())
    assert len(out) == 1
    assert "could not read (OSError: database is locked)" in out[0]
    assert "UNKNOWN, not none" in out[0]


def test_step_calls_the_one_channel():
    import inspect
    from agents.task.agent.core import step
    src = inspect.getsource(step)
    assert "_maybe_update_state_delta()" in src
    assert "_maybe_inject_operations" not in src and "_maybe_inject_live_health" not in src
