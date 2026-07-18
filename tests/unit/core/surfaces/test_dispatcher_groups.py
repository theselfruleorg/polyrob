"""Wave 3 Task 3 — route_inbound group-chat routing (GROUP_CHAT_ENABLED).

- Flag OFF (default): non-DM inbound is silently DENIED — a bot invited into a
  group/channel must not obey arbitrary members (discord/slack/signal have no
  sender allowlist of their own, so the old legacy fall-through meant "obey
  everyone in the room"). DMs are untouched.
- Flag ON:
  - unlisted chat -> DENIED silent (no pairing spam into channels);
  - owner mentioned in an allowed chat -> legacy flow (TASK_AGENT/STEER);
  - owner NOT mentioned (mention gate on) -> DENIED silent;
  - participant mentioned + bound session -> CORRESPONDENT_DATA into it;
  - participant mentioned, NO bound session -> DENIED silent;
  - mention state None counts as not mentioned (fail-closed).
"""
import types

import pytest

from core.surfaces.dispatcher import RouteKind, route_inbound
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
from core.surfaces.group_allowlist import GroupAllowlist
from core.surfaces.session_chat_registry import SessionChatRegistry


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
def _clean_env(monkeypatch):
    for k in ("POLYROB_REQUIRE_PAIRING", "POLYROB_LOCAL",
              "POLYROB_OWNER_USER_ID", "CORRESPONDENT_ACCESS_ENABLED",
              "GROUP_CHAT_ENABLED", "GROUP_REQUIRE_MENTION",
              "SESSION_RESET_MODE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SESSION_RESET_MODE", "none")


@pytest.mark.asyncio
async def test_flag_off_group_denied_silent(tmp_path):
    # OFF is the default: with no allowlist/tier/mention gate active, falling
    # through would let ANY room member drive the agent on surfaces without a
    # sender allowlist (discord/slack/signal). Deny silently instead.
    c = _Container(tmp_path, chat_reg=SessionChatRegistry(str(tmp_path / "c.db")))
    d = await route_inbound(c, _group_inbound("hi", user="u1"))
    assert d.kind == RouteKind.DENIED
    assert d.silent is True


@pytest.mark.asyncio
async def test_flag_off_dm_still_routes(tmp_path):
    c = _Container(tmp_path, chat_reg=SessionChatRegistry(str(tmp_path / "c.db")))
    src = SessionSource(surface_id="discord", chat_id="d1", chat_type="dm")
    ident = Identity(user_id="u1", source=src, raw_user_id="u1")
    d = await route_inbound(c, InboundMessage(text="hi", identity=ident))
    assert d.kind == RouteKind.TASK_AGENT


