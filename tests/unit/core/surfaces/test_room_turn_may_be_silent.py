"""A ROOM turn may commit zero messages — and `done` is never speech.

The live defect (2026-09-16, public group): a member said "Go boy, do it!", the
agent correctly judged it non-actionable and spoke nothing, and a "never zero"
safety net published `done`'s INTERNAL completion record into the room instead —
a session recap naming the owner's earlier private question, the unconfigured
API keys and the agent's own gating reasoning, addressed to nobody present.

Since 2026-09-17 `done` has no delivery path at all (see
tests/unit/tools/controller/test_done_is_not_speech.py). What this file pins is
the other half: the room is not mute, because `send_message` still carries it.
"""
import pytest

from core.surfaces import turn_reply

ROOM_KEY = "agent:main:telegram:group:-100123"


class _Router:
    def __init__(self):
        self.published = []

    async def publish(self, msg):
        self.published.append(msg)
        return True


@pytest.mark.asyncio
async def test_send_message_is_the_room_voice(monkeypatch):
    """If this breaks, rooms go mute."""
    monkeypatch.setenv("SINGULAR_CHAT_ENABLED", "true")
    from types import SimpleNamespace
    from core.surfaces.outbound_mirror import build_discrete_publish
    router = _Router()
    orch = SimpleNamespace(_message_router=router, _chat_session_key=ROOM_KEY)
    text = "Alexey — yes, claiming the fees first."
    await build_discrete_publish(router, ROOM_KEY)(text)
    turn_reply.mark_reply_published(orch, text)
    assert [m.text for m in router.published] == [text]


def test_the_room_prompt_says_done_is_not_delivered():
    """Suppressing `done` is only safe because the prompt teaches the verb: a
    `done()`-only turn is the majority shape, and an untaught model would
    leave the room with nothing."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[4] / "agents" / "task" / "agent"
           / "prompts.py").read_text()
    assert "`send_message` is the ONLY way to speak" in src
    assert "The user NEVER sees it" in src
