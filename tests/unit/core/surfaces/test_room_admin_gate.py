"""101: every branch of the room seat's decision, driven through the real gate hook."""
import os
import types

import pytest

from core.runtime_paths import surfaces_db_path
from core.surfaces import chat_policy
from core.surfaces.group_allowlist import GroupAllowlist
from core.surfaces.group_roles import GroupRoles
from core.surfaces.room_grants import seat_decision
from core.surfaces.room_seat import RoomSeat, make_admin_post_hook, note_admin_line
from core.surfaces.room_policy import make_room_gate_hook

CHAT = "-100"
OTHER = "-200"
ROOM = f"telegram:{CHAT}"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "owner_principal")
    monkeypatch.setenv("POLYROB_OWNER_TELEGRAM_ID", "999")
    GroupAllowlist(os.path.join(tmp_path, "group_allowlist.db")).allow("telegram", CHAT)
    GroupRoles(surfaces_db_path(str(tmp_path))).grant("telegram", CHAT, "77", "admin", granted_by="999")
    return str(tmp_path)


def _grant(home, *bundles):
    from core.instance import resolve_owner_user_id
    ok, msg = chat_policy.set(home, resolve_owner_user_id(), "telegram", CHAT,
                              "chat.admin_tools", ",".join(bundles))
    assert ok, msg


def _orch(home, *, stamped=True, line="", author=None, tainted=False):
    orch = types.SimpleNamespace(
        container=types.SimpleNamespace(config=types.SimpleNamespace(data_dir=home),
                                        get_service=lambda _n: None),
        _public_session=True,
        _chat_session_key=f"agent:main:telegram:supergroup:{CHAT}",
        _room_admin=({"surface": "telegram", "chat_id": CHAT, "uid": "77",
                      "user_id": "u_admin"} if stamped else None))
    note_admin_line(orch, line, author)
    orch._room_admin_tainted = tainted
    return orch


def _seat(orch, *, files=True, browser=True):
    return RoomSeat(orch, files_ok=lambda: files, browser_ok=lambda: browser)


def _hook(seat, tools=None):
    tools = tools or {}
    return make_room_gate_hook(lambda: True, resolve_tool=lambda a: tools.get(a), seat=seat)


TOOLS = {"read_file": "filesystem", "write_file": "filesystem", "http_request": "http_request",
         "go_to_url": "browser", "go_to_url_mgr": "browser_manager"}


def test_a_private_session_is_not_asked(home):
    hook = make_room_gate_hook(lambda: False, resolve_tool=lambda a: "filesystem",
                               seat=_seat(_orch(home, stamped=False)))
    assert hook("read_file", {}, None) is None


def test_no_stamp_refuses_every_bundle_tool(home):
    """R6: a session that lost its stamp keeps its loaded tools. They refuse."""
    _grant(home, "files", "http", "browser", "moderation")
    hook = _hook(_seat(_orch(home, stamped=False)), TOOLS)
    for action in ("read_file", "write_file", "http_request", "go_to_url", "go_to_url_mgr"):
        assert "refused" in (hook(action, {}, None) or ""), action
    assert hook("room_moderate", {"room": ROOM}, None)


def test_a_granted_tool_runs(home):
    _grant(home, "files", "http", "browser")
    hook = _hook(_seat(_orch(home, line="x")), TOOLS)
    for action in ("read_file", "http_request", "go_to_url", "go_to_url_mgr"):
        assert hook(action, {}, None) is None, action


def test_an_ungranted_bundle_refuses(home):
    _grant(home, "files")
    hook = _hook(_seat(_orch(home)), TOOLS)
    assert hook("read_file", {}, None) is None
    assert "not granted" in hook("http_request", {}, None)


def test_removing_a_bundle_refuses_at_the_next_call_read_file_included(home):
    _grant(home, "files")
    hook = _hook(_seat(_orch(home)), TOOLS)
    assert hook("read_file", {}, None) is None
    _grant(home, "http")
    assert "not granted" in hook("read_file", {}, None)


def test_a_demoted_admin_refuses_in_the_same_turn(home):
    _grant(home, "files")
    hook = _hook(_seat(_orch(home)), TOOLS)
    assert hook("read_file", {}, None) is None
    GroupRoles(surfaces_db_path(home)).revoke("telegram", CHAT, "77")
    assert "no longer holds" in hook("read_file", {}, None)


def test_the_owner_keeps_the_role_with_no_row(home, monkeypatch):
    _grant(home, "files")
    orch = _orch(home)
    orch._room_admin = dict(orch._room_admin, uid="999", user_id="owner_principal")
    assert _hook(_seat(orch), TOOLS)("read_file", {}, None) is None