@pytest.mark.asyncio
async def test_owner_group_session_binds_through_real_write_path(tmp_path, monkeypatch):
    """B-3 coverage: the participant rail depends on the OWNER's group session
    being bound via bind_chat_surface — which is gated on SINGULAR_CHAT_ENABLED
    (every `polyrob <surface>` daemon sets it). Bind through the REAL write
    path, not reg.bind directly, so a regression in that gate is visible."""
    import types as _types

    from core.surfaces.binding import bind_chat_surface

    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    monkeypatch.setenv("SINGULAR_CHAT_ENABLED", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner")
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("discord", "chan-1")
    reg = SessionChatRegistry(str(tmp_path / "c.db"))
    c = _Container(tmp_path, chat_reg=reg)
    c._svc["message_router"] = object()  # bus present (truthy)

    # 1) The owner's message routes TASK_AGENT with the group session key.
    owner_decision = await route_inbound(
        c, _group_inbound("do the thing", user="u_owner", mentions_bot=True))
    assert owner_decision.kind == RouteKind.TASK_AGENT

    # 2) create_session would bind that key via bind_chat_surface.
    src = SessionSource(surface_id="discord", chat_id="chan-1", chat_type="group")
    orch = _types.SimpleNamespace()
    bound = bind_chat_surface(
        orch, c, session_source=src,
        chat_session_key=owner_decision.session_key,
        session_id="sess-group-1", user_id="u_owner")
    assert bound is True

    # 3) A DIFFERENT participant's @mention lands as DATA in the owner's session.
    d = await route_inbound(c, _group_inbound("@bot help", user="u_stranger",
                                              mentions_bot=True))
    assert d.kind == RouteKind.CORRESPONDENT_DATA
    assert d.session_id == "sess-group-1"


@pytest.mark.asyncio
async def test_unlisted_chat_denied_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    c = _Container(tmp_path, chat_reg=SessionChatRegistry(str(tmp_path / "c.db")))
    d = await route_inbound(c, _group_inbound("hi", user="u1",
                                              mentions_bot=True))
    assert d.kind == RouteKind.DENIED
    assert d.silent is True


@pytest.mark.asyncio
async def test_owner_mentioned_falls_through(tmp_path, monkeypatch):
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner")
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("discord", "chan-1")
    c = _Container(tmp_path, chat_reg=SessionChatRegistry(str(tmp_path / "c.db")))
    d = await route_inbound(c, _group_inbound("do the thing", user="u_owner",
                                              mentions_bot=True))
    assert d.kind == RouteKind.TASK_AGENT


@pytest.mark.asyncio
async def test_owner_unmentioned_denied_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner")
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("discord", "chan-1")
    c = _Container(tmp_path, chat_reg=SessionChatRegistry(str(tmp_path / "c.db")))
    d = await route_inbound(c, _group_inbound("ambient chatter", user="u_owner",
                                              mentions_bot=False))
    assert d.kind == RouteKind.DENIED and d.silent is True


@pytest.mark.asyncio
async def test_owner_unmentioned_allowed_when_gate_off(tmp_path, monkeypatch):
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    monkeypatch.setenv("GROUP_REQUIRE_MENTION", "false")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner")
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("discord", "chan-1")
    c = _Container(tmp_path, chat_reg=SessionChatRegistry(str(tmp_path / "c.db")))
    d = await route_inbound(c, _group_inbound("hi", user="u_owner",
                                              mentions_bot=False))
    assert d.kind == RouteKind.TASK_AGENT


@pytest.mark.asyncio
async def test_participant_mentioned_with_bound_session_is_data(tmp_path, monkeypatch):
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner")
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("discord", "chan-1")
    reg = SessionChatRegistry(str(tmp_path / "c.db"))
    c = _Container(tmp_path, chat_reg=reg)

    # Owner activity bound a session to the group key.
    from core.surfaces.session_chat_registry import build_session_key
    src = SessionSource(surface_id="discord", chat_id="chan-1", chat_type="group")
    key = build_session_key(src, "u_owner")
    reg.bind(key, "sess-group-1", "u_owner", "discord", "chan-1")

    d = await route_inbound(c, _group_inbound("@bot help", user="u_stranger",
                                              mentions_bot=True))
    assert d.kind == RouteKind.CORRESPONDENT_DATA
    assert d.session_id == "sess-group-1"


@pytest.mark.asyncio
async def test_participant_without_session_denied_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("discord", "chan-1")
    c = _Container(tmp_path, chat_reg=SessionChatRegistry(str(tmp_path / "c.db")))
    d = await route_inbound(c, _group_inbound("@bot hi", user="u_stranger",
                                              mentions_bot=True))
    assert d.kind == RouteKind.DENIED and d.silent is True


@pytest.mark.asyncio
async def test_mention_none_counts_as_unmentioned(tmp_path, monkeypatch):
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("discord", "chan-1")
    c = _Container(tmp_path, chat_reg=SessionChatRegistry(str(tmp_path / "c.db")))
    d = await route_inbound(c, _group_inbound("hi", user="u_stranger",
                                              mentions_bot=None))
    assert d.kind == RouteKind.DENIED and d.silent is True
