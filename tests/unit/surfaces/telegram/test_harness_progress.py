"""handle_update drives a staged progress reporter: voice -> '🎤 Transcribing…' edited to
'⚙️ Working…' then deleted; text -> '⚙️ Working…' then deleted; status removed before an
immediate reply; no orphan on any RouteKind; redelivery doesn't re-post the status."""
import asyncio
import types

import pytest

from surfaces.telegram.harness import TelegramHarness
from core.surfaces.media import Media
from core.surfaces.progress import ProgressStage


class FakeBot:
    def __init__(self):
        self.calls = []   # ordered ops
        self._n = 0

    async def send_message(self, chat_id, text):
        self._n += 1
        self.calls.append(("send", str(chat_id), text, self._n))
        return types.SimpleNamespace(message_id=self._n)

    async def edit_message_text(self, *, text, chat_id, message_id):
        self.calls.append(("edit", str(chat_id), text, message_id))

    async def delete_message(self, chat_id, message_id):
        self.calls.append(("delete", str(chat_id), message_id))

    async def send_chat_action(self, chat_id, action):
        self.calls.append(("action", str(chat_id), action))

    def ops(self):
        return [c[0] for c in self.calls]

    def sent_texts(self):
        return [c[2] for c in self.calls if c[0] == "send"]


class FakeDedup:
    def __init__(self, peek=False):
        self._peek = peek
    def seen(self, update_id, now=None):
        return False
    def peek(self, update_id):
        return self._peek


class FakeUD:
    def resolve_internal(self, tg_id, src):
        return "u_" + str(tg_id)


class FakeContainer:
    def get_service(self, n):
        return None


def _harness(bot, dedup=None):
    return TelegramHarness(
        bot, FakeContainer(), object(),
        webhook_base=None, dedup=dedup or FakeDedup(), user_directory=FakeUD(),
    )


def _voice_update(uid=2, from_id=777, chat_id=555):
    return {"update_id": uid, "message": {
        "chat": {"id": chat_id, "type": "private"},
        "from": {"id": from_id, "username": "me"},
        "voice": {"file_id": "vf"}}}


def _text_update(uid=1, from_id=777, chat_id=555, text="hello"):
    return {"update_id": uid, "message": {
        "chat": {"id": chat_id, "type": "private"},
        "from": {"id": from_id, "username": "me"}, "text": text}}


def _result(text="hi", kind="STEER", session_key="telegram:555:dm", media=None):
    # media must mirror what build_inbound_message now sets so handle_update's
    # core voice_needs_guard check (Task 1.6) has the right list.
    inbound = types.SimpleNamespace(
        text=text,
        media=media if media is not None else [],
        identity=types.SimpleNamespace(user_id="u1", source=None))
    decision = types.SimpleNamespace(
        kind=kind, session_key=session_key, session_id="s1", command=None)
    return types.SimpleNamespace(inbound=inbound, decision=decision)


async def _drain():
    """Let spawned background tasks (the _wrapped turn) run to completion."""
    for _ in range(50):
        await asyncio.sleep(0)
        pending = [t for t in asyncio.all_tasks()
                   if t is not asyncio.current_task() and not t.done()]
        if not pending:
            return


def _patch(monkeypatch, *, result, act):
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "777")
    import surfaces.telegram.inbound as inbound_mod
    import surfaces.telegram.harness as harness_mod
    import surfaces.telegram.surface as surface_mod

    async def fake_process_update(*a, **k):
        return result
    monkeypatch.setattr(inbound_mod, "process_update", fake_process_update)
    monkeypatch.setattr(harness_mod, "act_on_inbound", act)
    monkeypatch.setattr(surface_mod, "chat_id_from_session_key", lambda key: "555")


def _spawn_act(result_text=None):
    """act_on_inbound that spawns a completing turn (returns None)."""
    async def act(task_agent, result, *, spawn, deliver=None):
        async def _turn():
            return None
        spawn(_turn())
        return None
    return act


@pytest.mark.asyncio
async def test_text_turn_working_then_delete_on_spawn(monkeypatch):
    bot = FakeBot()
    _patch(monkeypatch, result=_result(text="hello"), act=_spawn_act())
    await _harness(bot).handle_update(_text_update())
    await _drain()
    # status: send WORKING (no TRANSCRIBING for text), then delete after the turn
    assert ProgressStage.WORKING in bot.sent_texts()
    assert ProgressStage.TRANSCRIBING not in bot.sent_texts()
    assert "delete" in bot.ops()