def test_a_left_room_refuses(home):
    _grant(home, "files")
    GroupAllowlist(os.path.join(home, "group_allowlist.db")).revoke("telegram", CHAT)
    assert "no longer allowed" in _hook(_seat(_orch(home)), TOOLS)("read_file", {}, None)


def test_an_unreadable_role_store_refuses(home, monkeypatch):
    _grant(home, "files")
    seat = _seat(_orch(home))
    monkeypatch.setattr(RoomSeat, "role", lambda self: (_ for _ in ()).throw(RuntimeError("x")))
    assert "could not be read" in _hook(seat, TOOLS)("read_file", {}, None)


def test_a_store_read_fault_refuses(home, monkeypatch):
    _grant(home, "files")
    seat = _seat(_orch(home))
    import core.surfaces.group_roles as gr
    monkeypatch.setattr(gr, "execute_retry",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("locked")))
    assert _hook(seat, TOOLS)("read_file", {}, None)


@pytest.mark.parametrize("action,tool", [
    ("defi_trade_swap", "defi_trade"), ("wallet_sign_challenge", "wallet_sign"),
    ("load_tool", None), ("message", None), ("kb_search", "knowledge"), ("memory", None),
    ("propose_setting", None), ("owner_ask", None), ("email_send", "email"),
    ("goal_create", "goal"), ("run_code", "code_execution"),
])
def test_money_and_owner_verbs_refuse_with_every_bundle_granted(home, action, tool):
    _grant(home, "files", "http", "browser", "moderation")
    hook = _hook(_seat(_orch(home, line="x")), {action: tool})
    assert hook(action, {}, None), action


@pytest.mark.parametrize("room", [OTHER, f"telegram:{OTHER}", "lobby", "", None, "-1"])
def test_moderation_names_this_room_exactly(home, room):
    _grant(home, "moderation")
    hook = _hook(_seat(_orch(home, line="ban 55")))
    for action in ("room_read", "room_moderate"):
        assert "name this room" in hook(action, {"room": room, "target": "55"}, None)


def test_room_read_of_this_room_runs(home):
    _grant(home, "moderation")
    hook = _hook(_seat(_orch(home)))
    assert hook("room_read", {"room": ROOM}, None) is None
    assert hook("room_read", {"room": CHAT}, None) is None


def test_outside_text_stops_moderation_until_the_next_line(home):
    _grant(home, "moderation", "http")
    orch = _orch(home, line="/admin ban 55")
    seat = _seat(orch)
    hook = _hook(seat, TOOLS)
    args = {"room": ROOM, "action": "ban", "target": "55"}
    assert hook("room_moderate", args, None) is None
    post = make_admin_post_hook(seat, lambda: None)
    post("web_fetch", {}, None, None)
    assert "outside text" in hook("room_moderate", args, None)
    note_admin_line(orch, "/admin ban 55", None)
    assert hook("room_moderate", args, None) is None


@pytest.mark.parametrize("action", ["room_read", "http_request", "web_fetch", "read_file"])
def test_each_outside_read_sets_the_taint(home, action):
    orch = _orch(home)
    orch.controller = types.SimpleNamespace(
        get_action_details=lambda n: types.SimpleNamespace(tool=TOOLS.get(n)))
    make_admin_post_hook(_seat(orch), lambda: None)(action, {}, None, None)
    assert orch._room_admin_tainted is True


def test_a_restored_session_reads_as_tainted_until_a_line(home):
    _grant(home, "moderation")
    orch = _orch(home, line="", tainted=True)
    assert "outside text" in _hook(_seat(orch))(
        "room_moderate", {"room": ROOM, "action": "ban", "target": "55"}, None)


def test_a_planted_ban_needs_the_admins_own_line(home):
    """R5: `/admin ok` clears the taint, and a planted instruction is in the history."""
    _grant(home, "moderation")
    hook = _hook(_seat(_orch(home, line="ok")))
    out = hook("room_moderate", {"room": ROOM, "action": "ban", "target": "@victim"}, None)
    assert "not named" in out
    assert hook("room_moderate", {"room": ROOM, "action": "delete", "message_ids": ["9"]}, None)
    assert hook("room_moderate", {"room": ROOM, "action": "delete"}, None)


