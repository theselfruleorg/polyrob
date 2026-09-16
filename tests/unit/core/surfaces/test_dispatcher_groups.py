"""Wave 3 Task 3 — route_inbound group-chat routing (GROUP_CHAT_ENABLED).

- Flag OFF (default): non-DM inbound is silently DENIED — a bot invited into a
  group/channel must not obey arbitrary members (discord/slack/signal have no
  sender allowlist of their own, so the old legacy fall-through meant "obey
  everyone in the room"). DMs are untouched.
- Flag ON:
  - unlisted chat -> DENIED silent (no pairing spam into channels);
  - owner mentioned in an allowed chat -> legacy flow (TASK_AGENT/STEER);
  - owner NOT mentioned (mention gate on) -> DENIED silent;
  - member mentioned + bound session -> GROUP_TURN into it (044 T14);
  - member mentioned, NO bound session -> TASK_AGENT: a member may START the
    room session (the PUBLIC profile + the room tool gate bound it);
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

    # 3) A DIFFERENT member's @mention runs a room TURN in the owner's session.
    d = await route_inbound(c, _group_inbound("@bot help", user="u_stranger",
                                              mentions_bot=True))
    assert d.kind == RouteKind.GROUP_TURN
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
    """044 T17 replaced the gate with `chat.mode`, but not the rule: in the
    default `mention` mode an unaddressed line is denied for EVERYONE, the owner
    included. He is a person in a room full of other people — `active` is how he
    asks for every line to be answered."""
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner")
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("discord", "chan-1")
    c = _Container(tmp_path, chat_reg=SessionChatRegistry(str(tmp_path / "c.db")))
    d = await route_inbound(c, _group_inbound("ambient chatter", user="u_owner",
                                              mentions_bot=False))
    assert d.kind == RouteKind.DENIED and d.silent is True


@pytest.mark.asyncio
async def test_member_unmentioned_denied_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner")
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("discord", "chan-1")
    c = _Container(tmp_path, chat_reg=SessionChatRegistry(str(tmp_path / "c.db")))
    d = await route_inbound(c, _group_inbound("ambient chatter", user="u_member",
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
async def test_member_mentioned_with_bound_session_is_a_room_turn(tmp_path, monkeypatch):
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
    assert d.kind == RouteKind.GROUP_TURN
    assert d.session_id == "sess-group-1"


@pytest.mark.asyncio
async def test_member_without_session_starts_one(tmp_path, monkeypatch):
    """044 T14: a member may START the room session. It was a silent DENIED —
    so a room the owner had never spoken in answered nobody, ever."""
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("discord", "chan-1")
    c = _Container(tmp_path, chat_reg=SessionChatRegistry(str(tmp_path / "c.db")))
    d = await route_inbound(c, _group_inbound("@bot hi", user="u_stranger",
                                              mentions_bot=True))
    assert d.kind == RouteKind.TASK_AGENT


@pytest.mark.asyncio
async def test_owner_with_a_bound_room_session_is_a_room_turn(tmp_path, monkeypatch):
    """044 T14: the owner's own line in a room also gets the <group-context>
    block — so it routes GROUP_TURN, not STEER, once a session is bound."""
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner")
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("discord", "chan-1")
    reg = SessionChatRegistry(str(tmp_path / "c.db"))
    c = _Container(tmp_path, chat_reg=reg)
    from core.surfaces.session_chat_registry import build_session_key
    src = SessionSource(surface_id="discord", chat_id="chan-1", chat_type="group")
    reg.bind(build_session_key(src, "u_owner"), "sess-group-1", "u_owner",
             "discord", "chan-1")
    d = await route_inbound(c, _group_inbound("and now this", user="u_owner",
                                              mentions_bot=True))
    assert d.kind == RouteKind.GROUP_TURN and d.session_id == "sess-group-1"


@pytest.mark.asyncio
async def test_owner_command_in_a_room_is_still_a_command(tmp_path, monkeypatch):
    """The room turn replaces STEER, never COMMAND — the owner's control plane
    has to keep working from inside a room."""
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner")
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("discord", "chan-1")
    reg = SessionChatRegistry(str(tmp_path / "c.db"))
    c = _Container(tmp_path, chat_reg=reg)
    from core.surfaces.session_chat_registry import build_session_key
    src = SessionSource(surface_id="discord", chat_id="chan-1", chat_type="group")
    reg.bind(build_session_key(src, "u_owner"), "sess-group-1", "u_owner",
             "discord", "chan-1")
    d = await route_inbound(c, _group_inbound("/status", user="u_owner",
                                              mentions_bot=True))
    assert d.kind == RouteKind.COMMAND and d.command == "/status"


@pytest.mark.asyncio
async def test_mention_none_counts_as_unmentioned(tmp_path, monkeypatch):
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("discord", "chan-1")
    c = _Container(tmp_path, chat_reg=SessionChatRegistry(str(tmp_path / "c.db")))
    d = await route_inbound(c, _group_inbound("hi", user="u_stranger",
                                              mentions_bot=None))
    assert d.kind == RouteKind.DENIED and d.silent is True


# ---------------------------------------------------------------------------
# 044 T18 fix round 1 (Critical 1a) — a room ADMIN's own control-verb line is
# a COMMAND, not a room turn. A plain MEMBER's identical line is not.
# ---------------------------------------------------------------------------

def _grant_admin(tmp_path, surface, chat, user):
    from core.surfaces.group_roles import GroupRoles
    roles = GroupRoles(str(tmp_path / "surfaces.db"))
    roles.grant(surface, chat, user, "admin", granted_by="u_owner")
    return roles


@pytest.mark.asyncio
async def test_admin_groups_command_routes_as_command(tmp_path, monkeypatch):
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner")
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("discord", "chan-1")
    roles = _grant_admin(tmp_path, "discord", "chan-1", "u_admin")
    c = _Container(tmp_path, chat_reg=SessionChatRegistry(str(tmp_path / "c.db")))
    c._svc["group_roles"] = roles
    d = await route_inbound(c, _group_inbound("/groups mode here listen", user="u_admin",
                                              mentions_bot=True))
    assert d.kind == RouteKind.COMMAND
    assert d.command == "/groups"


@pytest.mark.asyncio
async def test_admin_mute_command_routes_as_command(tmp_path, monkeypatch):
    """Every admin-reachable verb, not just /groups."""
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner")
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("discord", "chan-1")
    roles = _grant_admin(tmp_path, "discord", "chan-1", "u_admin")
    c = _Container(tmp_path, chat_reg=SessionChatRegistry(str(tmp_path / "c.db")))
    c._svc["group_roles"] = roles
    d = await route_inbound(c, _group_inbound("/mute here 2h", user="u_admin",
                                              mentions_bot=True))
    assert d.kind == RouteKind.COMMAND and d.command == "/mute"


@pytest.mark.asyncio
async def test_member_groups_command_is_not_a_command(tmp_path, monkeypatch):
    """The SAME line from a plain member (no admin row) is not a command — it
    stays the room's own turn (member verbs are deferred, a later schema row)."""
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner")
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("discord", "chan-1")
    c = _Container(tmp_path, chat_reg=SessionChatRegistry(str(tmp_path / "c.db")))
    d = await route_inbound(c, _group_inbound("/groups mode here listen", user="u_member",
                                              mentions_bot=True))
    assert d.kind != RouteKind.COMMAND
    assert d.kind == RouteKind.TASK_AGENT  # no bound session yet -> starts one


