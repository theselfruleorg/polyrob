"""044 final-review room gates on the Telegram seat: I2, I7, I10.

Three separate holes, all on the same surface:

* I2 — a NON-DM chat is governed by the group model, which lives downstream in
  `route_inbound`. So every line from every group the bot had ever been added to
  reached `process_update` FIRST and paid for unauthenticated work on the way: a
  voice note was DOWNLOADED and transcribed, and a user-directory row was written
  for the sender.
* I7 — every FAILED room turn sent the owner a raw DM, uncapped.
* I10 — the owner's money/host/control verbs EXECUTED from inside a room. The
  reply was redirected to his DM, but the action ran.
"""
import time
import types

import pytest

from core.surfaces.dispatcher import RouteKind
from surfaces.telegram.harness import TelegramHarness
from tests.unit.surfaces.telegram.test_harness_progress import (
    FakeBot, FakeContainer, FakeDedup, FakeUD, _drain,
)

_ROOM_CHAT_ID = "-100555"
_ROOM_KEY = "agent:main:telegram:group:-100555"


def _room_update(uid=1, text="hello room", chat_id=_ROOM_CHAT_ID):
    return {"update_id": uid, "message": {
        "message_id": 4, "date": time.time(),
        "chat": {"id": int(chat_id), "type": "supergroup", "title": "Den"},
        "from": {"id": 9911, "username": "bob"}, "text": text}}


def _allow(tmp_path, chat_id=_ROOM_CHAT_ID):
    import os
    from core.surfaces.group_allowlist import GroupAllowlist
    GroupAllowlist(os.path.join(str(tmp_path), "group_allowlist.db")).allow(
        "telegram", chat_id)


# ---------------------------------------------------------------------------
# I2 — no unauthenticated work before the room allowlist
# ---------------------------------------------------------------------------

@pytest.fixture
def probe(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ALLOWED_TELEGRAM_USER_IDS", raising=False)
    calls = []

    async def fake_process_update(*a, **k):
        calls.append((a, k))
        return None

    import surfaces.telegram.inbound as inbound_mod
    monkeypatch.setattr(inbound_mod, "process_update", fake_process_update)
    return calls


@pytest.mark.asyncio
async def test_an_unlisted_room_never_reaches_process_update(probe, tmp_path):
    harness = TelegramHarness(FakeBot(), FakeContainer(), object(),
                              webhook_base=None, dedup=FakeDedup(),
                              user_directory=FakeUD())
    out = await harness.handle_update(_room_update())
    await _drain()
    assert out == {"ok": True}
    assert probe == [], "an unlisted room paid for transcription/user-directory work"


@pytest.mark.asyncio
async def test_an_allowed_room_still_reaches_process_update(probe, tmp_path):
    _allow(tmp_path)
    harness = TelegramHarness(FakeBot(), FakeContainer(), object(),
                              webhook_base=None, dedup=FakeDedup(),
                              user_directory=FakeUD())
    await harness.handle_update(_room_update())
    await _drain()
    assert len(probe) == 1


@pytest.mark.asyncio
async def test_a_dm_is_untouched_by_the_room_pre_check(probe, tmp_path, monkeypatch):
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "777")
    dm = {"update_id": 2, "message": {
        "message_id": 5, "date": time.time(),
        "chat": {"id": 777, "type": "private"},
        "from": {"id": 777}, "text": "hi"}}
    harness = TelegramHarness(FakeBot(), FakeContainer(), object(),
                              webhook_base=None, dedup=FakeDedup(),
                              user_directory=FakeUD())
    await harness.handle_update(dm)
    await _drain()
    assert len(probe) == 1


