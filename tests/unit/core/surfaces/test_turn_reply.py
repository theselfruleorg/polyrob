"""Per-turn reply record (agent communication contract, C1).

`send_message` records the text it published so the UNBOUND delivery paths
return the reply the user actually got. `done` is not speech and records
nothing here (see tests/unit/tools/controller/test_done_is_not_speech.py).
"""
from types import SimpleNamespace

from core.surfaces import turn_reply


def _orch():
    return SimpleNamespace()


def test_a_fresh_turn_has_no_reply():
    assert turn_reply.last_reply_text(_orch()) is None


def test_mark_then_read():
    orch = _orch()
    turn_reply.mark_reply_published(orch, "the answer")
    assert turn_reply.last_reply_text(orch) == "the answer"


def test_reset_clears_the_recorded_reply():
    orch = _orch()
    turn_reply.mark_reply_published(orch, "turn one")
    turn_reply.reset_turn(orch)
    assert turn_reply.last_reply_text(orch) is None


def test_a_none_orchestrator_never_raises():
    # Sub-agents and unbound sessions pass None through this seam.
    assert turn_reply.last_reply_text(None) is None
    turn_reply.mark_reply_published(None, "x")   # must not raise
    turn_reply.reset_turn(None)                  # must not raise


def test_a_hostile_orchestrator_reads_none_rather_than_raising():
    class Hostile:
        def __getattr__(self, name):  # every attribute read explodes
            raise RuntimeError("boom")

    assert turn_reply.last_reply_text(Hostile()) is None
    turn_reply.mark_reply_published(Hostile(), "x")   # must not raise


def test_only_a_real_string_is_recorded_as_the_reply():
    """A proxied/mock orchestrator auto-creates attributes; a truthy non-string
    coerced with str() would be DELIVERED to the user as the agent's answer."""
    from unittest.mock import MagicMock
    assert turn_reply.last_reply_text(MagicMock()) is None

    orch = _orch()
    turn_reply.mark_reply_published(orch, object())      # not a str
    assert turn_reply.last_reply_text(orch) is None
    turn_reply.mark_reply_published(orch, "   ")         # blank
    assert turn_reply.last_reply_text(orch) is None
    turn_reply.mark_reply_published(orch, "the answer")
    assert turn_reply.last_reply_text(orch) == "the answer"
