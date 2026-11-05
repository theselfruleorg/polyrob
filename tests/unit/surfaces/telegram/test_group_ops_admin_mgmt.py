"""101 Phase 1: granting and listing roles, and what an admin may not do."""
import asyncio
import time
from types import SimpleNamespace

import pytest

from core.surfaces import group_admin
from core.surfaces.command_reply import reply_to_room
from core.surfaces.dispatcher import RouteDecision, RouteKind
from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
from core.surfaces.group_ledger import GroupLedger, LedgerRow
from core.surfaces.group_roles import GroupRoles
from core.runtime_paths import surfaces_db_path
from surfaces.telegram import group_ops, room_help
from surfaces.telegram.inbound import InboundResult

CHAT = "-100"


class _Container:
    def __init__(self, data_dir):
        self.config = SimpleNamespace(data_dir=data_dir)

    def get_service(self, name):
        return None


class _Agent:
    def __init__(self, data_dir):
        self.container = _Container(data_dir)


def _result(text, *, role, uid="55", reply_from=None, forum_root=False, chat_type="supergroup"):
    source = SessionSource(surface_id="telegram", chat_id=CHAT, chat_type=chat_type)
    identity = Identity(user_id=uid, source=source, raw_user_id=uid, chat_role=role)
    msg = {"message_id": 7, "chat": {"id": CHAT}, "from": {"id": uid}, "text": text}
    if reply_from is not None:
        msg["reply_to_message"] = {"message_id": 6, "from": reply_from}
        if forum_root:
            msg["reply_to_message"]["forum_topic_created"] = {"name": "t"}
    inbound = InboundMessage(text=text, identity=identity, raw={"message": msg})
    decision = RouteDecision(kind=RouteKind.COMMAND, command="/groups",
                             session_key=f"agent:main:telegram:{chat_type}:{CHAT}")
    return InboundResult(inbound=inbound, decision=decision)


def _groups(agent, result, args):
    return asyncio.run(group_ops.groups_reply(agent, result, args))


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_TELEGRAM_ID", "999")
    return _Agent(str(tmp_path))


def _roles(agent):
    return GroupRoles(surfaces_db_path(agent.container.config.data_dir))


def _log_line(agent, uid, name):
    GroupLedger(surfaces_db_path(agent.container.config.data_dir)).append(LedgerRow(
        surface="telegram", chat_id=CHAT, thread_id=None, message_id=f"m{uid}", ts=time.time(),
        sender_id=uid, sender_name=name, sender_is_bot=False, role_at_write="member",
        kind="text", text="hi", reply_to_message_id=None, mentions_bot=False, media_path=None))


def test_owner_grants_by_reply(agent):
    out = _groups(agent, _result("x", role="owner", reply_from={"id": 77}),
                  ["role", "here", "admin"])
    assert "admin" in out
    assert _roles(agent).role("telegram", CHAT, "77", is_owner=False) == "admin"


def test_a_forum_topics_implicit_reply_is_not_a_reply(agent):
    """R3: every topic message `replies` to the root; the topic creator gains nothing."""
    _groups(agent, _result("x", role="owner", reply_from={"id": 77}, forum_root=True),
            ["role", "here", "admin"])
    assert _roles(agent).role("telegram", CHAT, "77", is_owner=False) == "member"


def test_a_bot_cannot_hold_a_role(agent):
    out = _groups(agent, _result("x", role="owner", reply_from={"id": 77, "is_bot": True}),
                  ["role", "here", "admin"])
    assert "bot" in out
    assert _roles(agent).list("telegram", CHAT) == []


def test_an_admin_cannot_grant_admin_by_the_reply_form(agent):
    out = _groups(agent, _result("x", role="admin", reply_from={"id": 77}),
                  ["role", "here", "admin"])
    assert out == group_ops._OWNER_ONLY_TEXT
    assert _roles(agent).list("telegram", CHAT) == []


def test_an_admin_may_block_a_member_by_reply(agent):
    _groups(agent, _result("x", role="admin", reply_from={"id": 77}),
            ["role", "here", "blocked"])
    assert _roles(agent).role("telegram", CHAT, "77", is_owner=False) == "blocked"


def test_an_admin_cannot_block_an_admin_by_reply(agent):
    _roles(agent).grant("telegram", CHAT, "77", "admin", granted_by="999")
    out = _groups(agent, _result("x", role="admin", reply_from={"id": 77}),
                  ["role", "here", "blocked"])
    assert "Only the owner" in out
    assert _roles(agent).role("telegram", CHAT, "77", is_owner=False) == "admin"