@pytest.mark.asyncio
async def test_an_unlisted_room_voice_note_is_never_downloaded(probe, tmp_path, monkeypatch):
    """The expensive half of I2, named: a voice note from an unlisted channel was
    fetched from Telegram and run through Whisper before anything checked whether
    the agent was allowed in that channel at all."""
    transcribed = []
    voice = _room_update(uid=3)
    voice["message"].pop("text")
    voice["message"]["voice"] = {"file_id": "f-1", "duration": 3}

    harness = TelegramHarness(FakeBot(), FakeContainer(), object(),
                              webhook_base=None, dedup=FakeDedup(),
                              user_directory=FakeUD())
    harness._transcribe_voice = lambda *a, **k: transcribed.append(a) or "text"
    await harness.handle_update(voice)
    await _drain()
    assert transcribed == [] and probe == []


# ---------------------------------------------------------------------------
# I7 — the owner-DM error breadcrumb is bounded
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_owner_breadcrumb_is_capped_per_room(monkeypatch):
    """A room in a crash loop (or two bots mentioning each other) turned into one
    raw owner DM per failure, with no ceiling. Same 30-minute per-(surface+chat)
    window the LLM-outage notice has had since proposal 015."""
    from core.surfaces.llm_outage_notice import reset_llm_outage_notice_state
    reset_llm_outage_notice_state()
    monkeypatch.setattr("core.instance.resolve_owner_telegram_id", lambda: "777")

    bot = FakeBot()
    harness = TelegramHarness(bot, FakeContainer(), object(), webhook_base=None,
                              dedup=FakeDedup(), user_directory=FakeUD())
    for _ in range(5):
        await harness._send_owner_only(_ROOM_KEY, _ROOM_CHAT_ID, "⚠️ boom")

    sends = [c for c in bot.calls if c[0] == "send"]
    assert len(sends) == 1, f"the breadcrumb is uncapped: {sends}"
    assert sends[0][1] == "777", "a room breadcrumb must go to the owner's DM"
    reset_llm_outage_notice_state()


@pytest.mark.asyncio
async def test_two_different_rooms_each_get_their_own_breadcrumb(monkeypatch):
    """The window is per-(surface+chat): one broken room must not silence another."""
    from core.surfaces.llm_outage_notice import reset_llm_outage_notice_state
    reset_llm_outage_notice_state()
    monkeypatch.setattr("core.instance.resolve_owner_telegram_id", lambda: "777")

    bot = FakeBot()
    harness = TelegramHarness(bot, FakeContainer(), object(), webhook_base=None,
                              dedup=FakeDedup(), user_directory=FakeUD())
    await harness._send_owner_only(_ROOM_KEY, _ROOM_CHAT_ID, "⚠️ boom")
    await harness._send_owner_only("agent:main:telegram:group:-100999", "-100999",
                                   "⚠️ boom")
    assert len([c for c in bot.calls if c[0] == "send"]) == 2
    reset_llm_outage_notice_state()


@pytest.mark.asyncio
async def test_the_breadcrumb_does_not_consume_the_outage_notice_window(monkeypatch):
    """Separate buckets, deliberately: a breadcrumb must never eat the window the
    real LLM-outage notice needs."""
    from core.surfaces.llm_outage_notice import (reset_llm_outage_notice_state,
                                                 should_send_llm_outage_notice,
                                                 should_send_owner_breadcrumb)
    reset_llm_outage_notice_state()
    assert should_send_owner_breadcrumb(_ROOM_KEY) is True
    assert should_send_owner_breadcrumb(_ROOM_KEY) is False
    assert should_send_llm_outage_notice(_ROOM_KEY) is True
    reset_llm_outage_notice_state()


# ---------------------------------------------------------------------------
# I10 — money/host/control verbs do not execute from a room
# ---------------------------------------------------------------------------

def _command_result(cmd, *, session_key=_ROOM_KEY):
    from core.surfaces.envelopes import Identity, InboundMessage, SessionSource
    chat_type = "supergroup" if session_key != "agent:main:telegram:dm:777" else "dm"
    src = SessionSource(surface_id="telegram", chat_id=_ROOM_CHAT_ID,
                        chat_type=chat_type)
    ident = Identity(user_id="u_owner", source=src, raw_user_id="777",
                     chat_role="owner")
    inbound = InboundMessage(text=cmd, identity=ident, raw={})
    decision = types.SimpleNamespace(kind=RouteKind.COMMAND, session_key=session_key,
                                     session_id=None, command=cmd.split()[0],
                                     silent=False)
    return types.SimpleNamespace(inbound=inbound, decision=decision)


