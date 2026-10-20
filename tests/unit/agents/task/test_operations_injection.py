"""Review 2026-10-15 rec. 2: the ``<operations>`` note reaches the owner-memory
regimes only (owner chat, cron, goal, autonomous) — never a room, a
correspondent or a child — and a builder failure says "could not read"."""
import asyncio
import logging
import types

import pytest

from agents.task.agent.core import live_health


class _MM:
    def __init__(self):
        self.pushed = []

    def push_ephemeral_message(self, msg):
        self.pushed.append(msg)


class _Agent(live_health.LiveHealthMixin):
    def __init__(self, *, public=False, correspondent=False, child=False, step=1):
        self.orchestrator = types.SimpleNamespace(
            _public_session=public, _correspondent_session=correspondent,
            _correspondent_tainted=False, container=None, task_agent=None)
        self.state = types.SimpleNamespace(n_steps=step)
        self._is_sub_agent = child
        self.session_id = "s"
        self.user_id = "u"
        self.logger = logging.getLogger("test.operations")
        self.message_manager = _MM()


@pytest.fixture(autouse=True)
def _builder(monkeypatch):
    monkeypatch.setattr(live_health, "build_operations_text", lambda *a, **k: "OPS_SENTINEL")


def _run(agent):
    asyncio.run(agent._maybe_inject_operations())
    return [m.content for m in agent.message_manager.pushed]


def test_owner_chat_gets_the_block():
    out = _run(_Agent())
    assert len(out) == 1
    assert "<operations>\nOPS_SENTINEL\n</operations>" in out[0]


def test_autonomous_run_gets_the_block(monkeypatch):
    from agents.task import session_class
    monkeypatch.setattr(session_class, "is_autonomous_session", lambda sid: True)
    assert len(_run(_Agent())) == 1


@pytest.mark.parametrize("kw", [{"public": True}, {"correspondent": True}, {"child": True}])
def test_room_correspondent_and_child_get_nothing(kw):
    assert _run(_Agent(**kw)) == []


def test_only_the_first_step_of_a_turn():
    assert _run(_Agent(step=2)) == []


def test_builder_failure_is_named_not_dropped(monkeypatch):
    def boom(*a, **k):
        raise OSError("database is locked")
    monkeypatch.setattr(live_health, "build_operations_text", boom)
    out = _run(_Agent())
    assert len(out) == 1
    assert "could not read (OSError: database is locked)" in out[0]
    assert "UNKNOWN, not none" in out[0]


def test_step_calls_the_injector():
    import inspect
    from agents.task.agent.core import step
    assert "_maybe_inject_operations()" in inspect.getsource(step)
