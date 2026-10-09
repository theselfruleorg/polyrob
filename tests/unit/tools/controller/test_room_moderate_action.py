"""The `room_moderate` action — the agent can mute/ban/delete in its rooms.

Owner rail 2026-10-05: Rob told the owner "I can't ban — I have no moderation
tool" while a spammer posted in The Public Den. The adapter existed (the owner's
own /mute /ban use `room_moderator`); the agent had no seat on it.
"""
import time
from types import SimpleNamespace

import pytest

from surfaces.telegram.moderation import ModResult
from surfaces.telegram.room_moderator import RoomModerator


class _Registry:
    def __init__(self):
        self.actions = {}

    def action(self, description, param_model=None, **kw):
        def deco(fn):
            self.actions[fn.__name__] = (fn, param_model, description)
            return fn
        return deco


class _Bot:
    def __init__(self, *, rights=("can_restrict_members", "can_delete_messages"),
                 statuses=None):
        self.rights = set(rights)
        self.statuses = statuses or {}
        self.calls = []

    async def get_me(self):
        return type("Me", (), {"id": 999})()

    async def get_chat_member(self, chat_id, user_id):
        if user_id == 999:
            m = type("M", (), {"status": "administrator"})()
            for r in ("can_restrict_members", "can_change_info",
                      "can_delete_messages", "can_pin_messages"):
                setattr(m, r, r in self.rights)
            return m
        return type("M", (), {"status": self.statuses.get(user_id, "member")})()

    async def ban_chat_member(self, **kw):
        self.calls.append(("ban", kw))

    async def unban_chat_member(self, **kw):
        self.calls.append(("unban", kw))

    async def restrict_chat_member(self, **kw):
        self.calls.append(("restrict", kw))

    async def delete_message(self, **kw):
        self.calls.append(("delete", kw))


class _Container:
    def __init__(self, data_dir, bot):
        self.config = type("Cfg", (), {"data_dir": str(data_dir)})()
        self._svc = {"room_moderator": RoomModerator(bot=bot)} if bot else {}

    def get_service(self, name):
        return self._svc.get(name)


class _Controller:
    def __init__(self, data_dir, bot):
        self.registry = _Registry()
        self.container = _Container(data_dir, bot)


CHAT = "-1001000000002"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL", "true")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "local")
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    import core.surfaces.owner_target as ot
    monkeypatch.setattr(ot, "is_owner_address",
                        lambda surface, addr: str(addr) == "1")
    from core.surfaces.group_allowlist import GroupAllowlist
    GroupAllowlist(str(tmp_path / "group_allowlist.db")).allow(
        "telegram", CHAT, "The Public Den")
    from core.surfaces.group_ledger import GroupLedger, LedgerRow
    ledger = GroupLedger(str(tmp_path / "surfaces.db"))
    now = time.time()
    for mid, (uid, name, text) in enumerate([
            ("1", "@owner", "welcome"),
            ("555", "@Mr_Zain_kols", "buy my promo package"),
            ("555", "@Mr_Zain_kols", "DM me for KOL deals"),
            ("777", "@Nrmpap", "gm")], start=1):
        ledger.append(LedgerRow(
            surface="telegram", chat_id=CHAT, thread_id=None, message_id=str(mid),
            ts=now - 600 + mid, sender_id=uid, sender_name=name,
            sender_is_bot=False, role_at_write="member", kind="text", text=text,
            reply_to_message_id=None, mentions_bot=False))
    return tmp_path


def _fn(c):
    from tools.controller.room_moderate_action import register_room_moderate_action
    register_room_moderate_action(c)
    fn, model, _ = c.registry.actions["room_moderate"]
    async def owner_call(params, execution_context=None):
        ctx = execution_context or SimpleNamespace(
            user_id="local", role="orchestrator", is_sub_agent=False,
            metadata={}, session_id="owner-room-review")
        return await fn(params, execution_context=ctx)
    return owner_call, model


