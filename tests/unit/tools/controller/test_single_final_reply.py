"""One committed reply per turn — and never zero (communication contract C1/T2).

Nothing in the suite asserted this before (finding F13): the existing
outbound-collapse test asserts a single publish per CALL, never per TURN.
"""
from types import SimpleNamespace

import pytest

from core.surfaces import turn_reply


class _Router:
    def __init__(self):
        self.published = []

    async def publish(self, msg):
        self.published.append(msg)


def _orch(router):
    return SimpleNamespace(_message_router=router, _chat_session_key="telegram:1")


@pytest.fixture(autouse=True)
def _bus_on(monkeypatch):
    # build_discrete_publish is a no-op unless the singular-chat bus is on.
    monkeypatch.setenv("SINGULAR_CHAT_ENABLED", "true")


async def _reply(orch, text):
    """What send_message's mirror does: publish, then claim the turn."""
    from core.surfaces.outbound_mirror import build_discrete_publish
    await build_discrete_publish(orch._message_router, orch._chat_session_key)(text)
    turn_reply.mark_reply_published(orch)


async def _complete(orch, text):
    """What done's mirror does: publish ONLY if the turn spoke nothing."""
    from core.surfaces.outbound_mirror import build_completion_publish
    await build_completion_publish(
        orch._message_router, orch._chat_session_key, orch)(text)


@pytest.mark.asyncio
async def test_reply_then_done_delivers_exactly_one_message():
    router = _Router()
    orch = _orch(router)
    await _reply(orch, "The capital of France is Paris.")
    await _complete(orch, "Answered the owner's question via Telegram. No further action.")
    assert [m.text for m in router.published] == ["The capital of France is Paris."]


@pytest.mark.asyncio
async def test_done_alone_still_speaks():
    """The safety net. A done()-only turn is today's majority shape; if done
    stopped publishing outright, those turns would go silent."""
    router = _Router()
    orch = _orch(router)
    await _complete(orch, "Here is your answer.")
    assert [m.text for m in router.published] == ["Here is your answer."]


@pytest.mark.asyncio
async def test_the_latch_resets_between_turns():
    router = _Router()
    orch = _orch(router)
    await _reply(orch, "turn one reply")
    await _complete(orch, "turn one recap")
    turn_reply.reset_turn(orch)          # what _drain_user_messages does
    await _complete(orch, "turn two answer")
    assert [m.text for m in router.published] == ["turn one reply", "turn two answer"]


@pytest.mark.asyncio
async def test_flag_off_restores_the_legacy_double_publish(monkeypatch):
    monkeypatch.setenv("CHAT_SINGLE_FINAL", "off")
    router = _Router()
    orch = _orch(router)
    await _reply(orch, "the answer")
    await _complete(orch, "the recap")
    assert [m.text for m in router.published] == ["the answer", "the recap"]