def test_an_admin_cannot_block_the_owner(agent):
    out = _groups(agent, _result("x", role="admin"), ["role", "here", "999", "blocked"])
    assert "Only the owner" in out
    assert _roles(agent).list("telegram", CHAT) == []


def test_a_spoofed_name_never_receives_a_role(agent):
    """R1: a member with no username renamed to the name the owner will type."""
    _log_line(agent, "66", "@bob")
    out = _groups(agent, _result("x", role="owner"), ["role", "here", "@bob", "admin"])
    assert _roles(agent).list("telegram", CHAT) == []
    assert "66" in out and "/groups role telegram _100 66 admin" in out


def test_a_name_held_by_two_members_lists_both(agent):
    _log_line(agent, "66", "bob")
    _log_line(agent, "67", "bob")
    out = _groups(agent, _result("x", role="owner"), ["role", "here", "bob", "admin"])
    assert "66" in out and "67" in out
    assert _roles(agent).list("telegram", CHAT) == []


def test_an_admin_cannot_block_an_admin_by_name(agent):
    """R2: a name never reaches the store, so it cannot walk past the admin check."""
    _roles(agent).grant("telegram", CHAT, "77", "admin", granted_by="999")
    _log_line(agent, "77", "@boss")
    _groups(agent, _result("x", role="admin"), ["role", "here", "@boss", "blocked"])
    assert _roles(agent).role("telegram", CHAT, "77", is_owner=False) == "admin"


def test_member_removes_the_row(agent):
    _roles(agent).grant("telegram", CHAT, "77", "admin", granted_by="999")
    out = _groups(agent, _result("x", role="owner"), ["role", "here", "77", "member"])
    assert "member again" in out
    assert _roles(agent).list("telegram", CHAT) == []
    assert _roles(agent).role("telegram", CHAT, "77", is_owner=False) == "member"
    again = _groups(agent, _result("x", role="owner"), ["role", "here", "77", "member"])
    assert "nothing changed" in again


def test_a_new_grant_dates_from_now(agent):
    roles = _roles(agent)
    roles.grant("telegram", CHAT, "77", "blocked", granted_by="999")
    first = roles.list("telegram", CHAT)[0]["created_at"]
    roles.grant("telegram", CHAT, "77", "admin", granted_by="999")
    assert roles.list("telegram", CHAT)[0]["created_at"] >= first


@pytest.mark.parametrize("role,allowed", [("owner", True), ("admin", True), ("member", False)])
def test_roles_list(agent, role, allowed):
    _roles(agent).grant("telegram", CHAT, "77", "admin", granted_by="999")
    out = _groups(agent, _result("x", role=role), ["roles", "here"])
    assert ("77" in out) is allowed
    if not allowed:
        assert out == group_ops._OWNER_OR_ADMIN_TEXT


def test_the_roles_list_says_when_it_could_not_read(agent, monkeypatch):
    class _Broken:
        def list(self, *_a):
            raise RuntimeError("locked")
    _roles(agent)     # the store file exists; only the read fails
    monkeypatch.setattr(group_admin, "_roles", lambda _c: _Broken())
    out = group_admin.list_roles(agent.container, "telegram", CHAT)
    assert "UNKNOWN" in out


def test_suggestions_name_the_room(agent):
    async def suggest(_chat):
        return [{"name": "Ann", "id": "12"}]
    harness = SimpleNamespace(suggest_admins=suggest)
    agent.container.get_service = lambda n: harness if n == "telegram_harness" else None
    out = _groups(agent, _result("x", role="owner"), ["admins", "here"])
    assert "/groups role telegram _100 12 admin" in out


class _Bot:
    def __init__(self):
        self.sent = []


def test_a_refused_admin_dm_posts_a_note_with_no_output(agent, monkeypatch):
    bot, sent = _Bot(), []
    async def fake_send(_bot, chat, text, actions=None):
        sent.append((chat, text))
    import surfaces.telegram.harness as h
    monkeypatch.setattr(h, "_send_telegram_text", fake_send)
    res = _result("/groups tail here", role="admin")
    done = asyncio.run(room_help.admin_reply_failed(
        bot, res, CHAT, "SECRET ROOM OUTPUT",
        RuntimeError("Telegram server says Forbidden: bot can't initiate conversation")))
    assert done is True
    assert sent == [(CHAT, room_help.ADMIN_DM_NOTE)]
    assert "SECRET" not in sent[0][1] and "Done" not in sent[0][1]


