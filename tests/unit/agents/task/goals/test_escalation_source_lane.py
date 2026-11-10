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