@pytest.mark.asyncio
async def test_member_groups_command_with_a_bound_session_is_a_room_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner")
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("discord", "chan-1")
    reg = SessionChatRegistry(str(tmp_path / "c.db"))
    c = _Container(tmp_path, chat_reg=reg)
    from core.surfaces.session_chat_registry import build_session_key
    src = SessionSource(surface_id="discord", chat_id="chan-1", chat_type="group")
    reg.bind(build_session_key(src, "u_owner"), "sess-group-1", "u_owner",
             "discord", "chan-1")
    d = await route_inbound(c, _group_inbound("/groups mode here listen", user="u_member",
                                              mentions_bot=True))
    assert d.kind == RouteKind.GROUP_TURN and d.session_id == "sess-group-1"


# ---------------------------------------------------------------------------
# Task 22 item 2 — a slash command is addressed on its own: an admin/owner
# `/verb` reaches the dispatcher without an `@mention`, but a plain member's
# `/verb` gets no such bonus.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_admin_mute_unaddressed_in_a_muted_room_routes_as_command(tmp_path, monkeypatch):
    """`/mute here 1h` from an admin, with NO @mention, in a room already
    demoted to `listen` by an earlier mute. Without the `is_command` bonus in
    `mode_allows_trigger`, this line would never pass the `no_mention` gate —
    an admin could never lift a mute without first re-addressing the muted bot."""
    import time

    from core.surfaces.chat_policy import set as set_pol
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner")
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("discord", "chan-1")
    ok, msg = set_pol(str(tmp_path), "u_owner", "discord", "chan-1",
                      "chat.mute_until", time.time() + 3600)
    assert ok, msg
    roles = _grant_admin(tmp_path, "discord", "chan-1", "u_admin")
    c = _Container(tmp_path, chat_reg=SessionChatRegistry(str(tmp_path / "c.db")))
    c._svc["group_roles"] = roles
    d = await route_inbound(c, _group_inbound("/mute here 1h", user="u_admin",
                                              mentions_bot=False))
    assert d.kind == RouteKind.COMMAND and d.command == "/mute"


