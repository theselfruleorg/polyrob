"""Voice out (064 S2b F5): text → an Ogg/Opus voice note, honest when it cannot.

Offline: the engines and ffmpeg are stubbed.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from core.surfaces import speech_out
from core.surfaces.speech_out import OGG_OPUS, speak


@pytest.fixture
def engines(monkeypatch):
    sp = speech_out
    state = {"key": None, "local": None, "ffmpeg": None, "opus": b"OggS-opus"}

    monkeypatch.setattr(sp, "openai_key", lambda: state["key"])
    monkeypatch.setattr(sp, "local_engine", lambda: state["local"])
    monkeypatch.setattr(speech_out, "ffmpeg_path", lambda: state["ffmpeg"])

    async def _openai(text, **kw):
        return state["opus"]

    async def _wav(text):
        return b"RIFF-wav"

    async def _transcode(audio):
        assert audio == b"RIFF-wav"
        return b"OggS-from-wav"

    monkeypatch.setattr(sp, "synthesize_openai_opus", _openai)
    monkeypatch.setattr(sp, "synthesize_local_wav", _wav)
    monkeypatch.setattr(speech_out, "transcode_to_ogg_opus", _transcode)
    return state


@pytest.mark.asyncio
async def test_openai_opus_needs_no_transcode(engines):
    engines["key"] = "sk-test"
    r = await speak("hello", OGG_OPUS)
    assert r.ok and r.audio == b"OggS-opus" and r.reason == ""


@pytest.mark.asyncio
async def test_local_engine_goes_through_ffmpeg(engines):
    engines["local"] = "/usr/bin/espeak-ng"
    engines["ffmpeg"] = "/usr/bin/ffmpeg"
    r = await speak("hello")
    assert r.ok and r.audio == b"OggS-from-wav"


@pytest.mark.asyncio
async def test_missing_ffmpeg_is_unavailable_not_a_crash(engines):
    engines["local"] = "/usr/bin/espeak-ng"
    r = await speak("hello")
    assert not r.ok
    assert r.reason.startswith("unavailable(") and "ffmpeg" in r.reason


@pytest.mark.asyncio
async def test_no_engine_names_both_remedies(engines):
    r = await speak("hello")
    assert not r.ok and "OPENAI_API_KEY" in r.reason and "espeak-ng" in r.reason


@pytest.mark.asyncio
async def test_empty_speech_response_explains_the_failure(engines):
    engines["key"] = "sk-test"
    engines["opus"] = b""
    r = await speak("hello")
    assert not r.ok and "empty" in r.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["synthesize_local_wav", "transcode_to_ogg_opus"])
@pytest.mark.parametrize("failure", [TimeoutError, asyncio.CancelledError])
async def test_audio_process_is_reaped_on_timeout_or_cancellation(monkeypatch, engine, failure):
    proc = SimpleNamespace(
        communicate=AsyncMock(side_effect=failure), returncode=None,
        kill=Mock(), wait=AsyncMock(),
    )
    create = AsyncMock(return_value=proc)
    monkeypatch.setattr(speech_out, "local_engine", lambda: "/usr/bin/espeak")
    monkeypatch.setattr(speech_out, "ffmpeg_path", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    with pytest.raises(failure):
        await getattr(speech_out, engine)("hello" if engine == "synthesize_local_wav" else b"wav")
    proc.kill.assert_called_once()
    proc.wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_speech_child_does_not_inherit_provider_or_wallet_secrets(monkeypatch):
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", "test-only-seed")
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-key")
    monkeypatch.setattr(speech_out, "local_engine", lambda: "/usr/bin/espeak")
    proc = SimpleNamespace(communicate=AsyncMock(return_value=(b"RIFF", None)), returncode=0)
    create = AsyncMock(return_value=proc)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    assert await speech_out.synthesize_local_wav("hello") == b"RIFF"
    env = create.call_args.kwargs.get("env")
    assert env is not None  # None inherits the complete credential-bearing environment.
    assert "AGENT_WALLET_MASTER_SEED" not in env
    assert "OPENAI_API_KEY" not in env


@pytest.mark.asyncio
async def test_long_or_empty_text_is_not_spoken(engines):
    engines["key"] = "sk-test"
    assert not (await speak("x" * (speech_out.MAX_SPOKEN_CHARS + 1))).ok
    assert not (await speak("   ")).ok
    assert not (await speak("hi", "amr")).ok


def test_voice_replies_is_a_pref_default_off(tmp_path, monkeypatch):
    from core.prefs import PREF_SCHEMA
    spec = PREF_SCHEMA["voice.replies"]
    assert spec.type == "bool" and spec.env_flag is None and spec.default_display is False
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    assert speech_out.voice_replies_enabled("u_owner") is False


# --- the one renderer: Telegram sendVoice -------------------------------------------

class _Bot:
    def __init__(self):
        self.texts, self.voices = [], []

    async def send_message(self, chat_id, text, **kw):
        self.texts.append((chat_id, text))

        class _M:
            message_id = 1
        return _M()

    async def send_voice(self, chat_id, voice, **kw):
        self.voices.append((chat_id, voice))


def _owner_env(monkeypatch, on: bool):
    monkeypatch.setenv("POLYROB_OWNER_TELEGRAM_ID", "42")
    monkeypatch.setattr("core.surfaces.speech_out.voice_replies_enabled", lambda uid: on)


@pytest.mark.asyncio
async def test_telegram_speaks_an_owner_reply_when_the_pref_is_on(engines, monkeypatch):
    from core.surfaces.envelopes import OutboundMessage
    from surfaces.telegram.surface import TelegramSurface
    engines["key"] = "sk-test"
    _owner_env(monkeypatch, True)
    bot = _Bot()
    s = TelegramSurface(bot)
    assert s.capabilities.voice_out == "ogg_opus"
    await s.send(OutboundMessage(session_key="agent:main:telegram:dm:42", text="Done."))
    assert bot.texts == [("42", "Done.")]            # the text always goes first
    assert len(bot.voices) == 1


@pytest.mark.asyncio
async def test_telegram_stays_silent_when_off_in_a_room_or_for_others(engines, monkeypatch):
    from core.surfaces.envelopes import OutboundMessage
    from surfaces.telegram.surface import TelegramSurface
    engines["key"] = "sk-test"
    bot = _Bot()
    s = TelegramSurface(bot)
    _owner_env(monkeypatch, False)
    await s.send(OutboundMessage(session_key="agent:main:telegram:dm:42", text="a"))
    _owner_env(monkeypatch, True)
    await s.send(OutboundMessage(session_key="agent:main:telegram:dm:777", text="b"))
    await s.send(OutboundMessage(session_key="agent:main:telegram:group:-1001", text="c"))
    assert bot.voices == []


@pytest.mark.asyncio
async def test_telegram_no_engine_keeps_the_text_and_sends_no_voice(engines, monkeypatch):
    from core.surfaces.envelopes import OutboundMessage
    from surfaces.telegram.surface import TelegramSurface
    _owner_env(monkeypatch, True)
    bot = _Bot()
    res = await TelegramSurface(bot).send(
        OutboundMessage(session_key="agent:main:telegram:dm:42", text="Done."))
    assert res.success and bot.texts == [("42", "Done.")] and bot.voices == []
