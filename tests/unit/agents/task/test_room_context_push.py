"""044 I8: "presented = handled" needs the block to have been PRESENTED, and a
room carries exactly ONE current context.

`push_room_context` is fail-open and returns False when it could not place the
block (no resident agent, no pending buffer). The cold start marked the turn
answered anyway, so a DROPPED block permanently retired ledger lines the model
never saw — they were gone from the next turn and from the service run alike.

Second half: the ephemeral queue caps at 30 and drops the OLDEST, so a busy room
that outran its turns could stack up to 30 context blocks (6000 chars each) in
front of one call — every one a stale view of the same conversation, and all but
the last already answered.
"""
import types

from modules.llm.messages import MessageOrigin
from agents.task.task_agent_delivery import _drop_queued_room_context


class _MM:
    def __init__(self):
        self._ephemeral_messages = []

    def push_ephemeral_message(self, msg):
        self._ephemeral_messages.append(msg)


def _ctx_msg(text):
    from modules.llm.messages import make_control_message
    return make_control_message(text, MessageOrigin.GROUP_CONTEXT)


def _other_msg(text):
    from modules.llm.messages import make_control_message
    return make_control_message(text, MessageOrigin.CORRESPONDENT)


def test_a_new_room_context_collapses_the_queued_ones():
    mm = _MM()
    mm.push_ephemeral_message(_ctx_msg("old one"))
    mm.push_ephemeral_message(_ctx_msg("old two"))
    assert _drop_queued_room_context(mm) == 2
    assert mm._ephemeral_messages == []


def test_the_collapse_never_touches_another_origin():
    """A correspondent reply, a memory note or a deferral notice queued beside the
    room context is NOT a stale view of the room — dropping it would lose real
    one-shot content."""
    mm = _MM()
    keep = _other_msg("a correspondent replied")
    mm.push_ephemeral_message(keep)
    mm.push_ephemeral_message(_ctx_msg("old"))
    assert _drop_queued_room_context(mm) == 1
    assert mm._ephemeral_messages == [keep]


def test_the_collapse_is_fail_open():
    assert _drop_queued_room_context(object()) == 0
    assert _drop_queued_room_context(None) == 0


class _Agent:
    def __init__(self, mm):
        self.message_manager = mm


class _Orch:
    def __init__(self, mm=None, pending=None):
        self.agents = {"executor": _Agent(mm)} if mm is not None else {}
        if pending is not None:
            self._pending_room_context = pending


class _TA:
    """The mixin under test, with the host state it reads."""
    def __init__(self, orch):
        from agents.task.task_agent_delivery import TaskAgentDeliveryMixin
        self.push_room_context = types.MethodType(
            TaskAgentDeliveryMixin.push_room_context, self)
        self._orch = orch

    def get_orchestrator(self, sid):
        return self._orch


def test_push_room_context_keeps_only_the_newest_block():
    mm = _MM()
    ta = _TA(_Orch(mm))
    assert ta.push_room_context("s1", "first block") is True
    assert ta.push_room_context("s1", "second block") is True
    assert len(mm._ephemeral_messages) == 1
    assert "second block" in mm._ephemeral_messages[0].content


def test_a_cold_start_buffer_keeps_only_the_newest_block():
    pending = []
    ta = _TA(_Orch(pending=pending))
    assert ta.push_room_context("s1", "first") is True
    assert ta.push_room_context("s1", "second") is True
    assert len(pending) == 1 and "second" in pending[0]


def test_push_returns_false_when_there_is_nowhere_to_put_the_block():
    """This False is what the cold start must now read before it marks lines
    answered — the caller's half of I8."""
    ta = _TA(_Orch())          # no agent, no pending buffer
    assert ta.push_room_context("s1", "a block") is False


def test_an_empty_block_is_never_pushed():
    mm = _MM()
    ta = _TA(_Orch(mm))
    assert ta.push_room_context("s1", "") is False
    assert mm._ephemeral_messages == []