@pytest.mark.asyncio
async def test_ban_by_name_with_cleanup(home):
    bot = _Bot()
    fn, M = _fn(_Controller(home, bot))
    res = await fn(M(room="Den", action="ban", target="mr_zain_kols",
                     delete_messages=True, reason="promo spam"))
    out = res.extracted_content
    assert "banned for 30d" in out, out
    bans = [kw for op, kw in bot.calls if op == "ban"]
    assert bans and bans[0]["user_id"] == 555
    deleted = sorted(kw["message_id"] for op, kw in bot.calls if op == "delete")
    assert deleted == [2, 3]


@pytest.mark.asyncio
async def test_mute_default_one_hour(home):
    bot = _Bot()
    fn, M = _fn(_Controller(home, bot))
    out = (await fn(M(room=CHAT, action="mute", target="555"))).extracted_content
    assert "muted for 1h" in out, out
    assert [op for op, _ in bot.calls] == ["restrict"]


@pytest.mark.asyncio
async def test_owner_is_never_a_target(home):
    bot = _Bot()
    fn, M = _fn(_Controller(home, bot))
    out = (await fn(M(room=CHAT, action="ban", target="@owner"))).extracted_content
    assert "refused" in out and not bot.calls


@pytest.mark.asyncio
async def test_live_admin_is_never_a_target(home):
    bot = _Bot(statuses={777: "administrator"})
    fn, M = _fn(_Controller(home, bot))
    out = (await fn(M(room=CHAT, action="mute", target="777"))).extracted_content
    assert "refused" in out and not bot.calls


@pytest.mark.asyncio
async def test_delete_by_id_refuses_owner_line(home):
    bot = _Bot()
    fn, M = _fn(_Controller(home, bot))
    out = (await fn(M(room=CHAT, action="delete",
                      message_ids=["1"]))).extracted_content
    assert "refused" in out and not bot.calls


@pytest.mark.asyncio
async def test_explicit_ids_checked_even_with_a_target(home):
    bot = _Bot()
    fn, M = _fn(_Controller(home, bot))
    out = (await fn(M(room=CHAT, action="delete", target="555",
                      message_ids=["1"]))).extracted_content
    assert "not sent by" in out and not bot.calls
    # Checked BEFORE the ban: a bad id never hides a ban that already landed.
    out = (await fn(M(room=CHAT, action="ban", target="555",
                      delete_messages=True, message_ids=["1"]))).extracted_content
    assert "nothing was done" in out and not bot.calls, out


@pytest.mark.asyncio
async def test_unseen_uid_only_for_lifting(home):
    bot = _Bot()
    fn, M = _fn(_Controller(home, bot))
    out = (await fn(M(room=CHAT, action="ban", target="31337"))).extracted_content
    assert "no member named" in out and not bot.calls
    out = (await fn(M(room=CHAT, action="unban", target="31337"))).extracted_content
    assert "unbanned" in out, out


@pytest.mark.asyncio
async def test_rights_fault_is_not_called_missing_permission(home):
    bot = _Bot()
    c = _Controller(home, bot)
    async def boom(*a):
        raise RuntimeError("loop")
    c.container._svc["room_moderator"].rights_async = boom
    fn, M = _fn(c)
    out = (await fn(M(room=CHAT, action="ban", target="555"))).extracted_content
    assert "could not read my own permissions" in out and not bot.calls


@pytest.mark.asyncio
async def test_spam_bot_is_a_target(home):
    from core.surfaces.group_ledger import GroupLedger, LedgerRow
    GroupLedger(str(home / "surfaces.db")).append(LedgerRow(
        surface="telegram", chat_id=CHAT, thread_id=None, message_id="9",
        ts=time.time(), sender_id="4242", sender_name="@promo_bot",
        sender_is_bot=True, role_at_write="member", kind="text", text="airdrop",
        reply_to_message_id=None, mentions_bot=False))
    bot = _Bot()
    fn, M = _fn(_Controller(home, bot))
    out = (await fn(M(room=CHAT, action="ban", target="@promo_bot"))).extracted_content
    assert "banned" in out, out


