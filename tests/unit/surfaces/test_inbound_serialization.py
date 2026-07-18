"""B6 (2026-07-13 correspondent review): per-chat serialization on polling surfaces.

The webhook surface serializes per-chat with a KeyedLock, but the polling surfaces
(telegram/discord/slack/signal/x/email all share telegram's act_on_inbound) had no
lock — two rapid messages to a cold chat could both route TASK_AGENT and race
create_session for the single session_chat binding, orphaning one session.

act_on_inbound now serializes on decision.session_key: same key = strictly
sequential; different keys still run concurrently.
"""
import asyncio

import pytest

import surfaces.telegram.harness as harness
from core.surfaces.dispatcher import RouteDecision, RouteKind
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
from surfaces.telegram.inbound import InboundResult


def _result(session_key: str, text: str = "hello") -> InboundResult:
    src = SessionSource(surface_id="telegram", chat_id="c1", chat_type="dm")
    ident = Identity(user_id="u1", source=src, raw_user_id="42")
    inbound = InboundMessage(text=text, identity=ident)
    return InboundResult(
        inbound=inbound,
        decision=RouteDecision(RouteKind.TASK_AGENT, session_key),
    )


class _Tracker:
    def __init__(self):
        self.active = 0
        self.max_active = 0

    async def run(self, *a, **k):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.02)
        self.active -= 1


@pytest.mark.asyncio
async def test_same_chat_inbounds_are_serialized(monkeypatch):
    tracker = _Tracker()
    monkeypatch.setattr(harness, "_start_task_session", tracker.run)
    await asyncio.gather(
        harness.act_on_inbound(object(), _result("chat-A", "m1")),
        harness.act_on_inbound(object(), _result("chat-A", "m2")),
    )
    assert tracker.max_active == 1, "same-chat inbounds must not interleave"


@pytest.mark.asyncio
async def test_different_chats_still_run_concurrently(monkeypatch):
    tracker = _Tracker()
    monkeypatch.setattr(harness, "_start_task_session", tracker.run)
    await asyncio.gather(
        harness.act_on_inbound(object(), _result("chat-A")),
        harness.act_on_inbound(object(), _result("chat-B")),
    )
    assert tracker.max_active == 2, "distinct chats must not serialize on each other"