def test_a_partial_word_is_not_a_name(home):
    _grant(home, "moderation")
    hook = _hook(_seat(_orch(home, line="ban 5512 and bobby")))
    assert hook("room_moderate", {"room": ROOM, "action": "ban", "target": "55"}, None)
    assert hook("room_moderate", {"room": ROOM, "action": "ban", "target": "bob"}, None)
    assert hook("room_moderate", {"room": ROOM, "action": "ban", "target": "5512"}, None) is None
    assert hook("room_moderate", {"room": ROOM, "action": "ban", "target": "@BOBBY"}, None) is None


def test_a_reply_anchored_turn_moderates_only_the_author(home):
    _grant(home, "moderation")
    hook = _hook(_seat(_orch(home, line="mute this", author={"uid": "55", "name": "Sam"})))
    base = {"room": ROOM, "action": "mute"}
    assert hook("room_moderate", dict(base, target="55"), None) is None
    assert "only that person" in hook("room_moderate", dict(base, target="66"), None)


@pytest.mark.parametrize("target", ["@sam", "sam", "@bob"])
def test_a_reply_pin_reads_the_uid_never_a_name(home, target):
    """R16: a member's first name is self-chosen, so it could be another member's handle."""
    _grant(home, "moderation")
    hook = _hook(_seat(_orch(home, line="mute this", author={"uid": "55", "name": target})))
    out = hook("room_moderate", {"room": ROOM, "action": "mute", "target": target}, None)
    assert "only that person" in out and 'target="55"' in out


@pytest.mark.parametrize("line,action,kw,ok", [
    ("is bob spamming?", "ban", {"target": "bob"}, False),
    ("should we look at bob", "delete", {"target": "bob"}, False),
    ("ban bob", "ban", {"target": "bob"}, True),
    ("kick bob now", "ban", {"target": "bob"}, True),
    ("remove bob's spam", "delete", {"target": "bob"}, True),
    ("mute bob", "mute", {"target": "bob", "delete_messages": True}, False),
    ("mute bob and delete his lines", "mute", {"target": "bob", "delete_messages": True}, True),
    ("unban bob", "ban", {"target": "bob"}, False),
])
def test_the_admins_line_asks_for_the_verb(home, line, action, kw, ok):
    """R17: naming a person is not asking to ban them."""
    _grant(home, "moderation")
    out = _hook(_seat(_orch(home, line=line)))("room_moderate", dict(room=ROOM, action=action, **kw), None)
    assert (out is None) is ok, out


def test_a_reply_turn_needs_the_verb_too(home):
    _grant(home, "moderation")
    hook = _hook(_seat(_orch(home, line="what is this?", author={"uid": "55", "name": "Sam"})))
    assert "does not ask" in hook("room_moderate", {"room": ROOM, "action": "ban", "target": "55"}, None)


def test_a_reply_or_a_file_starts_the_turn_tainted(home):
    """R19, and R18: the quoted text cannot choose what leaves this box."""
    _grant(home, "http", "moderation")
    orch = _orch(home)
    note_admin_line(orch, "mute this", {"uid": "55", "name": "Sam"})
    assert orch._room_admin_tainted is True
    hook = _hook(_seat(orch), TOOLS)
    assert "sends no request" in hook("http_request", {}, None)
    assert hook("room_moderate", {"room": ROOM, "action": "mute", "target": "55"}, None) is None
    note_admin_line(orch, "post x", None, True)
    assert orch._room_admin_tainted is True
    note_admin_line(orch, "post x", None)
    assert hook("http_request", {}, None) is None


def _browser_orch(cdp=None, wss=None, chrome=None, manager=True):
    live = types.SimpleNamespace(browser_config=types.SimpleNamespace(chrome_instance_path=chrome))
    mgr = types.SimpleNamespace(browser_config=types.SimpleNamespace(cdp_url=cdp, wss_url=wss),
                                browser=live) if manager else None
    return types.SimpleNamespace(container=types.SimpleNamespace(get_service=lambda _n: mgr))


@pytest.mark.parametrize("kw,ok", [
    ({}, False), ({"manager": False}, False), ({"cdp": "ws://r"}, True), ({"wss": "ws://r"}, True),
    ({"cdp": "ws://r", "chrome": "/Chrome"}, False),
])
def test_only_a_remote_browser_runs_in_a_room(kw, ok, monkeypatch):
    """R20: the real probe. A local browser reaches this host's network by redirect."""
    from agents.task.agent.core.room_admin_wiring import _browser_ok
    monkeypatch.delenv("BROWSER_ALLOW_PRIVATE_URLS", raising=False)
    assert _browser_ok(_browser_orch(**kw)) is ok