def test_the_owners_failed_reply_keeps_the_missed_rail(agent):
    res = _result("/groups tail here", role="owner")
    assert asyncio.run(room_help.admin_reply_failed(
        _Bot(), res, CHAT, "x", RuntimeError("Forbidden"))) is False


def test_a_reply_meant_for_the_room_is_not_an_admin_dm(agent):
    from core.surfaces.command_reply import CommandReply
    reply = CommandReply("x", to_room=True)
    assert reply_to_room(reply)
    res = _result("/mute", role="admin")
    assert asyncio.run(room_help.admin_reply_failed(
        _Bot(), res, CHAT, reply, RuntimeError("Forbidden"))) is False


# --- 101 Phase 2: the owner writes grants, nobody else ------------------------
def test_the_owner_adds_and_removes_a_bundle(agent, monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "local")
    out = _groups(agent, _result("x", role="owner"), ["tools", "here", "admin", "add", "moderation"])
    assert "admin_tools" in out, out
    shown = _groups(agent, _result("x", role="owner"), ["tools", "here"])
    assert "moderation" in shown
    _groups(agent, _result("x", role="owner"), ["tools", "here", "admin", "remove", "moderation"])
    assert "/admin is off" in _groups(agent, _result("x", role="owner"), ["tools", "here"])


def test_an_unknown_bundle_refuses_with_the_list(agent):
    out = _groups(agent, _result("x", role="owner"), ["tools", "here", "admin", "add", "root"])
    assert "moderation" in out and "root" in out


@pytest.mark.parametrize("args", [
    ["tools", "here", "admin", "add", "browser"],
    ["tools", "here", "add", "web_fetch"],
    ["tools", "here"],
    ["set", "here", "admin_tools", "browser"],
    ["set", "here", "chat.room_tools", "web_fetch"],
    ["set", "here", "admin_turns_per_day", "500"],
])
def test_an_admin_cannot_touch_a_grant(agent, args):
    assert _groups(agent, _result("x", role="admin"), args) == group_ops._OWNER_ONLY_TEXT


# --- 101 Phase 4: the agent proposes, the owner taps --------------------------
@pytest.mark.parametrize("line,words", [
    ("/groups role telegram _100 55 admin", ["/groups", "role", "telegram", "_100", "55", "admin"]),
    ("/groups role telegram -100 55 blocked", ["/groups", "role", "telegram", "_100", "55", "blocked"]),
    ("/groups tools telegram _100 admin add http",
     ["/groups", "tools", "telegram", "_100", "admin", "add", "http"]),
    ("/groups tools telegram _100 add web_fetch",
     ["/groups", "tools", "telegram", "_100", "add", "web_fetch"]),
])
def test_a_room_line_becomes_a_card(line, words):
    from core.setting_proposals import CARD, classify
    p = classify(line)
    assert p.outcome == CARD and p.words == words


@pytest.mark.parametrize("line", [
    "/groups role here 55 admin", "/groups role telegram _100 @bob admin",
    "/groups role telegram _100 55 owner", "/groups tools telegram _100 add filesystem",
    "/groups tools telegram _100 add browser", "/groups tools telegram _100 admin add wallet",
    "/groups allow telegram _100", "/groups set telegram _100 admin_tools browser",
    "/groups deny telegram _100", "/groups role telegram abc 55 admin",
])
def test_every_other_room_line_is_refused(line):
    from core.setting_proposals import REFUSED, classify
    assert classify(line).outcome == REFUSED


def test_a_setting_card_holds_a_room_line(tmp_path):
    from core.surfaces import cards
    st = cards.CardStore(str(tmp_path / "cards.db"))
    card = cards.setting_card("owner", ["/groups", "role", "telegram", "_100", "55", "admin"],
                              title="t", st=st)
    assert card.line == "/groups role telegram _100 55 admin"
    done = cards.press(card.card_id, "ok", "owner", st=st)
    assert done.run == card.line
    other = cards.setting_card("owner", ["/groups", "role", "telegram", "_100", "55", "admin"],
                               title="t", st=st)
    assert "No such card" in cards.press(other.card_id, "ok", "somebody_else", st=st).reply
    with pytest.raises(ValueError):
        cards.setting_card("owner", ["/groups", "allow", "telegram", "_100"], title="t", st=st)