@pytest.mark.asyncio
async def test_needs_the_telegram_right(home):
    bot = _Bot(rights=())
    fn, M = _fn(_Controller(home, bot))
    out = (await fn(M(room=CHAT, action="ban", target="555"))).extracted_content
    assert "can_restrict_members" in out and not bot.calls


@pytest.mark.asyncio
async def test_unknown_room_and_target_refuse(home):
    bot = _Bot()
    fn, M = _fn(_Controller(home, bot))
    out = (await fn(M(room="-100999", action="ban", target="555"))).extracted_content
    assert "not an allowlisted room" in out
    out = (await fn(M(room=CHAT, action="ban", target="@nobody"))).extracted_content
    assert "no member named" in out
    assert not bot.calls


@pytest.mark.asyncio
async def test_no_surface_running(home):
    fn, M = _fn(_Controller(home, None))
    out = (await fn(M(room=CHAT, action="ban", target="555"))).extracted_content
    assert "no chat connection" in out


def test_room_and_taint_gated():
    from core.surfaces.room_policy import is_room_denied_call
    assert is_room_denied_call("room_moderate", None)
    from agents.task.agent.core.correspondent_gate import is_high_impact_call
    assert is_high_impact_call("room_moderate", None)


def test_room_read_shows_ids(home):
    from core.surfaces.group_ledger import GroupLedger
    from tools.controller.room_read_action import _render_line
    rows = GroupLedger(str(home / "surfaces.db")).tail("telegram", CHAT)
    assert "{uid=555 msg=2}" in _render_line(rows[1])


@pytest.mark.asyncio
async def test_not_registered_when_rooms_off(tmp_path, monkeypatch):
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "false")
    c = _Controller(tmp_path, _Bot())
    from tools.controller.room_moderate_action import register_room_moderate_action
    register_room_moderate_action(c)
    assert "room_moderate" not in c.registry.actions


# --- a deleted line is handled once (prod 2026-10-06) ------------------------ #
# 01:00 deleted msg 3593 and warned; 02:01 read the same row, failed to delete it
# ("message to delete not found") and warned again.


@pytest.mark.asyncio
async def test_a_deleted_line_is_marked_and_not_deleted_twice(home):
    from core.surfaces.group_ledger import GroupLedger
    bot = _Bot()
    fn, M = _fn(_Controller(home, bot))
    await fn(M(room="Den", action="delete", target="555"))
    kinds = {r.message_id: r.kind for r in GroupLedger(str(home / "surfaces.db")).tail("telegram", CHAT)}
    assert kinds["2"] == kinds["3"] == "deleted"
    bot.calls.clear()
    out = (await fn(M(room="Den", action="delete", target="555"))).extracted_content
    assert "nothing to delete" in out, out
    assert not [op for op, _ in bot.calls if op == "delete"]


@pytest.mark.asyncio
async def test_not_found_on_delete_still_marks_the_line_handled(home):
    from core.surfaces.group_ledger import GroupLedger

    class _GoneBot(_Bot):
        async def delete_message(self, **kw):
            raise RuntimeError("Bad Request: message to delete not found")
    fn, M = _fn(_Controller(home, _GoneBot()))
    await fn(M(room="Den", action="delete", message_ids=["2"]))
    kinds = {r.message_id: r.kind for r in GroupLedger(str(home / "surfaces.db")).tail("telegram", CHAT)}
    assert kinds["2"] == "deleted"


def test_room_read_flags_a_deleted_line():
    from types import SimpleNamespace
    from tools.controller.room_read_action import _render_line
    row = SimpleNamespace(ts=time.time(), sender_is_bot=False, kind="deleted", media_path=None,
                          mentions_bot=False, text="(deleted by you — moderation)",
                          sender_name="@p", sender_id="876", role_at_write="member",
                          message_id="3593")
    assert "already handled" in _render_line(row)


# --- CHAT-5: whose decision a moderation is ---------------------------------

