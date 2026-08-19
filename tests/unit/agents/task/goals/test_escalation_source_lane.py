"""A blocked-goal escalation must ride its OWN delivery source, not the chatter lane.

`push_owner_message` hardcoded `source="self_evolution"`, so a blocker escalation
("I stopped, I need you") was indistinguishable from a "goal started" ping at the
delivery gate. Prod suppressed 156 self_evolution messages in 8 days; the
escalations went down with them, and 44 asks were still unanswered a month later.
"""
import asyncio
from types import SimpleNamespace

import pytest

from agents.task.goals import escalation as _escalation
from agents.task.goals.board import Goal


def test_blocked_escalation_sends_on_the_goal_blocked_source(monkeypatch):
    # GOAL_BLOCKER_ESCALATION is posture-defaulted (ON in prod, off in a bare
    # test env) — the lane, not the gate, is what this test is about.
    monkeypatch.setenv("GOAL_BLOCKER_ESCALATION", "true")
    seen = {}

    async def _fake_push(container, text, attachments=None, priority=None, source=None):
        seen["source"] = source
        seen["priority"] = priority
        return True

    import core.self_evolution as se
    monkeypatch.setattr(se, "push_owner_message", _fake_push)

    goal = Goal(id="g1", user_id="rob", title="ship the thing",
                status="blocked", last_failure_error="need an owner decision")
    agent = SimpleNamespace(container=SimpleNamespace())

    asyncio.run(_escalation.maybe_escalate_blocked(agent, goal))

    assert seen.get("source") == "goal_blocked", (
        "a blocked-goal escalation must not share the chatter source")


def test_push_owner_message_accepts_a_source(monkeypatch):
    """The seam itself: a caller can name its lane."""
    captured = {}

    async def _fake_deliver(container, owner, text, source=None, attachments=None,
                            priority=None):
        captured["source"] = source
        return "sent"

    import core.surfaces.user_delivery as ud
    monkeypatch.setattr(ud, "deliver_user_message", _fake_deliver)

    from core.self_evolution import push_owner_message
    ok = asyncio.run(push_owner_message(SimpleNamespace(), "hello", source="goal_blocked"))

    assert ok is True
    assert captured["source"] == "goal_blocked"


def test_push_owner_message_defaults_to_self_evolution(monkeypatch):
    captured = {}

    async def _fake_deliver(container, owner, text, source=None, attachments=None,
                            priority=None):
        captured["source"] = source
        return "sent"

    import core.surfaces.user_delivery as ud
    monkeypatch.setattr(ud, "deliver_user_message", _fake_deliver)

    from core.self_evolution import push_owner_message
    asyncio.run(push_owner_message(SimpleNamespace(), "hello"))

    assert captured["source"] == "self_evolution"
