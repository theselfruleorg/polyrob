"""P7 (2026-07-02): the STEER ingress relay must preserve message kind end-to-end.

A Telegram STEER queues via orchestrator.submit_user_message BEFORE run_session
is spawned. Two seams matter:
- no agent yet (recreated orchestrator): the message parks in _pending_messages
  as (text, kind, metadata) — create_agent later flushes it into the agent's
  HITL queue with the SAME kind (execution.py flush loop);
- agent resident: it routes straight to hitl_manager.queue_user_message.

``kind`` drives the origin stamping in inject_user_guidance (P1/P4): if it were
lost here, a genuine owner comment could be demoted to a forged turn or vice
versa.
"""
import asyncio
import logging
import pathlib

from unittest.mock import AsyncMock, MagicMock

from agents.task.session.hitl_ingress import HITLIngressMixin


class _TinyOrch(HITLIngressMixin):
    def __init__(self):
        self.agents = {}
        self._pending_messages = []
        self._pending_messages_lock = asyncio.Lock()
        self.logger = logging.getLogger("test.steer")
        self.session_id = "s"
        self.user_id = "u"


def test_pre_agent_pending_preserves_kind_and_metadata():
    o = _TinyOrch()
    asyncio.run(o.submit_user_message(
        agent_id=None, text="yo", kind="delegation_result", metadata={"m": 1}))
    assert o._pending_messages == [("yo", "delegation_result", {"m": 1})]


def test_resident_agent_receives_same_kind():
    o = _TinyOrch()
    agent = MagicMock()
    agent.agent_id = "executor_s"
    agent.hitl_manager = MagicMock()
    agent.hitl_manager.get_queue_size.return_value = 0
    agent.hitl_manager.queue_user_message = AsyncMock()
    o.agents = {"executor_s": agent}

    asyncio.run(o.submit_user_message(
        agent_id=None, text="yo", kind="delegation_result", metadata={"m": 1}))
    agent.hitl_manager.queue_user_message.assert_awaited_once_with(
        "yo", "delegation_result", {"m": 1})


def test_create_agent_flush_forwards_kind():
    # The create_agent flush loop must forward the parked tuple verbatim
    # (text, kind, metadata) — pin the source so a refactor can't drop kind.
    src = (pathlib.Path(__file__).resolve().parents[4]
           / "agents" / "task" / "session" / "execution.py").read_text()
    assert "for text, kind, metadata in self._pending_messages:" in src
    assert "await agent.hitl_manager.queue_user_message(text, kind, metadata)" in src