def test_the_tapped_line_runs_for_the_owner_and_refuses_an_admin_row(agent, monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "999")
    source_owner = _result("x", role=None, uid="999", chat_type="dm")
    assert "admin" in _groups(agent, source_owner, ["role", "telegram", "_100", "55", "admin"])
    assert _roles(agent).role("telegram", CHAT, "55", is_owner=False) == "admin"
    stranger = _result("x", role=None, uid="55", chat_type="dm")
    assert _groups(agent, stranger, ["role", "telegram", "_100", "66", "admin"]) == group_ops._OWNER_ONLY_TEXT


# --- 101 Phase 5.4 ------------------------------------------------------------
def test_the_audit_is_owner_only_and_names_who_ran_what(agent):
    from core.surfaces.room_caps import RoomCaps
    RoomCaps(surfaces_db_path(agent.container.config.data_dir)).record_admin_tool(
        "telegram", CHAT, "room_moderate", "77")
    assert _groups(agent, _result("x", role="admin"), ["audit", "here"]) == group_ops._OWNER_ONLY_TEXT
    out = _groups(agent, _result("x", role="owner"), ["audit", "here"])
    assert "77" in out and "room_moderate" in out


def test_status_names_a_rooms_admins_and_bundles(agent, monkeypatch):
    import os as _os
    from core.instance import resolve_owner_user_id
    from core.status_rooms import _groups_section
    from core.surfaces import chat_policy
    from core.surfaces.group_allowlist import GroupAllowlist
    home = agent.container.config.data_dir
    GroupAllowlist(_os.path.join(home, "group_allowlist.db")).allow("telegram", CHAT)
    _roles(agent).grant("telegram", CHAT, "77", "admin", granted_by="999")
    chat_policy.set(home, resolve_owner_user_id(), "telegram", CHAT, "chat.admin_tools", "files")
    row = _groups_section("u", home).data["rooms"][0]
    assert row["admins"] == ["77"] and row["admin_bundles"] == ["files"]


# --- the second review: one truth on every surface ------------------------------
@pytest.mark.parametrize("surface,uid", [("slack", "U0ABC123"), ("discord", "12345"),
                                         ("signal", "+15550100"), ("whatsapp", "15550100"),
                                         ("signal", "12345678-1234-1234-1234-123456789abc"),
                                         ("feishu", "ou_abc1"), ("dingtalk", "staff_1"),
                                         ("dingtalk", "$:LWCP_v1:$abc+def==")])
def test_a_platform_id_writes_a_role_on_each_surface(tmp_path, surface, uid):
    c = SimpleNamespace(config=SimpleNamespace(data_dir=str(tmp_path)), get_service=lambda _n: None)
    assert "✅" in group_admin.set_role(c, surface, "c1", uid, "admin", by="owner")
    assert GroupRoles(surfaces_db_path(str(tmp_path))).role(surface, "c1", uid, is_owner=False) == "admin"


@pytest.mark.parametrize("surface,name", [("telegram", "U0ABC123"), ("telegram", "bob"), ("slack", "bob"),
                                          ("signal", "bob"), ("feishu", "bob"), ("dingtalk", "bob")])
def test_a_name_still_writes_no_role(tmp_path, surface, name):
    c = SimpleNamespace(config=SimpleNamespace(data_dir=str(tmp_path)), get_service=lambda _n: None)
    group_admin.set_role(c, surface, "c1", name, "admin", by="owner")
    assert GroupRoles(surfaces_db_path(str(tmp_path))).list(surface, "c1") == []


def test_an_admin_bundle_refuses_off_telegram_and_a_removal_passes(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner_principal")
    c = SimpleNamespace(config=SimpleNamespace(data_dir=str(tmp_path)), get_service=lambda _n: None)
    out = group_admin.set_tools(c, "owner_principal", "discord", "123", admin=True, add=True,
                                name="moderation")
    assert "Telegram only" in out and "❌" in out
    assert "Telegram only" in group_admin.show_tools(c, "owner_principal", "discord", "123")
    assert "✅" in group_admin.set_tools(c, "owner_principal", "telegram", "123", admin=True,
                                         add=True, name="moderation")
    assert "❌" not in group_admin.set_tools(c, "owner_principal", "discord", "123", admin=True,
                                             add=False, name="moderation")


@pytest.mark.parametrize("line", ["/groups tools discord 123 admin add moderation",
                                  "/groups tools nonsense 123 admin add moderation",
                                  "/groups role nonsense 123 55 admin"])
def test_a_card_refuses_what_cannot_run(line):
    from core.setting_proposals import classify
    assert classify(line).outcome != "card"
