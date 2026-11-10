"""113 W0 (M-1): a DM chat that starts fresh after an idle gap says so, once.

Before this the dispatcher returned TASK_AGENT with no reason and the person's chat
lost its history with no word."""
import sqlite3
import time
from types import SimpleNamespace

import pytest

from core.surfaces.dispatcher import RouteKind, route_inbound
from core.surfaces.session_chat_registry import build_session_key
from core.surfaces.session_policy import idle_gap_seconds
from surfaces.telegram.chat_ops import refresh_notice, tell_refresh
from tests.unit.core.surfaces.test_dispatcher import _FakeContainer, _inbound, _registry_with

H = 3600.0


def test_the_gap_is_measured_from_the_newest_activity():
    assert idle_gap_seconds({"updated_at": 100.0, "last_outbound_at": 400.0}, now=1000.0) == 600.0
    assert idle_gap_seconds({"updated_at": 100.0}, now=1000.0) == 900.0
    assert idle_gap_seconds({}, now=1000.0) is None
    assert idle_gap_seconds({"updated_at": "x"}, now=1000.0) is None


def test_the_line_names_the_gap_and_is_empty_without_one():
    assert refresh_notice(None) == ""
    assert "26 hours ago" in refresh_notice(26 * H)
    assert "3 days ago" in refresh_notice(72 * H)
    assert "/thread" in refresh_notice(26 * H)


@pytest.mark.asyncio
async def test_an_idle_dm_routes_fresh_with_its_gap_and_a_live_one_does_not(tmp_path, monkeypatch):
    monkeypatch.setenv("SESSION_RESET_MODE", "idle")
    monkeypatch.setenv("SESSION_IDLE_MINUTES", "60")
    msg = _inbound("hello again")
    key = build_session_key(msg.identity.source, msg.identity.user_id)
    registry = _registry_with(tmp_path, warm_key=key)
    c = _FakeContainer(registry)
    live = await route_inbound(c, msg)
    assert live.kind == RouteKind.STEER and live.fresh_gap_s is None

    with sqlite3.connect(registry.db_path) as conn:
        conn.execute("UPDATE session_chat_map SET updated_at=?", (time.time() - 5 * H,))
    idle = await route_inbound(c, msg)
    assert idle.kind == RouteKind.TASK_AGENT
    assert 4.9 * H < idle.fresh_gap_s < 5.1 * H


def _result(gap, chat_type="dm"):
    src = SimpleNamespace(chat_type=chat_type)
    return SimpleNamespace(
        decision=SimpleNamespace(fresh_gap_s=gap, session_key="agent:main:telegram:dm:1:u_x"),
        inbound=SimpleNamespace(identity=SimpleNamespace(source=src, user_id="u_x")))


@pytest.mark.asyncio
async def test_tell_refresh_sends_once_for_a_dm_and_never_in_a_room():
    sent = []

    async def deliver(text):
        sent.append(text)

    agent = SimpleNamespace(container=None)
    assert await tell_refresh(agent, _result(26 * H), deliver) is True
    assert len(sent) == 1 and "26 hours ago" in sent[0]
    assert await tell_refresh(agent, _result(None), deliver) is False
    assert await tell_refresh(agent, _result(26 * H, chat_type="group"), deliver) is False
    assert await tell_refresh(agent, _result(26 * H), None) is False
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_a_failed_send_never_stops_the_turn():
    async def boom(text):
        raise RuntimeError("network")
    assert await tell_refresh(SimpleNamespace(container=None), _result(26 * H), boom) is False
