"""Voice out (064 S2b F5): text → a voice note in the surface's format.

``speak(text, fmt)`` returns a :class:`SpeechResult` — the audio, or the reason
there is none. Two engines, first usable wins: OpenAI ``/v1/audio/speech``
(``OPENAI_API_KEY``) asked for ``opus`` — already an Ogg/Opus voice note — and a
local espeak-ng / espeak, whose WAV needs ffmpeg. They live HERE, not in
``modules/``: they need nothing from that tier, and core may not import upward
(``tests/test_layering_ratchet.py``). ffmpeg is optional. A missing
engine or a missing ffmpeg is ``unavailable``, never a crash and never a
silently empty voice note.

Default OFF: the owner turns it on with the preference ``voice.replies``
(``/prefs set voice.replies true``). It costs a speech call per reply, so it is
never on by default and never an env flag.
"""
import asyncio
import logging
import os
import shutil
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


# --- engines ---------------------------------------------------------------

#: The OpenAI speech model and voice (the cheapest model that speaks well).
OPENAI_TTS_MODEL = "gpt-4o-mini-tts"
OPENAI_TTS_VOICE = "alloy"


class SpeechUnavailable(RuntimeError):
    """No usable speech engine, with the reason in ``str(exc)``."""


def openai_key() -> Optional[str]:
    return (os.environ.get("OPENAI_API_KEY") or "").strip() or None


async def synthesize_openai_opus(text: str, *, voice: str = OPENAI_TTS_VOICE) -> bytes:
    """Ogg/Opus bytes for ``text`` from OpenAI's speech endpoint."""
    key = openai_key()
    if not key:
        raise SpeechUnavailable("no OPENAI_API_KEY for the speech endpoint")
    import aiohttp
    body = {"model": OPENAI_TTS_MODEL, "voice": voice, "input": text,
            "response_format": "opus"}
    timeout = aiohttp.ClientTimeout(total=60)
    async with aiohttp.ClientSession(timeout=timeout) as s, s.post(
            "https://api.openai.com/v1/audio/speech", json=body,
            headers={"Authorization": f"Bearer {key}"}) as resp:
        if resp.status != 200:
            raise SpeechUnavailable(f"speech endpoint answered HTTP {resp.status}")
        return await resp.read()


def local_engine() -> Optional[str]:
    return shutil.which("espeak-ng") or shutil.which("espeak")


async def _run_audio_process(*args: str, audio: bytes) -> bytes:
    """Run a fixed audio converter without credentials; always reap its child."""
    proc = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, env={"PATH": os.defpath})
    try:
        out, _ = await asyncio.wait_for(proc.communicate(audio), timeout=60)
    except BaseException:
        # wait_for cancels communicate(), not the underlying OS process.
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        await proc.wait()
        raise
    if proc.returncode != 0 or not out:
        raise SpeechUnavailable(f"{os.path.basename(args[0])} exited {proc.returncode} with no usable audio")
    return out


async def synthesize_local_wav(text: str) -> bytes:
    """WAV bytes from espeak-ng / espeak (text on stdin, WAV on stdout)."""
    engine = local_engine()
    if not engine:
        raise SpeechUnavailable("no local speech engine (espeak-ng)")
    return await _run_audio_process(engine, "--stdout", audio=text.encode("utf-8"))


#: The surface formats this module can produce.
OGG_OPUS = "ogg_opus"

#: Longer replies are not spoken: a two-minute voice note of a report helps no one.
MAX_SPOKEN_CHARS = 1200


@dataclass(frozen=True)
class SpeechResult:
    audio: Optional[bytes]
    fmt: str = OGG_OPUS
    reason: str = ""          # why there is no audio ("" when there is)

    @property
    def ok(self) -> bool:
        return bool(self.audio)


def ffmpeg_path() -> Optional[str]:
    return shutil.which("ffmpeg")


async def transcode_to_ogg_opus(audio: bytes) -> bytes:
    """Any ffmpeg-readable audio → Ogg/Opus (voice-note quality)."""
    ff = ffmpeg_path()
    if not ff:
        raise RuntimeError("ffmpeg not installed")
    return await _run_audio_process(
        ff, "-loglevel", "error", "-i", "pipe:0", "-c:a", "libopus", "-b:a", "32k",
        "-f", "ogg", "pipe:1", audio=audio)


async def speak(text: str, fmt: str = OGG_OPUS) -> SpeechResult:
    """Synthesize ``text`` as ``fmt``; the audio or the honest reason."""
    text = (text or "").strip()
    if fmt != OGG_OPUS:
        return SpeechResult(None, fmt, f"unavailable(format {fmt!r} not supported)")
    if not text:
        return SpeechResult(None, fmt, "unavailable(nothing to say)")
    if len(text) > MAX_SPOKEN_CHARS:
        return SpeechResult(None, fmt, f"unavailable(reply longer than {MAX_SPOKEN_CHARS} chars)")
    reasons = []
    if openai_key():
        try:
            audio = await synthesize_openai_opus(text)
            if not audio:
                raise SpeechUnavailable("speech endpoint returned empty audio")
            return SpeechResult(audio, fmt)
        except Exception as e:
            reasons.append(str(e) if isinstance(e, SpeechUnavailable) else type(e).__name__)
    if local_engine():
        if not ffmpeg_path():
            reasons.append("ffmpeg not installed (needed for the local engine)")
        else:
            try:
                wav = await synthesize_local_wav(text)
                return SpeechResult(await transcode_to_ogg_opus(wav), fmt)
            except Exception as e:
                reasons.append(str(e) or type(e).__name__)
    if not reasons:
        reasons.append("no speech engine: set OPENAI_API_KEY or install espeak-ng + ffmpeg")
    return SpeechResult(None, fmt, "unavailable(" + "; ".join(reasons) + ")")


def voice_replies_enabled(user_id: str) -> bool:
    """The owner's ``voice.replies`` preference (default OFF). Unreadable = OFF."""
    try:
        from core import prefs
        from core.runtime_paths import data_dir_or_home
        return bool(prefs.resolve("voice.replies", user_id, data_dir_or_home(None),
                                  env_value=None, default=False))
    except Exception:
        logger.debug("voice.replies unreadable — treated as off", exc_info=True)
        return False
