"""098 AG-06: a REPL line is an owner turn — the forged marker never sticks."""
from types import SimpleNamespace

import pytest

from agents.task.agent.conversation import Conversation
from agents.task.agent.core import user_ingress
from agents.task.agent.core.user_ingress import _update_forged_turn_marker, mark_owner_turn


class _Agent:
    def __init__(self, orch):
        self.orchestrator = orch
        self.seen_kind = "unset"

    def set_turn_input(self, text):
        pass

    async def run(self, max_steps=0, _continue_session=False):
        self.seen_kind = self.orchestrator._forged_turn_kind
        return []


def _orch():
    return SimpleNamespace(session_id="s1", _forged_turn_kind=None, _owner_turn_text="")


@pytest.mark.asyncio
async def test_repl_line_after_delegation_result_is_owner():
    orch = _orch()
    _update_forged_turn_marker(orch, [{"kind": "delegation_result", "text": "x"}])
    assert orch._forged_turn_kind == "delegation_result"
    agent = _Agent(orch)
    await Conversation(agent).respond("run ls")
    assert agent.seen_kind is None


def test_repl_line_with_forged_message_in_queue_stays_forged():
    orch = _orch()
    mark_owner_turn(orch)
    # run()'s first drain sees the forged message queued with the line
    _update_forged_turn_marker(orch, [{"kind": "self_wake", "text": "wake"}])
    assert orch._forged_turn_kind == "self_wake"


def test_repl_line_resets_reentry_budget(monkeypatch):
    reset = []
    import agents.task.agent.core.self_wake as sw
    monkeypatch.setattr(sw, "get_reentry_budget",
                        lambda: SimpleNamespace(reset=lambda sid: reset.append(sid)))
    orch = _orch()
    orch._forged_turn_kind = "self_wake"
    mark_owner_turn(orch)
    assert reset == ["s1"] and orch._forged_turn_kind is None


def test_repl_line_clears_correspondent_taint(monkeypatch):
    cleared = []
    import core.security.taints as taints
    monkeypatch.setattr(taints, "clear_on_owner_turn", lambda o: cleared.append(o))
    orch = _orch()
    mark_owner_turn(orch)
    assert cleared == [orch]


def test_mark_owner_turn_tolerates_no_orchestrator():
    mark_owner_turn(None)