@pytest.mark.asyncio
@pytest.mark.parametrize("cmd", [
    "/trade", "/wallet", "/deploy", "/launch", "/bridge", "/dev",
    "/pause", "/halt", "/resume", "/approve", "/reject",
    "/allow", "/deny", "/config", "/prefs", "/mcp", "/apps",
    "/invoices", "/settle",
])
async def test_money_host_and_control_verbs_are_refused_from_a_room(cmd, monkeypatch):
    """Redirecting the REPLY to the owner's DM was never enough: the ACTION ran,
    so a member who talked the owner into typing one got it — and the room is the
    one place a shoulder-surfer is guaranteed."""
    from surfaces.telegram import harness as h

    ran = []

    async def _explode(*a, **k):
        ran.append(a)
        return "EXECUTED"

    monkeypatch.setattr(h, "_handle_owner_admin", _explode)
    out = await h._handle_command(object(), _command_result(cmd), None)
    assert ran == [], f"{cmd} EXECUTED from inside a room"
    assert "private chat" in (out or "")
    assert cmd in (out or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("cmd", ["/status", "/goals", "/recap", "/journey",
                                 "/missed", "/groups", "/mute"])
async def test_read_only_and_room_scoped_verbs_still_work_from_a_room(cmd, monkeypatch):
    from surfaces.telegram import harness as h

    async def _ok(*a, **k):
        return "EXECUTED"

    monkeypatch.setattr(h, "_handle_owner_admin", _ok)
    out = await h._handle_command(object(), _command_result(cmd), None)
    assert out == "EXECUTED", f"{cmd} was refused in a room but must stay reachable"


@pytest.mark.asyncio
async def test_the_same_verb_still_works_in_a_dm(monkeypatch):
    from surfaces.telegram import harness as h

    async def _ok(*a, **k):
        return "EXECUTED"

    monkeypatch.setattr(h, "_handle_owner_admin", _ok)
    out = await h._handle_command(
        object(), _command_result("/trade", session_key="agent:main:telegram:dm:777"),
        None)
    assert out == "EXECUTED"


# ---------------------------------------------------------------------------
# 044 C6 round 2 — the I2 pre-check must not re-break `/groups allow here`
# ---------------------------------------------------------------------------

@pytest.fixture
def owner_probe(monkeypatch, tmp_path):
    """The I2 rig, plus a configured owner Telegram id."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYROB_OWNER_TELEGRAM_ID", "9911")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    monkeypatch.delenv("ALLOWED_TELEGRAM_USER_IDS", raising=False)
    calls = []

    async def fake_process_update(*a, **k):
        calls.append((a, k))
        return None

    import surfaces.telegram.inbound as inbound_mod
    monkeypatch.setattr(inbound_mod, "process_update", fake_process_update)
    return calls


def _harness():
    return TelegramHarness(FakeBot(), FakeContainer(), object(), webhook_base=None,
                           dedup=FakeDedup(), user_directory=FakeUD())


@pytest.mark.asyncio
async def test_owner_groups_allow_here_reaches_routing_in_an_unlisted_room(owner_probe):
    """`/groups allow here` is the verb that CREATES the allowlist row and the
    documented first step, so the I2 pre-check (which drops every unlisted-room
    line before `process_update`) made C6's dispatcher fix unreachable on
    Telegram: the documented path was a silent no-op end to end."""
    out = await _harness().handle_update(
        _room_update(uid=11, text="/groups allow here"))
    await _drain()
    assert out == {"ok": True}
    assert len(owner_probe) == 1, "the owner's /groups never reached routing"


@pytest.mark.asyncio
async def test_the_botname_suffix_is_tolerated(owner_probe):
    await _harness().handle_update(
        _room_update(uid=12, text="/groups@MyBot allow here"))
    await _drain()
    assert len(owner_probe) == 1


@pytest.mark.asyncio
async def test_a_stranger_groups_line_is_still_dropped(owner_probe, monkeypatch):
    """The carve-out is the OWNER's alone — a stranger cannot spend a turn (or a
    ledger row) in a room the owner never allowed by typing the magic verb."""
    upd = _room_update(uid=13, text="/groups allow here")
    upd["message"]["from"] = {"id": 5555, "username": "mallory"}
    await _harness().handle_update(upd)
    await _drain()
    assert owner_probe == []


@pytest.mark.asyncio
async def test_an_owner_voice_note_in_an_unlisted_room_is_still_dropped(owner_probe):
    """The expensive half of I2 must survive the carve-out: only a `/groups`
    TEXT line is admitted, so a voice note is never downloaded or transcribed."""
    voice = _room_update(uid=14)
    voice["message"].pop("text")
    voice["message"]["voice"] = {"file_id": "f-1", "duration": 3}
    harness = _harness()
    transcribed = []
    harness._transcribe_voice = lambda *a, **k: transcribed.append(a) or "text"
    await harness.handle_update(voice)
    await _drain()
    assert owner_probe == [] and transcribed == []


@pytest.mark.asyncio
async def test_an_owner_ordinary_line_in_an_unlisted_room_is_still_dropped(owner_probe):
    await _harness().handle_update(_room_update(uid=15, text="hey rob, what's up"))
    await _drain()
    assert owner_probe == []


@pytest.mark.asyncio
async def test_another_owner_slash_verb_is_still_dropped(owner_probe):
    """Only `/groups`. `/status` in an unlisted room stays a dropped line."""
    await _harness().handle_update(_room_update(uid=16, text="/status"))
    await _drain()
    assert owner_probe == []


@pytest.mark.asyncio
async def test_owner_groups_allow_here_really_writes_the_allowlist_row(tmp_path, monkeypatch):
    """End to end, through the REAL routing + command dispatch: the documented
    first step actually allows the room it was typed in."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYROB_OWNER_TELEGRAM_ID", "9911")
    monkeypatch.setenv("POLYROB_OWNER_USER_ID", "rob")
    monkeypatch.setenv("GROUP_CHAT_ENABLED", "true")
    monkeypatch.setenv("SINGULAR_CHAT_ENABLED", "true")
    monkeypatch.delenv("ALLOWED_TELEGRAM_USER_IDS", raising=False)
    monkeypatch.setattr("core.runtime_paths.resolve_data_home", lambda: tmp_path)

    import types as _t
    from core.surfaces.session_chat_registry import SessionChatRegistry

    class _C:
        def __init__(self):
            self.config = _t.SimpleNamespace(data_dir=str(tmp_path))
            self._svc = {"session_chat_registry":
                         SessionChatRegistry(str(tmp_path / "c.db"))}

        def get_service(self, n):
            return self._svc.get(n)

    bot = FakeBot()
    harness = TelegramHarness(bot, _C(), object(), webhook_base=None,
                              dedup=FakeDedup(), user_directory=FakeUD())
    harness.task_agent = _t.SimpleNamespace(container=harness.container)
    await harness.handle_update(_room_update(uid=21, text="/groups allow here"))
    await _drain()

    from core.surfaces.group_allowlist import GroupAllowlist
    import os
    store = GroupAllowlist(os.path.join(str(tmp_path), "group_allowlist.db"))
    assert store.is_allowed("telegram", _ROOM_CHAT_ID) is True, (
        f"the room was not allowed; bot said {bot.sent_texts()}")
    # …and the confirmation went to the owner's DM, not into the room.
    assert all(c[1] != _ROOM_CHAT_ID for c in bot.calls if c[0] == "send"), bot.calls
