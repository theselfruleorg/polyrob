import asyncio
import types

import pytest

from surfaces.telegram.harness import TelegramHarness
from core.surfaces.media import Media


class FakeBot:
    def __init__(self):
        self.calls = []
        self._n = 0

    async def send_message(self, chat_id, text, reply_to_message_id=None):
        self._n += 1
        self.calls.append(("send", str(chat_id), text, reply_to_message_id))
        return types.SimpleNamespace(message_id=self._n)

    async def edit_message_text(self, *, text, chat_id, message_id):
        self.calls.append(("edit", str(chat_id), text, None))

    async def delete_message(self, chat_id, message_id):
        self.calls.append(("delete", str(chat_id), None, None))

    async def send_chat_action(self, chat_id, action):
        self.calls.append(("action", str(chat_id), action, None))

    def sent(self):
        return [(c[2], c[3]) for c in self.calls if c[0] == "send"]


class FakeDedup:
    def seen(self, u, now=None):
        return False

    def peek(self, u):
        return False


class FakeUD:
    def resolve_internal(self, tg_id, src):
        return "u_" + str(tg_id)


class FakeContainer:
    def get_service(self, n):
        return None


def _harness(bot):
    return TelegramHarness(bot, FakeContainer(), object(), webhook_base=None,
                           dedup=FakeDedup(), user_directory=FakeUD())


def _voice_update(uid=2, from_id=777, chat_id=555, msg_id=42):
    return {"update_id": uid, "message": {"message_id": msg_id,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": from_id, "username": "me"}, "voice": {"file_id": "vf"}}}


def _result(text, media, kind="TASK_AGENT", key="telegram:555:dm"):
    inbound = types.SimpleNamespace(text=text, media=media,
              identity=types.SimpleNamespace(user_id="u1", source=None))
    decision = types.SimpleNamespace(kind=kind, session_key=key, session_id="s1", command=None)
    return types.SimpleNamespace(inbound=inbound, decision=decision)


async def _drain():
    for _ in range(50):
        await asyncio.sleep(0)
        if not [t for t in asyncio.all_tasks()
                if t is not asyncio.current_task() and not t.done()]:
            return


def _spawn_act():
    async def act(task_agent, result, *, spawn, deliver=None):
        async def _turn():
            return None
        spawn(_turn())
        return None
    return act


def _patch(monkeypatch, *, result, act):
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "777")
    import surfaces.telegram.inbound as inbound_mod
    import surfaces.telegram.harness as harness_mod
    import surfaces.telegram.surface as surface_mod

    async def fake_pu(*a, **k):
        return result
    monkeypatch.setattr(inbound_mod, "process_update", fake_pu)
    monkeypatch.setattr(harness_mod, "act_on_inbound", act)
    monkeypatch.setattr(surface_mod, "chat_id_from_session_key", lambda key: "555")


@pytest.mark.asyncio
async def test_voice_echo_quotes_voice_note(monkeypatch):
    monkeypatch.setenv("VOICE_TRANSCRIPT_ECHO", "true")
    bot = FakeBot()
    media = [Media(kind="voice", transcript="turn on the lights")]
    _patch(monkeypatch, result=_result("turn on the lights", media), act=_spawn_act())
    await _harness(bot).handle_update(_voice_update(msg_id=42))
    await _drain()
    echoes = [(t, r) for (t, r) in bot.sent() if t.startswith("🎙️ Transcript")]
    assert echoes == [('🎙️ Transcript: "turn on the lights"', 42)]


@pytest.mark.asyncio
async def test_no_echo_for_text_message(monkeypatch):
    monkeypatch.setenv("VOICE_TRANSCRIPT_ECHO", "true")
    bot = FakeBot()
    _patch(monkeypatch, result=_result("hello", []), act=_spawn_act())
    await _harness(bot).handle_update({"update_id": 1, "message": {"message_id": 7,
        "chat": {"id": 555, "type": "private"}, "from": {"id": 777}, "text": "hello"}})
    await _drain()
    assert not any(t.startswith("🎙️ Transcript") for (t, _) in bot.sent())


@pytest.mark.asyncio
async def test_flag_off_no_echo(monkeypatch):
    monkeypatch.setenv("VOICE_TRANSCRIPT_ECHO", "false")
    bot = FakeBot()
    media = [Media(kind="voice", transcript="hi")]
    _patch(monkeypatch, result=_result("hi", media), act=_spawn_act())
    await _harness(bot).handle_update(_voice_update())
    await _drain()
    assert not any(t.startswith("🎙️ Transcript") for (t, _) in bot.sent())