def _autonomous(monkeypatch, sid, job_id, task, agent_authored=False):
    from agents.task.goals import autonomy_marker as am
    am.mark_autonomous(sid, cron_job_id=job_id)
    if not agent_authored:
        am.note_owner_job(job_id, task)
    return SimpleNamespace(user_id="local", role="orchestrator", is_sub_agent=False,
                           metadata={}, session_id=sid)


@pytest.mark.asyncio
async def test_standing_owner_job_for_this_room_may_moderate(home, monkeypatch):
    """The live DEN ENGAGEMENT cron: an owner-authored job naming the room."""
    bot = _Bot()
    fn, M = _fn(_Controller(home, bot))
    ctx = _autonomous(monkeypatch, "cron-den-1", "job-den",
                      f"DEN ENGAGEMENT — moderation in telegram:{CHAT}")
    out = (await fn(M(room=CHAT, action="mute", target="555"),
                    execution_context=ctx)).extracted_content
    assert "muted for 1h" in out, out


@pytest.mark.asyncio
async def test_agent_authored_or_other_room_job_may_not_moderate(home, monkeypatch):
    bot = _Bot()
    fn, M = _fn(_Controller(home, bot))
    ctx = _autonomous(monkeypatch, "cron-x-1", "job-agent", f"moderate {CHAT}",
                      agent_authored=True)
    out = (await fn(M(room=CHAT, action="ban", target="555"),
                    execution_context=ctx)).extracted_content
    assert "standing owner-authored job" in out and not bot.calls
    ctx = _autonomous(monkeypatch, "cron-y-1", "job-other", "moderate -100999")
    out = (await fn(M(room=CHAT, action="ban", target="555"),
                    execution_context=ctx)).extracted_content
    assert "standing owner-authored job" in out and not bot.calls


@pytest.mark.asyncio
async def test_tainted_owner_turn_needs_the_owners_tap(home, monkeypatch):
    """A member's line read through room_read must not steer a ban."""
    bot = _Bot()
    fn, M = _fn(_Controller(home, bot))
    asked = []

    class _Queue:
        decides_as_owner = True

        def __init__(self, answer):
            self.answer = answer

        async def request(self, name, params, ctx):
            asked.append((name, params.get("action")))
            return self.answer

    import tools.controller.approval as approval
    answer = {"v": False}
    monkeypatch.setattr(approval, "get_approval_provider_or_deny",
                        lambda name, **k: _Queue(answer["v"]))
    ctx = SimpleNamespace(user_id="local", role="orchestrator", is_sub_agent=False,
                          metadata={"untrusted_read": True}, session_id="owner-room-review")
    out = (await fn(M(room=CHAT, action="ban", target="555"),
                    execution_context=ctx)).extracted_content
    assert "not approved" in out and not bot.calls
    answer["v"] = True
    out = (await fn(M(room=CHAT, action="ban", target="555"),
                    execution_context=ctx)).extracted_content
    assert "banned" in out
    assert asked == [("room_moderate", "ban"), ("room_moderate", "ban")]


@pytest.mark.asyncio
async def test_standing_owner_job_may_name_the_room_by_its_title(home, monkeypatch):
    """The owner writes "moderate The Public Den", not the chat id. The hourly
    den-engagement run reads room text (tainted) and then bans a spammer."""
    import core.surfaces.room_label as rl
    monkeypatch.setattr(rl, "room_name", lambda row, pol=None: "The Public Den")
    bot = _Bot()
    fn, M = _fn(_Controller(home, bot))
    ctx = _autonomous(monkeypatch, "cron-den-2", "job-den-2",
                      "Hourly: engage and moderate the public den; ban spam.")
    ctx.metadata["untrusted_read"] = True
    out = (await fn(M(room=CHAT, action="ban", target="555"),
                    execution_context=ctx)).extracted_content
    assert "banned" in out, out
    # An agent-authored job naming the same title still may not.
    ctx = _autonomous(monkeypatch, "cron-den-3", "job-den-3",
                      "moderate The Public Den", agent_authored=True)
    bot.calls.clear()
    out = (await fn(M(room=CHAT, action="ban", target="555"),
                    execution_context=ctx)).extracted_content
    assert "standing owner-authored job" in out and not bot.calls
