"""An owner APPROVAL request must not be droppable by goal-started chatter.

Over 2026-08-10..17 prod recorded 196 owner notices and delivered exactly ONE.
195 were "[suppressed by daily proactive-message cap]", and among them were SIX
`source=approval` messages — the owner-queue lane that money verbs block on. A
chatter flood starving the approval lane is a correctness bug, not a tuning
question: the agent then waits on a decision the owner was never asked for.
"""
from core.surfaces.user_delivery import (
    PRIORITY_CRITICAL, PRIORITY_LOW, PRIORITY_NORMAL,
    effective_cap_for_priority, resolve_priority,
)


def test_approval_is_critical_so_the_daily_cap_cannot_drop_it():
    assert resolve_priority("approval", None) == PRIORITY_CRITICAL


def test_payment_approval_is_critical_too():
    assert resolve_priority("payment_approval", None) == PRIORITY_CRITICAL


def test_a_blocked_goal_ask_is_critical():
    """A goal that stopped and needs the owner is the whole point of the rail."""
    assert resolve_priority("goal_blocked", None) == PRIORITY_CRITICAL


def test_goal_started_chatter_is_low():
    assert resolve_priority("self_evolution", "low") == PRIORITY_LOW


def test_an_explicit_priority_still_wins_over_the_source():
    assert resolve_priority("approval", PRIORITY_LOW) == PRIORITY_LOW


def test_ordinary_traffic_stays_normal():
    assert resolve_priority("agent_send", None) == PRIORITY_NORMAL


def test_critical_keeps_the_whole_cap():
    assert effective_cap_for_priority(30, PRIORITY_CRITICAL) == 30
