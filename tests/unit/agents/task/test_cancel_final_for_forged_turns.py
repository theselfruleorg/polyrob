"""098 review O-01: an owner cancel stays until a PERSON writes again.

Q1 made every session state resumable (``cancelled -> running``). A forged turn
— a self-wake, a background delegation result — or a correspondent's data must
not undo the owner's stop: it stays queued and is read by the run that the
owner's next message starts. The cancel also stops the session's background
delegations.
"""
import asyncio
from types import SimpleNamespace

from agents.task.task_agent_control import TaskAgentControlMixin
from agents.task.task_agent_delivery import TaskAgentDeliveryMixin


def _agent_with(queue, pending=()):
    hitl = SimpleNamespace(_user_messages=list(queue), get_queue_size=lambda: len(queue))
    orch = SimpleNamespace(agents={"a": SimpleNamespace(hitl_manager=hitl, message_manager=None)},
                           _pending_messages=list(pending))

    class _TA(TaskAgentDeliveryMixin):
        _registry = SimpleNamespace(get=lambda sid: orch)

        def take_pending_room_turn(self, sid):
            return None
    return _TA()


def test_a_forged_turn_never_resumes_a_cancelled_session():
    ta = _agent_with([{"kind": "self_wake"}, {"kind": "delegation_result"}])
    assert ta._resume_has_input("s1", "cancelled") is False
    assert ta._resume_has_input("s1", "completed") is True   # unchanged elsewhere


def test_a_person_message_resumes_a_cancelled_session():
    assert _agent_with([{"kind": "self_wake"}, {"kind": "comment"}])._resume_has_input(
        "s1", "cancelled") is True
    assert _agent_with([], pending=[("hi", "comment", {})])._resume_has_input(
        "s1", "cancelled") is True
    assert _agent_with([])._resume_has_input("s1", "cancelled") is False   # a stale kick


def _orch_with_delegations(cancelled):
    import logging
    import types as _types
    from agents.task.agent.orchestrator import SessionOrchestrator
    orch = SimpleNamespace(cancel=lambda: None, logger=logging.getLogger("t"),
                           async_delegation=SimpleNamespace(
                               cancel_all=lambda reason: cancelled.append(reason) or 1))
    orch.cancel_background_delegations = _types.MethodType(
        SessionOrchestrator.cancel_background_delegations, orch)
    return orch


def test_durable_cancel_also_stops_background_delegations():
    """/run stop and `polyrob session cancel` go through the control mailbox,
    not TaskAgent.cancel_session — they stop background children too."""
    from agents.task.agent.core.session_control import control_checkpoint
    cancelled, acks = [], []
    orch = _orch_with_delegations(cancelled)
    store = SimpleNamespace(
        read=lambda: {"generation": 1, "request": "cancel", "acknowledged": "running"},
        acknowledge=lambda gen, cmd, state: acks.append(state))
    agent = SimpleNamespace(_session_control=(store, 1), cancel=lambda: None,
                            orchestrator=orch, state=SimpleNamespace(run_control=None))
    assert asyncio.run(control_checkpoint(agent)) is False
    assert cancelled and acks == ["cancelled"]


def test_cancel_stops_background_delegations():
    cancelled = []
    orch = _orch_with_delegations(cancelled)
    statuses = []

    class _TA(TaskAgentControlMixin):
        task_available = True
        _registry = SimpleNamespace(get=lambda sid: orch)
        session_manager = SimpleNamespace(
            get_session_info=lambda sid: {"user_id": "u"},
            update_session_status=lambda sid, st: statuses.append(st))

    assert asyncio.run(_TA().cancel_session("u", session_id="s1")) is True
    assert cancelled and statuses == ["cancelled"]
