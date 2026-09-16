"""044 T17: `chat.mode` replaces the mention gate in ``route_inbound``.

The gate is driven by a REAL policy file written through ``chat_policy.set``,
not a monkeypatched loader — the thing under test is that the owner's written
configuration reaches the routing boundary.
"""
import types

import pytest

from core.surfaces.chat_policy import set as set_pol
from core.surfaces.dispatcher import RouteKind, route_inbound
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
from core.surfaces.group_allowlist import GroupAllowlist
from core.surfaces.group_roles import GroupRoles
from core.surfaces.session_chat_registry import SessionChatRegistry

_OWNER = "u_owner"


class _Container:
    def __init__(self, tmp_path, *, chat_reg=None):
        self._svc = {}
        if chat_reg is not None:
            self._svc["session_chat_registry"] = chat_reg
        self.config = types.SimpleNamespace(data_dir=str(tmp_path))

    def get_service(self, name):
        return self._svc.get(name)


def _group_inbound(text, *, user, surface="discord", chat="chan-1",
                   mentions_bot=None):
    src = SessionSource(surface_id=surface, chat_id=chat, chat_type="group")
    ident = Identity(user_id=user, source=src, raw_user_id=user)
    return InboundMessage(text=text, identity=ident, mentions_bot=mentions_bot)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for k in ("POLYROB_REQUIRE_PAIRING", "POLYROB_LOCAL",
              "CORRESPONDENT_ACCESS_ENABLED", "GROUP_REQUIRE_MENTION",
              "GROUP_DEFAULT_MODE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SESSION_RESET_MODE", "none")
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", _OWNER)
    monkeypatch.setenv("POLYROB_INSTANCE_ID", "polyrob")
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("discord", "chan-1")


def _container(tmp_path):
    return _Container(tmp_path,
                      chat_reg=SessionChatRegistry(str(tmp_path / "c.db")))


def _set(tmp_path, key, value, chat="chan-1"):
    ok, msg = set_pol(tmp_path, _OWNER, "discord", chat, key, value)
    assert ok, msg


@pytest.mark.asyncio
async def test_mention_mode_is_the_default(tmp_path):
    c = _container(tmp_path)
    assert (await route_inbound(c, _group_inbound("hi", user="u1", mentions_bot=True))
            ).kind == RouteKind.TASK_AGENT
    d = await route_inbound(c, _group_inbound("hi", user="u2", mentions_bot=False))
    assert d.kind == RouteKind.DENIED and d.silent is True


@pytest.mark.asyncio
async def test_listen_mode_denies_member_mention(tmp_path):
    _set(tmp_path, "chat.mode", "listen")
    c = _container(tmp_path)
    d = await route_inbound(c, _group_inbound("hi", user="u1", mentions_bot=True))
    assert d.kind == RouteKind.DENIED and d.silent is True


@pytest.mark.asyncio
async def test_listen_mode_still_hears_an_admin(tmp_path):
    _set(tmp_path, "chat.mode", "listen")
    GroupRoles(str(tmp_path / "surfaces.db")).grant(
        "discord", "chan-1", "u_admin", "admin", granted_by=_OWNER)
    c = _container(tmp_path)
    d = await route_inbound(c, _group_inbound("hi", user="u_admin", mentions_bot=True))
    assert d.kind == RouteKind.TASK_AGENT


@pytest.mark.asyncio
async def test_active_mode_routes_unmentioned_member(tmp_path):
    _set(tmp_path, "chat.mode", "active")
    c = _container(tmp_path)
    d = await route_inbound(c, _group_inbound("just talking", user="u1",
                                              mentions_bot=False))
    assert d.kind == RouteKind.TASK_AGENT


@pytest.mark.asyncio
async def test_off_mode_denies_even_the_owner(tmp_path):
    """`off` means off. An owner who wants the room back changes the mode from
    a seat, not by shouting into it."""
    _set(tmp_path, "chat.mode", "off")
    c = _container(tmp_path)
    d = await route_inbound(c, _group_inbound("hi", user=_OWNER, mentions_bot=True))
    assert d.kind == RouteKind.DENIED and d.silent is True


