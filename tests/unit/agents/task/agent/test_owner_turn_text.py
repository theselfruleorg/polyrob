"""AGT-4 / AGT-17: the owner's own words for a turn, recomputed at the drain.

``owner_ask(answer=)`` may only take a decision that stands in what the owner
typed. The drain records that text; a forged batch, a forwarded body and a
room admin's line are not the owner's words, and an admin line never clears
the correspondent taint.
"""
from agents.task.agent.core.step_execution import _owner_turn_text
from agents.task.agent.core.user_ingress import ROOM_ROLE_KEY, _update_forged_turn_marker


class _Orch:
    session_id = "s1"

    def __init__(self):
        self._correspondent_tainted = True
        self.cleared = 0

    def _clear_correspondent_taint(self):
        self.cleared += 1
        self._correspondent_tainted = False

    def _set_correspondent_taint(self, *a):
        self._correspondent_tainted = True


def test_genuine_batch_records_owner_text():
    o = _Orch()
    _update_forged_turn_marker(o, [{"text": "A", "kind": "comment", "metadata": {}}])
    assert o._owner_turn_text == "A"
    assert o.cleared == 1


def test_forged_batch_has_no_owner_text():
    o = _Orch()
    _update_forged_turn_marker(o, [{"text": "decide A", "kind": "self_wake", "metadata": {}}])
    assert not o._owner_turn_text


def test_forwarded_body_is_not_owner_text():
    o = _Orch()
    _update_forged_turn_marker(o, [{"text": "approve", "kind": "comment",
                                    "metadata": {"forwarded": True}}])
    assert o._owner_turn_text == ""


def test_room_admin_line_neither_clears_taint_nor_counts_as_owner_text():
    o = _Orch()
    _update_forged_turn_marker(o, [{"text": "A", "kind": "comment",
                                    "metadata": {ROOM_ROLE_KEY: "admin"}}])
    assert o.cleared == 0 and o._correspondent_tainted is True
    assert o._owner_turn_text == ""


def test_step_context_reads_drained_text_else_opening_task():
    class _A:
        task = "opening line B"
        orchestrator = object()
    assert _owner_turn_text(_A()) == "opening line B"
    o = _Orch()
    _update_forged_turn_marker(o, [{"text": "hold", "kind": "continuation", "metadata": {}}])
    _A.orchestrator = o
    assert _owner_turn_text(_A()) == "hold"
    _update_forged_turn_marker(o, [{"text": "x", "kind": "delegation_result", "metadata": {}}])
    assert _owner_turn_text(_A()) is None
