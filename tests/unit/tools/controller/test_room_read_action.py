"""The `room_read` action — the agent can finally READ an allowlisted room.

Rob could POST to The Public Den but not read it (owner rail 2026-09-15 19:41Z:
"fix tg chat reading and deploy and restart"). The harness ALREADY captures
every allowlisted-room line into `group_ledger` (`core/surfaces/group_ledger.py`,
044 §4.1) — the room turn, the room-servicing goal and `/groups tail` all read
it — but an ordinary session had no action over it, so the agent probed dead
routes (t.me/s preview, anysite) and correctly reported "no read surface".

This action is that read surface, over the SAME store — no Telegram API is
involved (the Bot API cannot read history anyway), so the honest framing is
"the local room ledger", with its retention window stated.

⚠️ Read-only, and it never posts. Posting stays on `message` / the bound room
session. A refusal is structured (lists the allowlisted rooms), never a fake
empty read.
"""
import time

import pytest


class _Registry:
    def __init__(self):
        self.actions = {}

    def action(self, description, param_model=None, **kw):
        def deco(fn):
            self.actions[fn.__name__] = (fn, param_model, description)
            return fn
        return deco


class _Controller:
    def __init__(self, data_dir):
        self.registry = _Registry()
        self.container = type("C", (), {
            "config": type("Cfg", (), {"data_dir": str(data_dir)})()})()
        self.user_id = "rob"
        self.orchestrator = None


def _ctx():
    return type("Ctx", (), {"user_id": "rob", "role": "orchestrator",
                            "is_sub_agent": False, "session_id": "s",
                            "metadata": {}})()


def _seed_room(home, *, chat_id="-1002125904710", note="The Public Den (@thepublicden)"):
    from core.surfaces.group_allowlist import GroupAllowlist
    GroupAllowlist(str(home / "group_allowlist.db")).allow("telegram", chat_id, note)
    return chat_id


def _seed_lines(home, chat_id, rows):
    from core.surfaces.group_ledger import GroupLedger, LedgerRow
    ledger = GroupLedger(str(home / "surfaces.db"))
    base = time.time() - len(rows) * 60
    for i, (sender, text) in enumerate(rows):
        ledger.append(LedgerRow(
            surface="telegram", chat_id=chat_id, thread_id=None,
            message_id=str(i + 1), ts=base + i * 60, sender_id=str(100 + i),
            sender_name=sender, sender_is_bot=False, role_at_write="member",
            kind="text", text=text, reply_to_message_id=None, mentions_bot=False))
    return ledger


def _action(c):
    from tools.controller.room_read_action import register_room_read_action
    register_room_read_action(c)
    assert "room_read" in c.registry.actions, "action was not registered"
    fn, model, _ = c.registry.actions["room_read"]
    return fn, model


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    return tmp_path


@pytest.mark.asyncio
async def test_not_registered_when_rooms_off(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "false")
    c = _Controller(tmp_path)
    from tools.controller.room_read_action import register_room_read_action
    register_room_read_action(c)
    assert "room_read" not in c.registry.actions


@pytest.mark.asyncio
async def test_lists_rooms_when_no_room_given(home):
    chat = _seed_room(home)
    _seed_lines(home, chat, [("@a", "hello")])
    c = _Controller(home)
    fn, model = _action(c)
    res = await fn(model(), _ctx())
    out = res.extracted_content
    assert "-1002125904710" in out
    assert "The Public Den" in out
    assert "1 line" in out


@pytest.mark.asyncio
async def test_reads_lines_by_chat_id(home):
    chat = _seed_room(home)
    _seed_lines(home, chat, [
        ("@Nrmpap", "Keep doubting, let the believers keep making money"),
        ("@themontreal", "/unban"),
    ])
    c = _Controller(home)
    fn, model = _action(c)
    res = await fn(model(room="-1002125904710"), _ctx())
    out = res.extracted_content
    assert "@Nrmpap" in out and "Keep doubting" in out
    assert "@themontreal" in out and "/unban" in out
    # honest source framing — it is the local ledger, not a live history fetch
    assert "local room ledger" in out


@pytest.mark.asyncio
async def test_matches_room_by_title_fragment(home):
    chat = _seed_room(home)
    _seed_lines(home, chat, [("@a", "hi")])
    c = _Controller(home)
    fn, model = _action(c)
    res = await fn(model(room="public den"), _ctx())
    assert "hi" in res.extracted_content


@pytest.mark.asyncio
async def test_unknown_room_refuses_and_lists(home):
    chat = _seed_room(home)
    _seed_lines(home, chat, [("@a", "hi")])
    c = _Controller(home)
    fn, model = _action(c)
    res = await fn(model(room="-1009999999999"), _ctx())
    out = res.extracted_content
    assert "not an allowlisted room" in out
    assert "-1002125904710" in out, "refusal must list the real rooms"


@pytest.mark.asyncio
async def test_known_room_empty_ledger_is_honest(home):
    chat = _seed_room(home)
    c = _Controller(home)
    fn, model = _action(c)
    res = await fn(model(room=chat), _ctx())
    assert "no lines captured" in res.extracted_content


@pytest.mark.asyncio
async def test_since_minutes_filters_old_lines(home):
    chat = _seed_room(home)
    from core.surfaces.group_ledger import GroupLedger, LedgerRow
    ledger = GroupLedger(str(home / "surfaces.db"))
    now = time.time()
    ledger.append(LedgerRow(
        surface="telegram", chat_id=chat, thread_id=None, message_id="1",
        ts=now - 7200, sender_id="1", sender_name="@old", sender_is_bot=False,
        role_at_write="member", kind="text", text="two hours old",
        reply_to_message_id=None, mentions_bot=False))
    ledger.append(LedgerRow(
        surface="telegram", chat_id=chat, thread_id=None, message_id="2",
        ts=now - 60, sender_id="2", sender_name="@new", sender_is_bot=False,
        role_at_write="member", kind="text", text="one minute old",
        reply_to_message_id=None, mentions_bot=False))
    c = _Controller(home)
    fn, model = _action(c)
    res = await fn(model(room=chat, since_minutes=30), _ctx())
    out = res.extracted_content
    assert "one minute old" in out
    assert "two hours old" not in out
