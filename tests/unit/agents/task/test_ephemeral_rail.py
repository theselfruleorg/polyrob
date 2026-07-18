"""B1/B2/B3 (2026-07-13 correspondent review): the ephemeral delivery rail must
wake an idle session, survive eviction, and be bounded.

- B1: ``_session_has_pending_input`` must treat queued ephemerals (correspondent
  replies) as pending input — otherwise a reply into a resident+``completed``
  session sits unconsumed until the owner's next genuine turn (the no-op resume
  guard in ``run_session`` skips the wake).
- B2: unconsumed ephemerals must survive ``save_to_disk``/``load_from_disk``
  (eviction/restart) — they were never persisted, so an idle-arrived
  correspondent reply was silently lost.
- B3: the ephemeral queue must be capped (``MAX_EPHEMERAL_MESSAGES``, default 30,
  drop-oldest) — the HITL queue has backpressure; this rail had none.
"""
import types

import pytest
from unittest.mock import MagicMock

import agents.task.agent.service  # noqa: F401 (import order)
from agents.task.agent.message_manager.service import MessageManager
from agents.task.agent.prompts import SystemPrompt
from agents.task.path import get_path_manager, set_path_manager
from modules.llm.messages import HumanMessage, MessageOrigin, make_control_message

SESSION = "test-ephemeral-rail"
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


# --- B1: pending-input must see ephemerals -----------------------------------

class _HITLFake:
    def get_queue_size(self):
        return 0


class _MMFake:
    def __init__(self, ephemeral=None, pending=None):
        self._ephemeral_messages = list(ephemeral or [])
        self._ephemeral_pending = list(pending or [])


class _AgentFake:
    def __init__(self, mm):
        self.hitl_manager = _HITLFake()
        self.message_manager = mm


class _OrchFake:
    def __init__(self, mm):
        self._pending_messages = []
        self.agents = {"a": _AgentFake(mm)}


def _task_agent_with(orch):
    from agents.task_agent_lite import TaskAgent
    ta = object.__new__(TaskAgent)  # no __init__: only _registry is touched
    ta._registry = types.SimpleNamespace(get=lambda sid: orch)
    return ta


def test_pending_input_sees_queued_ephemeral():
    orch = _OrchFake(_MMFake(ephemeral=[object()]))
    ta = _task_agent_with(orch)
    assert ta._session_has_pending_input(SESSION) is True


def test_pending_input_sees_inflight_ephemeral():
    """A consumed-but-uncommitted ephemeral (LLM call in flight/failed) still counts."""
    orch = _OrchFake(_MMFake(pending=[object()]))
    ta = _task_agent_with(orch)
    assert ta._session_has_pending_input(SESSION) is True


def test_pending_input_false_when_all_queues_empty():
    orch = _OrchFake(_MMFake())
    ta = _task_agent_with(orch)
    assert ta._session_has_pending_input(SESSION) is False


# --- B3: bounded ephemeral queue ----------------------------------------------

def test_push_ephemeral_caps_queue_drop_oldest(monkeypatch):
    monkeypatch.setenv("MAX_EPHEMERAL_MESSAGES", "5")
    mm = _mm()
    for i in range(8):
        mm.push_ephemeral_message(HumanMessage(content=f"eph-{i}"))
    assert len(mm._ephemeral_messages) == 5
    contents = [m.content for m in mm._ephemeral_messages]
    assert contents == [f"eph-{i}" for i in range(3, 8)], (
        "overflow must drop the OLDEST ephemerals, keeping the newest")


def test_push_ephemeral_default_cap_allows_normal_use(monkeypatch):
    monkeypatch.delenv("MAX_EPHEMERAL_MESSAGES", raising=False)
    mm = _mm()
    for i in range(3):
        mm.push_ephemeral_message(HumanMessage(content=f"eph-{i}"))
    assert len(mm._ephemeral_messages) == 3


# --- B2: ephemerals survive save/load -----------------------------------------

def test_unconsumed_ephemeral_survives_save_load(tmp_data_root):
    mm = _mm()
    mm.add_message(HumanMessage(content="owner turn"))
    mm.push_ephemeral_message(
        make_control_message("correspondent reply body", MessageOrigin.CORRESPONDENT))
    mm.save_to_disk(SESSION, USER)

    mm2 = _mm()
    assert mm2.load_from_disk(SESSION, USER) is True
    assert len(mm2._ephemeral_messages) == 1
    restored = mm2._ephemeral_messages[0]
    assert "correspondent reply body" in restored.content
    assert restored.origin == MessageOrigin.CORRESPONDENT
    # restored ephemerals are NOT part of committed history
    assert all("correspondent reply body" not in str(m.message.content)
               for m in mm2.history.messages)


def test_no_ephemeral_key_when_queue_empty(tmp_data_root):
    mm = _mm()
    mm.add_message(HumanMessage(content="owner turn"))
    mm.save_to_disk(SESSION, USER)
    mm2 = _mm()
    assert mm2.load_from_disk(SESSION, USER) is True
    assert mm2._ephemeral_messages == []