@pytest.mark.asyncio
async def test_member_unaddressed_slash_command_in_mention_room_is_denied(tmp_path, monkeypatch):
    """A plain member's `/foo`, unaddressed, in the default `mention` room:
    the slash-command bonus is owner/admin only, so this stays denied exactly
    as an unaddressed plain-text line would."""
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner")
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("discord", "chan-1")
    c = _Container(tmp_path, chat_reg=SessionChatRegistry(str(tmp_path / "c.db")))
    d = await route_inbound(c, _group_inbound("/foo", user="u_member",
                                              mentions_bot=False))
    assert d.kind == RouteKind.DENIED


# ---------------------------------------------------------------------------
# 044 C6 — `/groups allow here` must work in a room that is NOT yet allowlisted
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_owner_groups_verb_routes_in_a_not_yet_allowed_room(tmp_path, monkeypatch):
    """`resolve_access_tier` checks the ALLOWLIST before the owner, so the owner's
    own `/groups allow here` — the verb that CREATES the allowlist row — was a
    silent DENIED in the one room where it is needed. The documented way to allow
    a room from inside it could never work."""
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner")
    c = _Container(tmp_path, chat_reg=SessionChatRegistry(str(tmp_path / "c.db")))
    d = await route_inbound(c, _group_inbound("/groups allow here", user="u_owner"))
    assert d.kind == RouteKind.COMMAND
    assert d.command == "/groups"
    assert d.session_id is None


@pytest.mark.asyncio
async def test_owner_groups_verb_tolerates_the_botname_suffix(tmp_path, monkeypatch):
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner")
    c = _Container(tmp_path, chat_reg=SessionChatRegistry(str(tmp_path / "c.db")))
    d = await route_inbound(c, _group_inbound("/groups@MyBot list", user="u_owner"))
    assert d.kind == RouteKind.COMMAND and d.command == "/groups"


@pytest.mark.asyncio
async def test_non_owner_groups_verb_is_still_denied_in_an_unlisted_room(tmp_path, monkeypatch):
    """The pre-check is owner-only and narrow: nothing else changes."""
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner")
    c = _Container(tmp_path, chat_reg=SessionChatRegistry(str(tmp_path / "c.db")))
    d = await route_inbound(c, _group_inbound("/groups allow here", user="u_stranger"))
    assert d.kind == RouteKind.DENIED and d.silent is True


@pytest.mark.asyncio
async def test_owner_ordinary_line_is_still_denied_in_an_unlisted_room(tmp_path, monkeypatch):
    """Only `/groups` moves. An unlisted room still answers nobody, owner included."""
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner")
    c = _Container(tmp_path, chat_reg=SessionChatRegistry(str(tmp_path / "c.db")))
    d = await route_inbound(c, _group_inbound("hey rob", user="u_owner",
                                              mentions_bot=True))
    assert d.kind == RouteKind.DENIED and d.silent is True


# ---------------------------------------------------------------------------
# 044 I1 — the hourly reply cap must bound what is SPENT, not just what is SAID
# ---------------------------------------------------------------------------

def _caps(*, may_reply=(True, ""), may_trigger=(True, "")):
    recorded = []
    caps = types.SimpleNamespace(
        may_trigger=lambda *a, **k: may_trigger,
        may_reply=lambda *a, **k: may_reply,
        record_trigger=lambda *a, **k: recorded.append(a),
    )
    caps.recorded = recorded
    return caps


@pytest.mark.asyncio
async def test_member_turn_is_refused_when_the_room_reply_cap_is_spent(tmp_path, monkeypatch):
    """`may_trigger` bounds ONE member (cooldown + bot loop); the per-CHAT hourly
    bound lived only in `may_reply`, at PUBLISH time. So N members meant N paid
    model calls and the cap only suppressed the output."""
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner")
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("discord", "chan-1")
    reg = SessionChatRegistry(str(tmp_path / "c.db"))
    reg.bind("agent:main:discord:group:chan-1", "sess-1", "u_owner", "discord", "chan-1")
    c = _Container(tmp_path, chat_reg=reg)
    c._svc["room_caps"] = _caps(may_reply=(False, "room reply cap (20/h) reached"))

    d = await route_inbound(c, _group_inbound("hey", user="u_member", mentions_bot=True))
    assert d.kind == RouteKind.DENIED
    assert d.silent is True and d.reason == "room_cap"


