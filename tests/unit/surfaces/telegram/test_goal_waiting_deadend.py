"""`/goal <verb> <id>` on a WAITING goal must not be a dead end.

2026-09-08: goal 2dee2891 sat in `waiting` (an unmet dependency -- the ordinary
state for a seeded chain). `_GOAL_TRANSITIONS` only accepts triage/blocked as
sources, so `/goal ready 2dee2891` answered "only triage/blocked goals can be
marked ready" and stopped. The owner was told no and given nothing: no reason,
no prerequisite, no next step. Forcing it would be wrong -- the dependency is
doing its job -- so the fix is to explain, not to permit.
"""
import types

import pytest


def _goal(gid="2dee2891a085", status="waiting"):
    return types.SimpleNamespace(
        id=gid, kind="goal", status=status, priority=1,
        title="Treasury: manage open positions and take a screened entry",
        body="", consecutive_failures=0, last_failure_error=None)


class _Board:
    def __init__(self, goals, edges=None):
        self._goals = {g.id: g for g in goals}
        self._edges = edges or {}

    def list(self, user_id=None, limit=1000):
        return list(self._goals.values())

    def get(self, gid, user_id=None):
        return self._goals.get(gid)

    def dependencies(self, gid):
        return self._edges.get(gid, [])


def test_a_waiting_goal_is_refused_with_its_blocking_edge():
    from surfaces.telegram import owner_ops

    prereq = _goal("a66f5f92a7e2", "ready")
    prereq.title = "Treasury: refresh the watchlist"
    board = _Board([_goal(), prereq], {"2dee2891a085": ["a66f5f92a7e2"]})

    out = owner_ops.goal_reply("u", "/tmp", ["ready", "2dee2891"], board=board)
    assert "waiting" in out.lower()
    assert "a66f5f92" in out, "the owner must be told WHAT it waits on"
    assert "refresh the watchlist" in out, "an id alone is not an explanation"


def test_the_refusal_offers_a_real_next_step():
    from core.owner_remedy import unknown_owner_actions
    from surfaces.telegram import owner_ops

    prereq = _goal("a66f5f92a7e2", "ready")
    board = _Board([_goal(), prereq], {"2dee2891a085": ["a66f5f92a7e2"]})
    out = owner_ops.goal_reply("u", "/tmp", ["ready", "2dee2891"], board=board)
    assert "/goal show" in out
    assert unknown_owner_actions(out) == []


def test_a_blocked_goal_still_transitions_normally():
    """The carve-out must not soften the verbs that already worked."""
    from surfaces.telegram import owner_ops

    g = _goal(status="blocked")
    board = _Board([g])
    board.update_status = lambda *a, **k: True
    out = owner_ops.goal_reply("u", "/tmp", ["retry", "2dee2891"], board=board)
    assert "waiting on" not in out.lower()
