"""Correspondent taint must clear only when an owner turn actually ENTERS the session.

The clear used to run at the top of `submit_user_message`, before any of the checks
that decide whether the message is accepted. Three paths accept nothing and still
cleared the gate:

  * `agent_id` given but not found -> silent `return` (message dropped entirely)
  * pending-queue full -> MessageQueueFullError
  * HITL-queue full   -> MessageQueueFullError

In each case the capability gate re-opened every high-impact tool while the session
still held untrusted correspondent DATA and no owner turn had arrived. An attacker
who can make the queue reject (flood it, or simply catch it full) gets the gate opened
for free.

The clear now happens at the drain — the point the message provably enters the turn,
and the same place the sibling forged-turn marker is recomputed.
"""
import asyncio

import pytest

from agents.task.agent.core.user_ingress import _update_forged_turn_marker
from agents.task.session.hitl_ingress import HITLIngressMixin
from core.exceptions import MessageQueueFullError


class _Logger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass


class _HITL:
    def __init__(self, size=0):
        self._size = size
        self.queued = []

    def get_queue_size(self):
        return self._size

    async def queue_user_message(self, text, kind, metadata):
        self.queued.append((text, kind))


class _Agent:
    def __init__(self, size=0):
        self.agent_id = "a1"
        self.hitl_manager = _HITL(size)


class _Orch(HITLIngressMixin):
    def __init__(self, agents=None):
        self.logger = _Logger()
        self.agents = agents if agents is not None else {}
        self._pending_messages = []
        self._pending_messages_lock = asyncio.Lock()
        self.session_id = "s1"
        self._correspondent_tainted = False
        self._correspondent_taint_sources = set()


def _taint(orch):
    orch._set_correspondent_taint("email", "attacker@evil.test")
    assert orch._correspondent_tainted is True


def test_submit_alone_no_longer_clears_taint():
    """The queue-time clear is gone: submitting does not by itself re-open the gate."""
    orch = _Orch({"a1": _Agent()})
    _taint(orch)
    asyncio.run(orch.submit_user_message("a1", "ok", kind="comment"))
    assert orch._correspondent_tainted is True, (
        "taint cleared at submit time — it must wait until the message is drained"
    )


def test_taint_survives_a_dropped_message_to_a_missing_agent():
    orch = _Orch({})  # no such agent -> submit returns without queueing
    _taint(orch)
    asyncio.run(orch.submit_user_message("nope", "ok", kind="comment"))
    assert orch._correspondent_tainted is True
    assert orch._correspondent_taint_sources


def test_taint_survives_a_rejected_message_when_the_queue_is_full():
    import os

    orch = _Orch({"a1": _Agent(size=int(os.environ.get("MAX_QUEUED_MESSAGES", "10")))})
    _taint(orch)
    with pytest.raises(MessageQueueFullError):
        asyncio.run(orch.submit_user_message("a1", "ok", kind="comment"))
    assert orch._correspondent_tainted is True, (
        "a REJECTED owner message re-opened the gate"
    )


def test_taint_survives_a_full_pending_queue():
    import os

    orch = _Orch({})  # no agents -> pending path
    orch._pending_messages = [("x", "comment", {})] * int(
        os.environ.get("MAX_QUEUED_MESSAGES", "10"))
    _taint(orch)
    with pytest.raises(MessageQueueFullError):
        asyncio.run(orch.submit_user_message(None, "ok", kind="comment"))
    assert orch._correspondent_tainted is True


def test_draining_a_genuine_owner_batch_clears_the_taint():
    """The intended behaviour still works — the owner replying re-opens the gate."""
    orch = _Orch({"a1": _Agent()})
    _taint(orch)
    _update_forged_turn_marker(orch, [{"kind": "comment"}])
    assert orch._correspondent_tainted is False
    assert orch._correspondent_taint_sources == set()


def test_draining_a_forged_batch_does_not_clear_the_taint():
    orch = _Orch({"a1": _Agent()})
    _taint(orch)
    _update_forged_turn_marker(orch, [{"kind": "self_wake"}])
    assert orch._correspondent_tainted is True


def test_draining_a_non_owner_kind_does_not_clear_the_taint():
    """Only the human-intake kinds count; a system/guidance injection is not the
    owner taking the wheel."""
    orch = _Orch({"a1": _Agent()})
    _taint(orch)
    _update_forged_turn_marker(orch, [{"kind": "guidance"}])
    assert orch._correspondent_tainted is True


def test_mixed_batch_with_a_forged_message_still_clears_on_the_genuine_one():
    """A mixed batch marks the TURN forged (fail toward untrusted) but the owner's
    real message did arrive, so the taint clears — matching the pre-existing
    forged-marker semantics rather than inventing a second rule."""
    orch = _Orch({"a1": _Agent()})
    _taint(orch)
    _update_forged_turn_marker(orch, [{"kind": "self_wake"}, {"kind": "comment"}])
    assert orch._forged_turn_kind == "self_wake"
    assert orch._correspondent_tainted is False


def test_end_to_end_submit_then_drain_clears():
    orch = _Orch({"a1": _Agent()})
    _taint(orch)
    asyncio.run(orch.submit_user_message("a1", "go ahead", kind="comment"))
    assert orch._correspondent_tainted is True  # queued, not yet drained
    _update_forged_turn_marker(orch, [{"kind": "comment", "text": "go ahead"}])
    assert orch._correspondent_tainted is False
