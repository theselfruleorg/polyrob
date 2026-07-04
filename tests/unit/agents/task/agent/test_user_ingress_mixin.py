"""P9 pass-5 — UserIngressMixin extracted from service.py."""
import logging
import types

import pytest

from agents.task.agent.core.user_ingress import UserIngressMixin


def test_agent_composes_user_ingress_mixin():
    from agents.task.agent.service import Agent
    assert issubclass(Agent, UserIngressMixin)
    for m in ("receive_user_message", "_drain_user_messages", "request_approval",
              "record_approval_decision", "_check_todo_completion", "_emit_todo_status"):
        assert getattr(Agent, m).__qualname__.startswith("UserIngressMixin")


class _Host(UserIngressMixin):
    def __init__(self):
        self.logger = logging.getLogger("ingress-test")
        self.agent_id = "a1"
        self.state = types.SimpleNamespace(n_steps=0)
        self.telemetry_manager = types.SimpleNamespace(capture_event=lambda e: None)


@pytest.mark.asyncio
async def test_receive_user_message_queues_via_hitl():
    h = _Host()
    queued = []

    class _HITL:
        async def queue_user_message(self, text, kind, metadata):
            queued.append((text, kind, metadata))

    h.hitl_manager = _HITL()
    await h.receive_user_message("hello", kind="comment")
    assert queued == [("hello", "comment", {})]


@pytest.mark.asyncio
async def test_request_approval_auto_approves():
    h = _Host()
    assert await h.request_approval("because", "checkpoint") is True