@pytest.mark.asyncio
async def test_voice_transcribing_then_working_then_delete(monkeypatch):
    bot = FakeBot()
    _voice_media = [Media(kind="voice", mime="audio/ogg")]
    _patch(monkeypatch, result=_result(text="turn on the lights", media=_voice_media), act=_spawn_act())
    await _harness(bot, FakeDedup(peek=False)).handle_update(_voice_update())
    await _drain()
    texts = bot.sent_texts()
    assert ProgressStage.TRANSCRIBING in texts           # sent before transcription
    # WORKING is an EDIT of the same status bubble (not a fresh send)
    assert any(c[0] == "edit" and c[2] == ProgressStage.WORKING for c in bot.calls)
    assert "delete" in bot.ops()


@pytest.mark.asyncio
async def test_voice_redelivery_does_not_prestage(monkeypatch):
    bot = FakeBot()
    # peek=True -> redelivery -> no TRANSCRIBING; process_update returns None
    _patch(monkeypatch, result=None, act=_spawn_act())
    await _harness(bot, FakeDedup(peek=True)).handle_update(_voice_update())
    await _drain()
    assert ProgressStage.TRANSCRIBING not in bot.sent_texts()
    assert bot.ops() == []   # no status, no reply — clean


@pytest.mark.asyncio
async def test_immediate_reply_deletes_status_before_reply(monkeypatch):
    bot = FakeBot()

    async def act(task_agent, result, *, spawn, deliver=None):
        return "🔒 You're not authorized."   # DENIED-style immediate reply, no spawn

    _patch(monkeypatch, result=_result(text="hi"), act=act)
    await _harness(bot).handle_update(_text_update())
    await _drain()
    ops = bot.ops()
    assert "delete" in ops and "send" in ops
    # the status was deleted BEFORE the reply was sent
    assert ops.index("delete") < ops.index("send", ops.index("delete"))
    assert "🔒 You're not authorized." in bot.sent_texts()


@pytest.mark.asyncio
async def test_voice_guard_clears_transcribing(monkeypatch):
    bot = FakeBot()
    # empty transcript + voice media -> voice_needs_guard True -> finish + guard, no agent run
    _voice_media = [Media(kind="voice", mime="audio/ogg")]
    _patch(monkeypatch, result=_result(text="", media=_voice_media), act=_spawn_act())
    await _harness(bot, FakeDedup(peek=False)).handle_update(_voice_update())
    await _drain()
    ops = bot.ops()
    assert "delete" in ops                       # TRANSCRIBING removed
    assert any("voice" in t.lower() or "🎤" in t for t in bot.sent_texts())  # guard sent


@pytest.mark.asyncio
async def test_no_reply_no_spawn_finishes(monkeypatch):
    bot = FakeBot()

    async def act(task_agent, result, *, spawn, deliver=None):
        return None   # neither replies NOR spawns (create_session-no-id case)

    _patch(monkeypatch, result=_result(text="hi"), act=act)
    await _harness(bot).handle_update(_text_update())
    await _drain()
    assert "delete" in bot.ops()   # WORKING cleaned up via the `elif not spawned` branch


@pytest.mark.asyncio
async def test_empty_nonvoice_message_dropped(monkeypatch):
    bot = FakeBot()
    called = {"act": False}

    async def act(task_agent, result, *, spawn, deliver=None):
        called["act"] = True
        return None

    # empty text, NOT a voice update -> must be dropped without dispatching a turn
    _patch(monkeypatch, result=_result(text="   "), act=act)
    await _harness(bot).handle_update(_text_update(text=""))
    await _drain()
    assert called["act"] is False              # no agent dispatch on empty content
    assert ProgressStage.WORKING not in bot.sent_texts()


@pytest.mark.asyncio
async def test_spawn_error_breadcrumb(monkeypatch):
    bot = FakeBot()

    async def act(task_agent, result, *, spawn, deliver=None):
        async def _turn():
            raise RuntimeError("turn blew up")
        spawn(_turn())
        return None

    _patch(monkeypatch, result=_result(text="hi"), act=act)
    await _harness(bot).handle_update(_text_update())
    await _drain()
    # status deleted, then a one-line error breadcrumb sent
    assert "delete" in bot.ops()
    assert any("went wrong" in t.lower() for t in bot.sent_texts())
