"""Telegram `/run` (070 decision 1): one background run from the phone.

The rows and the control are core.run_control (tested there); this pins the
seat wiring — routable, owner-gated, refused in a room, and the asking chat's
own session is never offered as a run to stop.
"""
import pytest

from core.surfaces.dispatcher import RouteDecision, RouteKind, _COMMANDS
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
from surfaces.telegram.harness import (_OWNER_ADMIN_COMMANDS, _ROOM_REFUSED_COMMANDS,
                                       act_on_inbound)
from surfaces.telegram.inbound import InboundResult


class _Cfg:
    def __init__(self, data_dir):
        self.data_dir = data_dir


class _Container:
    def __init__(self, data_dir):
        self.config = _Cfg(data_dir)

    def get_service(self, name):
        return None


class _Agent:
    def __init__(self, data_dir):
        self.container = _Container(data_dir)


def _cmd(text, user="alice", session_id="s-chat"):
    src = SessionSource("telegram", "555", "dm")
    inbound = InboundMessage(text=text,
                             identity=Identity(user_id=user, source=src, raw_user_id="555"))
    return InboundResult(inbound=inbound, decision=RouteDecision(
        RouteKind.COMMAND, "agent:main:telegram:dm:555:" + user, command="/run",
        session_id=session_id))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "alice")
    monkeypatch.setenv("POLYROB_INSTANCE_ID", "rob")
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    return tmp_path


def test_run_is_a_routed_owner_verb_refused_in_rooms():
    assert "/run" in _COMMANDS
    assert "/run" in _OWNER_ADMIN_COMMANDS
    assert "/run" in _ROOM_REFUSED_COMMANDS


@pytest.mark.asyncio
async def test_owner_run_calls_the_core_reply_and_excludes_this_chat(env, monkeypatch):
    seen = {}

    async def fake(user_id, data_dir, sessions_root, args, *, exclude=()):
        seen.update(user_id=user_id, args=args, exclude=tuple(exclude))
        return "Runs in the background now:"

    monkeypatch.setattr("core.run_control.run_reply", fake)
    out = await act_on_inbound(_Agent(str(env)), _cmd("/run stop 2"))
    assert out == "Runs in the background now:"
    assert seen == {"user_id": "alice", "args": ["stop", "2"], "exclude": ("s-chat",)}


@pytest.mark.asyncio
async def test_non_owner_is_refused(env, monkeypatch):
    called = []

    async def fake(*a, **k):
        called.append(1)
        return "x"

    monkeypatch.setattr("core.run_control.run_reply", fake)
    out = await act_on_inbound(_Agent(str(env)), _cmd("/run", user="mallory"))
    assert "Owner only" in out
    assert not called
