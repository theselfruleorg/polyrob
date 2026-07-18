"""C7: the self_context_manage and skill_manage promote gates only treated
sub-agent/leaf turns as "forged" (never-auto-promote). An autonomous goal/cron/
planner-spawned run is a TOP-LEVEL orchestrator (role='orchestrator',
is_sub_agent=False) and is_owner_local_safe is True under POLYROB_LOCAL — so it could
promote its own unreviewed self-context / skill draft, bypassing owner review. An
autonomous session must count as forged.
"""
from types import SimpleNamespace

from tools.controller.action_registration import _is_forged_or_autonomous_turn
from agents.task.goals.autonomy_marker import mark_autonomous


def _ctx(role="orchestrator", is_sub=False, session_id="s"):
    return SimpleNamespace(role=role, is_sub_agent=is_sub, session_id=session_id)


def test_leaf_role_is_forged():
    assert _is_forged_or_autonomous_turn(_ctx(role="leaf"), None) is True


def test_sub_agent_is_forged():
    assert _is_forged_or_autonomous_turn(_ctx(is_sub=True), None) is True


def test_plain_orchestrator_is_not_forged():
    assert _is_forged_or_autonomous_turn(_ctx(session_id="plain-session"), None) is False


def test_autonomous_orchestrator_is_forged():
    mark_autonomous("auto-session-c7")
    assert _is_forged_or_autonomous_turn(
        _ctx(role="orchestrator", is_sub=False, session_id="auto-session-c7"), None
    ) is True


def test_autonomy_marker_probe_exception_is_forged(monkeypatch):
    """MH1: if the autonomy-marker probe RAISES, the turn must be treated as forged
    (fail-closed) — returning False (pre-fix) would let a forged/autonomous turn
    slip past every gate keyed on this predicate (owner_queue approval,
    writable-skills, message/self_context promotion)."""
    def _boom(_sid):
        raise RuntimeError("autonomy-marker store exploded")

    monkeypatch.setattr("agents.task.goals.autonomy_marker.is_autonomous", _boom)
    # A plain orchestrator turn (not leaf/sub/forged-kind) reaches the marker leg.
    assert _is_forged_or_autonomous_turn(_ctx(session_id="plain-session"), None) is True