@pytest.mark.asyncio
async def test_member_cold_start_is_refused_when_capped(tmp_path, monkeypatch):
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner")
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("discord", "chan-1")
    c = _Container(tmp_path, chat_reg=SessionChatRegistry(str(tmp_path / "c.db")))
    c._svc["room_caps"] = _caps(may_reply=(False, "room reply cap (20/h) reached"))

    d = await route_inbound(c, _group_inbound("hey", user="u_member", mentions_bot=True))
    assert d.kind == RouteKind.DENIED and d.reason == "room_cap"


@pytest.mark.asyncio
async def test_an_admin_control_verb_still_works_in_a_capped_room(tmp_path, monkeypatch):
    """A capped room must stay CONFIGURABLE — `/groups mode here off` is how an
    admin stops the noise that spent the cap in the first place."""
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner")
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("discord", "chan-1")
    from core.surfaces.group_roles import GroupRoles
    import os
    GroupRoles(os.path.join(str(tmp_path), "surfaces.db")).grant(
        "discord", "chan-1", "u_admin", "admin", granted_by="u_owner")
    c = _Container(tmp_path, chat_reg=SessionChatRegistry(str(tmp_path / "c.db")))
    c._svc["room_caps"] = _caps(may_reply=(False, "room reply cap (20/h) reached"))

    d = await route_inbound(c, _group_inbound("/groups mode here off", user="u_admin",
                                              mentions_bot=True))
    assert d.kind == RouteKind.COMMAND and d.command == "/groups"


@pytest.mark.asyncio
async def test_owner_command_still_works_in_a_capped_room(tmp_path, monkeypatch):
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner")
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("discord", "chan-1")
    c = _Container(tmp_path, chat_reg=SessionChatRegistry(str(tmp_path / "c.db")))
    c._svc["room_caps"] = _caps(may_reply=(False, "capped"))
    d = await route_inbound(c, _group_inbound("/mute here 2h", user="u_owner",
                                              mentions_bot=True))
    assert d.kind == RouteKind.COMMAND


@pytest.mark.asyncio
async def test_owner_room_turn_is_refused_when_capped(tmp_path, monkeypatch):
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner")
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("discord", "chan-1")
    c = _Container(tmp_path, chat_reg=SessionChatRegistry(str(tmp_path / "c.db")))
    c._svc["room_caps"] = _caps(may_reply=(False, "capped"))
    d = await route_inbound(c, _group_inbound("hey rob", user="u_owner",
                                              mentions_bot=True))
    assert d.kind == RouteKind.DENIED and d.reason == "room_cap"


# ---------------------------------------------------------------------------
# 044 I11 — a room session must age out like every other chat session
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_stale_room_session_starts_fresh_for_a_member(tmp_path, monkeypatch):
    """The member branch returned GROUP_TURN upstream of `should_start_fresh`, so
    a room accumulated ONE session forever while the OWNER's line in the same
    room did reset (it falls through to the STEER site)."""
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner")
    monkeypatch.setenv("SESSION_RESET_MODE", "idle")
    monkeypatch.setenv("SESSION_IDLE_MINUTES", "30")
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("discord", "chan-1")
    reg = SessionChatRegistry(str(tmp_path / "c.db"))
    key = "agent:main:discord:group:chan-1"
    reg.bind(key, "sess-old", "u_owner", "discord", "chan-1")
    # Age the row past the idle boundary.
    from core.sqlite_util import execute_retry
    import time as _time
    execute_retry(str(tmp_path / "c.db"),
                  "UPDATE session_chat_map SET updated_at=? WHERE session_key=?",
                  (_time.time() - 7200, key))
    c = _Container(tmp_path, chat_reg=reg)

    d = await route_inbound(c, _group_inbound("hey", user="u_member", mentions_bot=True))
    assert d.kind == RouteKind.TASK_AGENT, "a stale room session was resumed"


@pytest.mark.asyncio
async def test_a_warm_room_session_is_still_a_group_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "u_owner")
    monkeypatch.setenv("SESSION_RESET_MODE", "idle")
    monkeypatch.setenv("SESSION_IDLE_MINUTES", "30")
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow("discord", "chan-1")
    reg = SessionChatRegistry(str(tmp_path / "c.db"))
    reg.bind("agent:main:discord:group:chan-1", "sess-warm", "u_owner", "discord", "chan-1")
    c = _Container(tmp_path, chat_reg=reg)

    d = await route_inbound(c, _group_inbound("hey", user="u_member", mentions_bot=True))
    assert d.kind == RouteKind.GROUP_TURN and d.session_id == "sess-warm"