@pytest.mark.asyncio
async def test_a_wake_word_triggers_without_a_mention(tmp_path):
    _set(tmp_path, "chat.wake_words", ["hey rob"])
    c = _container(tmp_path)
    d = await route_inbound(c, _group_inbound("hey Rob, what is the time?", user="u1",
                                              mentions_bot=False))
    assert d.kind == RouteKind.TASK_AGENT
    d2 = await route_inbound(c, _group_inbound("nothing to see", user="u2",
                                               mentions_bot=False))
    assert d2.kind == RouteKind.DENIED


@pytest.mark.asyncio
async def test_a_blocked_member_is_denied_before_the_mode(tmp_path):
    _set(tmp_path, "chat.mode", "active")
    GroupRoles(str(tmp_path / "surfaces.db")).grant(
        "discord", "chan-1", "u_bad", "blocked", granted_by=_OWNER)
    c = _container(tmp_path)
    d = await route_inbound(c, _group_inbound("hi", user="u_bad", mentions_bot=True))
    assert d.kind == RouteKind.DENIED and d.silent is True


@pytest.mark.asyncio
async def test_require_mention_false_alias_means_active(tmp_path, monkeypatch):
    """No policy file at all: the legacy env still decides, one release long."""
    monkeypatch.setenv("GROUP_REQUIRE_MENTION", "false")
    c = _container(tmp_path)
    d = await route_inbound(c, _group_inbound("just talking", user="u1",
                                              mentions_bot=False))
    assert d.kind == RouteKind.TASK_AGENT


@pytest.mark.asyncio
async def test_the_denial_is_still_the_no_mention_slug(tmp_path):
    """The owner-facing vocabulary did not change: a member who was not
    addressed is still refused for `no_mention`."""
    c = _container(tmp_path)
    d = await route_inbound(c, _group_inbound("hi", user="u1", mentions_bot=False))
    assert d.reason == "no_mention"


@pytest.mark.asyncio
async def test_a_policy_fault_fails_closed(tmp_path, monkeypatch):
    """The group block is fail-CLOSED once the flag is on: a policy load that
    raises denies the turn, it never falls through to the obey path."""
    import core.surfaces.chat_policy as cp

    def _boom(*a, **kw):
        raise RuntimeError("policy store unreadable")

    monkeypatch.setattr(cp, "load", _boom)
    c = _container(tmp_path)
    d = await route_inbound(c, _group_inbound("hi", user="u1", mentions_bot=True))
    assert d.kind == RouteKind.DENIED and d.reason == "group_fault"


@pytest.mark.asyncio
async def test_dm_is_untouched_by_the_mode(tmp_path):
    _set(tmp_path, "chat.mode", "off")
    c = _container(tmp_path)
    src = SessionSource(surface_id="discord", chat_id="chan-1", chat_type="dm")
    ident = Identity(user_id=_OWNER, source=src, raw_user_id=_OWNER)
    d = await route_inbound(c, InboundMessage(text="hi", identity=ident))
    assert d.kind == RouteKind.TASK_AGENT


@pytest.mark.asyncio
async def test_mention_mode_denies_an_unaddressed_owner(tmp_path):
    """The gate binds EVERYONE. `active` is how an owner asks for every line to
    be answered; shouting into a `mention` room is not."""
    c = _container(tmp_path)
    d = await route_inbound(c, _group_inbound("ambient chatter", user=_OWNER,
                                              mentions_bot=False))
    assert d.kind == RouteKind.DENIED and d.silent is True


@pytest.mark.asyncio
async def test_listen_mode_denies_an_unaddressed_owner(tmp_path):
    _set(tmp_path, "chat.mode", "listen")
    c = _container(tmp_path)
    d = await route_inbound(c, _group_inbound("ambient chatter", user=_OWNER,
                                              mentions_bot=False))
    assert d.kind == RouteKind.DENIED and d.silent is True
    # …and answers him the moment he addresses it.
    d2 = await route_inbound(c, _group_inbound("@bot status?", user=_OWNER,
                                               mentions_bot=True))
    assert d2.kind == RouteKind.TASK_AGENT


@pytest.mark.asyncio
async def test_active_mode_answers_an_unaddressed_owner(tmp_path):
    _set(tmp_path, "chat.mode", "active")
    c = _container(tmp_path)
    d = await route_inbound(c, _group_inbound("ambient chatter", user=_OWNER,
                                              mentions_bot=False))
    assert d.kind == RouteKind.TASK_AGENT
