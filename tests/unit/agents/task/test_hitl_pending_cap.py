"""The pre-agent pending-message queue must be bounded.

When an orchestrator has no agent yet, submit_user_message parks the message in
``_pending_messages``. Without a cap this grows unbounded — the MessageQueueFullError
backpressure (a-MED2) only applied once an agent existed. These tests pin the cap.
"""
import asyncio
import logging

import pytest

from agents.task.session.hitl_ingress import HITLIngressMixin
from core.exceptions import MessageQueueFullError


class _TinyOrch(HITLIngressMixin):
    def __init__(self):
        self.agents = {}
        self._pending_messages = []
        self._pending_messages_lock = asyncio.Lock()
        self.logger = logging.getLogger("test.hitl")
        self.session_id = "s"
        self.user_id = "u"


def test_pending_queue_raises_when_full(monkeypatch):
    monkeypatch.setenv("MAX_QUEUED_MESSAGES", "10")
    o = _TinyOrch()
    # Already at cap, no agent created yet.
    o._pending_messages = [("x", "delegation_result", {})] * 10
    with pytest.raises(MessageQueueFullError):
        # kind not in the trusted-context-ref set → no filesystem expansion side effects.
        asyncio.run(o.submit_user_message(agent_id=None, text="more", kind="delegation_result"))


def test_pending_queue_accepts_below_cap(monkeypatch):
    monkeypatch.setenv("MAX_QUEUED_MESSAGES", "10")
    o = _TinyOrch()
    asyncio.run(o.submit_user_message(agent_id=None, text="hi", kind="delegation_result"))
    assert len(o._pending_messages) == 1
