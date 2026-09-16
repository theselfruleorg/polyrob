"""A ROOM turn may commit zero messages — and `done` is never room speech.

The live defect (2026-09-16, public group): a member said "Go boy, do it!", the
agent correctly judged it non-actionable and spoke nothing, and the "never zero"
safety net published `done`'s INTERNAL completion record into the room instead —
a session recap naming the owner's earlier private question, the unconfigured
API keys and the agent's own gating reasoning, addressed to nobody present.

The room prompt already taught `[SILENT]` as a valid outcome. The framework
overrode the prompt it wrote. These pin that it no longer can.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.surfaces import turn_reply

ROOM_KEY = "agent:main:telegram:group:-100123"
ROOM_THREAD_KEY = "agent:main:telegram:supergroup:-100123:thread:7"
DM_KEY = "agent:main:telegram:dm:555:u_abc"

#: The shape that actually went out. Kept verbatim: every future reader of this
#: test should see what "done's summary" looks like when a room receives it.
LEAKED_RECAP = (
    "Session closed. Owner's paid-group capabilities question was answered in "
    "full earlier (no turnkey paid-group module; honest gates: CollabLand/Alchemy "
    "keys unconfigured, x402 money tools owner-grant-only). Two follow-up group "
    "turns were non-actionable spam/member noise and were closed with the exact "
    "[SILENT] sentinel — already on record, so not resent."
)


class _Router:
    def __init__(self):
        self.published = []

    async def publish(self, msg):
        self.published.append(msg)
        return True


def _orch(router, key):
    return SimpleNamespace(_message_router=router, _chat_session_key=key)


@pytest.fixture(autouse=True)
def _bus_on(monkeypatch):
    monkeypatch.setenv("SINGULAR_CHAT_ENABLED", "true")


async def _complete(orch, text):
    from core.surfaces.outbound_mirror import build_completion_publish
    await build_completion_publish(
        orch._message_router, orch._chat_session_key, orch)(text)


async def _speak(orch, text):
    from core.surfaces.outbound_mirror import build_discrete_publish
    await build_discrete_publish(orch._message_router, orch._chat_session_key)(text)
    turn_reply.mark_reply_published(orch, text)


@pytest.mark.asyncio
@pytest.mark.parametrize("key", [ROOM_KEY, ROOM_THREAD_KEY])
async def test_done_never_publishes_into_a_room(key):
    """THE regression. A silent room turn must cost zero messages."""
    router = _Router()
    await _complete(_orch(router, key), LEAKED_RECAP)
    assert router.published == [], (
        "done published into a room — this is the exact leak: an internal "
        "completion record delivered to a group of people as if it were speech")


@pytest.mark.asyncio
async def test_done_still_speaks_in_a_dm():
    """The safety net is unchanged where it was right: a done()-only DM turn is
    the majority shape, and silencing it would be strictly worse."""
    router = _Router()
    await _complete(_orch(router, DM_KEY), "Here is your answer.")
    assert [m.text for m in router.published] == ["Here is your answer."]


@pytest.mark.asyncio
async def test_send_message_is_the_room_voice():
    """Suppressing done only works because send_message still carries the room's
    real answers. If this breaks, rooms go mute."""
    router = _Router()
    orch = _orch(router, ROOM_KEY)
    await _speak(orch, "Alexey — yes, claiming the fees first.")
    await _complete(orch, LEAKED_RECAP)
    assert [m.text for m in router.published] == ["Alexey — yes, claiming the fees first."]


@pytest.mark.asyncio
async def test_the_legacy_flag_cannot_reopen_the_room_leak(monkeypatch):
    """`CHAT_SINGLE_FINAL=off` restores the legacy DOUBLE publish — it must not
    also restore publishing an internal recap into a public room. The room rule
    is a disclosure boundary, not a chat-shape preference, so it is checked
    BEFORE the flag."""
    monkeypatch.setenv("CHAT_SINGLE_FINAL", "off")
    router = _Router()
    orch = _orch(router, ROOM_KEY)
    await _speak(orch, "the answer")
    await _complete(orch, LEAKED_RECAP)
    assert [m.text for m in router.published] == ["the answer"]


def test_the_room_check_is_not_swallowed():
    """Ratchet: the predicate is a pure string split and must not sit inside a
    try/except. A fail-open swallow here silently restores the leak, which is
    exactly how a rule stops being a rule."""
    src = (Path(__file__).resolve().parents[4] / "core" / "surfaces"
           / "outbound_mirror.py").read_text()
    body = src[src.index("def build_completion_publish"):]
    guard = body[:body.index("return _complete")]
    assert "is_group_session_key(session_key)" in guard, (
        "build_completion_publish no longer checks for a room session key")
    # The check must precede the latch's try/except, not live inside it.
    assert guard.index("is_group_session_key(session_key)") < guard.index("try:"), (
        "the room check moved inside the fail-open latch block — a swallowed "
        "exception there publishes done's text into the room again")
