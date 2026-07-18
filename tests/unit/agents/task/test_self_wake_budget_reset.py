"""Regression (P1 finalization): a genuine (non-forged) user turn must reset the
self-wake ReentryBudget. Previously the only reset() caller was dead code, so after
SELF_WAKE_MAX_REENTRIES forged wakes self-wake died permanently for a session and
never recovered from real conversation.
"""
import types

from agents.task.agent.core.user_ingress import _update_forged_turn_marker
from agents.task.agent.core.self_wake import get_reentry_budget, reset_reentry_budget


def _orch(session_id):
    return types.SimpleNamespace(session_id=session_id, _forged_turn_kind=None)


def test_genuine_turn_resets_reentry_budget(monkeypatch):
    monkeypatch.setenv("SELF_WAKE_MAX_REENTRIES", "3")
    monkeypatch.setenv("SELF_WAKE_IDLE_BACKOFF_SEC", "0")  # exhaust without clock gaps
    reset_reentry_budget()  # rebuild singleton from env
    budget = get_reentry_budget()
    sid = "sess-reset-1"

    # Exhaust the budget via forged wakes (try_consume increments; allow is read-only).
    for _ in range(3):
        budget.try_consume(sid)
    assert budget.remaining(sid) == 0

    # A genuine (non-forged) batch drains → marker cleared → budget reset.
    _update_forged_turn_marker(_orch(sid), [{"kind": "comment", "text": "hi"}])
    assert budget.remaining(sid) == 3, "genuine user turn must restore the self-wake budget"


def test_forged_batch_does_not_reset(monkeypatch):
    monkeypatch.setenv("SELF_WAKE_MAX_REENTRIES", "3")
    reset_reentry_budget()
    budget = get_reentry_budget()
    sid = "sess-reset-2"
    budget.try_consume(sid)
    assert budget.remaining(sid) == 2
    # A forged (self_wake) batch must NOT reset the budget.
    _update_forged_turn_marker(_orch(sid), [{"kind": "self_wake", "text": "wake"}])
    assert budget.remaining(sid) == 2