def test_the_private_url_flag_refuses_a_remote_browser_too(monkeypatch):
    from agents.task.agent.core.room_admin_wiring import _browser_ok
    monkeypatch.setenv("BROWSER_ALLOW_PRIVATE_URLS", "true")
    assert _browser_ok(_browser_orch(cdp="ws://r")) is False


def test_a_reply_anchored_delete_cannot_skip_the_target(home):
    """R4: `delete` with message_ids and no target would reach anyone."""
    _grant(home, "moderation")
    hook = _hook(_seat(_orch(home, line="delete", author={"uid": "55", "name": "Sam"})))
    out = hook("room_moderate", {"room": ROOM, "action": "delete", "message_ids": ["9"]}, None)
    assert "only that person" in out


def test_files_and_browser_pins(home):
    _grant(home, "files", "browser")
    assert "shared folder" in _hook(_seat(_orch(home), files=False), TOOLS)("read_file", {}, None)
    assert "stays off" in _hook(_seat(_orch(home), browser=False), TOOLS)("go_to_url", {}, None)


def test_the_audit_records_a_bundle_call_with_no_params(home):
    class Caps:
        rows = []

        def record_admin_tool(self, surface, chat, action, uid):
            self.rows.append((surface, chat, action, uid))
    orch = _orch(home)
    post = make_admin_post_hook(_seat(orch), lambda: Caps())
    post("room_read", {"room": "SECRET"}, None, None)
    post("send_message", {"text": "x"}, None, None)
    assert Caps.rows == [("telegram", CHAT, "room_read", "77")]


def test_a_revoked_member_grant_refuses(home, monkeypatch):
    from core.instance import resolve_owner_user_id
    import core.tool_capabilities as tc
    monkeypatch.setitem(tc.TOOL_CAPABILITIES, "safe_read", tc.ToolRow(room_grantable=True))
    seat = _seat(_orch(home, stamped=False))
    tools = {"safe_read_go": "safe_read"}
    hook = _hook(seat, tools)
    assert "removed" in hook("safe_read_go", {}, None)
    chat_policy.set(home, resolve_owner_user_id(), "telegram", CHAT, "chat.room_tools", "safe_read")
    assert hook("safe_read_go", {}, None) is None
    chat_policy.unset(home, resolve_owner_user_id(), "telegram", CHAT, "chat.room_tools")
    assert "removed" in hook("safe_read_go", {}, None)


def test_a_hook_with_no_seat_is_unchanged():
    assert seat_decision(None, "read_file", "filesystem", {}) is None


# --- the second review ----------------------------------------------------------
def test_web_fetch_refuses_after_outside_text_in_a_stamped_session_only(home):
    assert seat_decision(_seat(_orch(home, tainted=False)), "web_fetch", "web_fetch", {}) is None
    allow, why = seat_decision(_seat(_orch(home, tainted=True)), "web_fetch", "web_fetch",
                               {"url": "https://evil.example/?d=log"})
    assert allow is False and "outside text" in why
    # `/admin compare <a> and <b>` reads both, and a longer URL built on a named page does not pass.
    orch = _orch(home, tainted=True, line="compare https://a.example/x and https://b.example/")
    assert seat_decision(_seat(orch), "web_fetch", "web_fetch", {"url": "https://b.example/"}) is None
    allow, _ = seat_decision(_seat(orch), "web_fetch", "web_fetch",
                             {"url": "https://a.example/x?d=log"})
    assert allow is False
    # An ordinary room session has no stamp, so the member rule is unchanged.
    assert seat_decision(_seat(_orch(home, stamped=False, tainted=True)),
                         "web_fetch", "web_fetch", {}) is None


def test_an_unreadable_overlay_refuses_and_says_so(home):
    from core.instance import resolve_owner_user_id
    _grant(home, "files")
    path = chat_policy.chat_preferences_path(home, resolve_owner_user_id(), "telegram", CHAT)
    path.write_text("not = [valid")
    chat_policy._CACHE.clear()
    allow, why = seat_decision(_seat(_orch(home)), "read_file", "filesystem", {})
    assert allow is False and "could not be read" in why


def test_a_line_during_a_run_only_ever_adds_taint(home):
    orch = _orch(home, line="old", tainted=True)
    note_admin_line(orch, "new", {"uid": "9"}, mid_run=True)
    assert orch._room_admin_line == "old" and orch._room_admin_tainted is True
    assert orch._room_admin_reply_author is None
    orch._room_admin_tainted = False
    note_admin_line(orch, "new", None, True, mid_run=True)
    assert orch._room_admin_tainted is True and orch._room_admin_line == "old"
