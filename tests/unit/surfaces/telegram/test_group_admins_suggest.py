"""044 T19: `/groups admins here` — Telegram's own admin list as SUGGESTIONS
the owner confirms one by one. Never written; a bot admin is never suggested."""
import asyncio
import types

import pytest

from core.surfaces.dispatcher import RouteDecision, RouteKind
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
from surfaces.telegram.group_ops import groups_reply
from surfaces.telegram.harness import TelegramHarness
from surfaces.telegram.inbound import InboundResult


def test_suggest_admins_renders_without_writing(tmp_path):
    class _Bot:
        async def get_chat_administrators(self, chat_id):
            return [types.SimpleNamespace(user=types.SimpleNamespace(id=9911, username="bob", is_bot=False), status="administrator"),
                    types.SimpleNamespace(user=types.SimpleNamespace(id=1, username="robbot", is_bot=True), status="administrator")]
    h = TelegramHarness.__new__(TelegramHarness)
    h.bot = _Bot()
    out = asyncio.run(h.suggest_admins("-1"))
    assert out == [{"id": "9911", "name": "@bob"}]   # bots excluded


def test_suggest_admins_no_username_falls_back_to_first_name():
    class _Bot:
        async def get_chat_administrators(self, chat_id):
            return [types.SimpleNamespace(
                user=types.SimpleNamespace(id=42, username=None, first_name="Bob", is_bot=False),
                status="creator")]
    h = TelegramHarness.__new__(TelegramHarness)
    h.bot = _Bot()
    out = asyncio.run(h.suggest_admins("-1"))
    assert out == [{"id": "42", "name": "Bob"}]


def test_suggest_admins_fails_open_to_an_empty_list():
    class _Bot:
        async def get_chat_administrators(self, chat_id):
            raise RuntimeError("boom")
    h = TelegramHarness.__new__(TelegramHarness)
    h.bot = _Bot()
    assert asyncio.run(h.suggest_admins("-1")) == []


# ---------------------------------------------------------------------------
# /groups admins here — the group_ops seat over suggest_admins
# ---------------------------------------------------------------------------

class _Cfg:
    def __init__(self, data_dir):
        self.data_dir = data_dir


class _FakeHarness:
    def __init__(self, suggestions):
        self._suggestions = suggestions

    async def suggest_admins(self, chat_id):
        return self._suggestions


class _Container:
    def __init__(self, data_dir, harness=None):
        self.config = _Cfg(data_dir)
        self._harness = harness

    def get_service(self, name):
        if name == "telegram_harness":
            return self._harness
        return None


class _Agent:
    def __init__(self, data_dir, harness=None):
        self.container = _Container(data_dir, harness)


def _result(text, *, user_id="rob", chat_role=None, chat_type="supergroup", chat_id="-100"):
    source = SessionSource(surface_id="telegram", chat_id=chat_id, chat_type=chat_type)
    identity = Identity(user_id=user_id, source=source, raw_user_id=user_id, chat_role=chat_role)
    inbound = InboundMessage(text=text, identity=identity)
    decision = RouteDecision(kind=RouteKind.COMMAND,
                             session_key=f"agent:main:telegram:{chat_type}:{chat_id}",
                             session_id=None, command=text.split()[0])
    return InboundResult(inbound=inbound, decision=decision)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    return tmp_path


def test_admins_here_owner_only(env):
    agent = _Agent(str(env), harness=_FakeHarness([{"id": "9911", "name": "@bob"}]))
    result = _result("/groups admins here", chat_role="member")
    out = asyncio.run(groups_reply(agent, result, ["admins", "here"]))
    assert out == "🔒 Owner only."


def test_admins_here_renders_the_confirm_command(env):
    agent = _Agent(str(env), harness=_FakeHarness([{"id": "9911", "name": "@bob"}]))
    result = _result("/groups admins here", chat_role="owner")
    out = asyncio.run(groups_reply(agent, result, ["admins", "here"]))
    assert "@bob" in out and "9911" in out
    assert "/groups role here 9911 admin" in out


def test_admins_here_no_suggestions(env):
    agent = _Agent(str(env), harness=_FakeHarness([]))
    result = _result("/groups admins here", chat_role="owner")
    out = asyncio.run(groups_reply(agent, result, ["admins", "here"]))
    assert "no" in out.lower() and "admin" in out.lower()


def test_admins_here_without_a_registered_harness(env):
    agent = _Agent(str(env), harness=None)
    result = _result("/groups admins here", chat_role="owner")
    out = asyncio.run(groups_reply(agent, result, ["admins", "here"]))
    assert "not available" in out.lower()


def test_admins_writes_nothing(env):
    """Suggestions never touch `GroupRoles` — only an explicit `role` grant does."""
    agent = _Agent(str(env), harness=_FakeHarness([{"id": "9911", "name": "@bob"}]))
    result = _result("/groups admins here", chat_role="owner")
    asyncio.run(groups_reply(agent, result, ["admins", "here"]))
    from core.surfaces.group_roles import GroupRoles
    import os
    roles = GroupRoles(os.path.join(str(env), "surfaces.db"))
    assert roles.role("telegram", "-100", "9911", is_owner=False) == "member"
