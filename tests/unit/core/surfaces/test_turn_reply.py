"""Per-turn reply latch (agent communication contract, C1).

The framework had no concept of "the reply": both `send_message` and `done`
published, unconditionally. These tests pin the one rule that replaces that —
a turn commits exactly one reply, and never zero.
"""
from types import SimpleNamespace

import pytest

from core.surfaces import turn_reply


def _orch():
    return SimpleNamespace()


def test_a_fresh_turn_has_published_nothing():
    assert turn_reply.reply_published(_orch()) is False


def test_mark_then_read():
    orch = _orch()
    turn_reply.mark_reply_published(orch)
    assert turn_reply.reply_published(orch) is True


def test_reset_clears_the_latch_for_the_next_turn():
    orch = _orch()
    turn_reply.mark_reply_published(orch)
    turn_reply.reset_turn(orch)
    assert turn_reply.reply_published(orch) is False


def test_a_none_orchestrator_never_raises():
    # Sub-agents and unbound sessions pass None through this seam.
    assert turn_reply.reply_published(None) is False
    turn_reply.mark_reply_published(None)   # must not raise
    turn_reply.reset_turn(None)             # must not raise


def test_a_hostile_orchestrator_reads_false_rather_than_raising():
    class Hostile:
        def __getattr__(self, name):  # every attribute read explodes
            raise RuntimeError("boom")

    assert turn_reply.reply_published(Hostile()) is False


@pytest.mark.parametrize("value,expected", [("off", False), ("false", False),
                                            ("0", False), (None, True)])
def test_single_final_flag(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("CHAT_SINGLE_FINAL", raising=False)
    else:
        monkeypatch.setenv("CHAT_SINGLE_FINAL", value)
    assert turn_reply.single_final_enabled() is expected


def test_only_a_real_string_is_recorded_as_the_reply():
    """A proxied/mock orchestrator auto-creates attributes; a truthy non-string
    coerced with str() would be DELIVERED to the user as the agent's answer."""
    from unittest.mock import MagicMock
    assert turn_reply.last_reply_text(MagicMock()) is None

    orch = _orch()
    turn_reply.mark_reply_published(orch, object())      # not a str
    assert turn_reply.last_reply_text(orch) is None
    turn_reply.mark_reply_published(orch, "the answer")
    assert turn_reply.last_reply_text(orch) == "the answer"


def test_reset_clears_the_recorded_reply_too():
    orch = _orch()
    turn_reply.mark_reply_published(orch, "turn one")
    turn_reply.reset_turn(orch)
    assert turn_reply.last_reply_text(orch) is None
