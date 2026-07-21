"""Speak the agent's voice signature through a native TTS engine (local setup).

The stored ``{pitch, rate, timbre}`` (see ``avatar/README.md``) is engine-agnostic:
pitch/rate are ~1.0-centered multipliers, timbre 0–1 picks a persona from whatever
clear voices the engine offers (the same mapping the studio/webview use with
``speechSynthesis``). Engines, first available wins:

- macOS ``say`` — voice by timbre (clear-English pool), ``-r`` wpm, ``[[pbas ±N]]``
  semitone pitch shift.
- ``espeak-ng`` / ``espeak`` — ``-p`` pitch (50-centered), ``-s`` wpm, ``en+m/f``
  variant by timbre.
- Windows PowerShell SAPI — SSML prosody pitch/rate.

No engine → :class:`VoiceUnavailable` with pointers to the web paths (the webview
/identity page and ``polyrob pfp studio`` both speak in the browser). Never a hard
dependency; callers treat speech as best-effort.
"""
from __future__ import annotations

import math
import re
import shutil
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional

DEFAULT_TEXT = "Hello! This is my voice — pleased to meet you."
_BASE_WPM = 175  # ~normal speech; rate is a multiplier around 1.0

# Mirrors the studio's clear-English filter (novelty/mumbly voices excluded).
_NOVELTY = re.compile(
    r"\b(albert|bad news|bahh|bells|boing|bubbles|cellos|deranged|good news|jester|"
    r"organ|superstar|trinoids|whisper|wobble|zarvox|flo|grandma|grandpa|reed|rocko|"
    r"sandy|shelley|eddy|junior|kathy|princess|ralph)\b", re.IGNORECASE)


class VoiceUnavailable(RuntimeError):
    """No native TTS engine found on this machine."""


def _norm(voice: Optional[Dict[str, Any]]) -> Dict[str, float]:
    v = voice or {}
    return {
        "pitch": float(v.get("pitch", 1.0)),
        "rate": float(v.get("rate", 1.0)),
        "timbre": float(v.get("timbre", 0.5)),
    }


def _pick(pool: List[str], timbre: float) -> Optional[str]:
    """Stable timbre→persona mapping (same formula as the studio)."""
    if not pool:
        return None
    return pool[int(timbre * len(pool)) % len(pool)]


def _macos_voices(runner: Callable[..., Any]) -> List[str]:
    """Installed clear-English `say` voices, sorted for a stable timbre mapping."""
    try:
        out = runner(["say", "-v", "?"], capture_output=True, text=True, timeout=10)
        lines = (out.stdout or "").splitlines()
    except Exception:
        return []
    pool = []
    for line in lines:
        m = re.match(r"^(.*?)\s{2,}(\S+)\s+#", line)
        if not m:
            continue
        name, lang = m.group(1).strip(), m.group(2)
        if lang.lower().startswith("en") and not _NOVELTY.search(name):
            pool.append(name)
    return sorted(pool)


def speak_voice(voice: Optional[Dict[str, Any]], text: str = DEFAULT_TEXT, *,
                runner: Callable[..., Any] = subprocess.run,
                platform: Optional[str] = None,
                which: Callable[[str], Optional[str]] = shutil.which) -> str:
    """Speak ``text`` with the signature ``voice``; return the engine used.

    ``runner``/``platform``/``which`` are injectable for tests. Blocks until the
    engine finishes (one short sample line). Raises :class:`VoiceUnavailable`
    when no engine exists."""
    v = _norm(voice)
    plat = platform or sys.platform
    wpm = max(80, min(400, round(_BASE_WPM * v["rate"])))

    if plat == "darwin" and which("say"):
        # pitch multiplier -> relative semitones (12·log2), clamped to something sane
        semis = max(-10.0, min(10.0, 12.0 * math.log2(max(0.25, v["pitch"]))))
        cmd = ["say", "-r", str(wpm)]
        persona = _pick(_macos_voices(runner), v["timbre"])
        if persona:
            cmd += ["-v", persona]
        cmd.append(f"[[pbas {'+' if semis >= 0 else ''}{semis:.1f}]] {text}")
        runner(cmd, timeout=60)
        return f"say ({persona or 'default voice'})"

    for exe in ("espeak-ng", "espeak"):
        if which(exe):
            pool = [f"en+m{i}" for i in range(1, 8)] + [f"en+f{i}" for i in range(1, 5)]
            persona = _pick(sorted(pool), v["timbre"]) or "en"
            pitch = max(0, min(99, round(50 * v["pitch"])))
            runner([exe, "-v", persona, "-p", str(pitch), "-s", str(wpm), text],
                   timeout=60)
            return f"{exe} ({persona})"

    if plat.startswith("win") and which("powershell"):
        pitch_pct = round((v["pitch"] - 1.0) * 100)
        rate_pct = round((v["rate"] - 1.0) * 100)
        safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        ssml = (f"<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' "
                f"xml:lang='en-US'><prosody pitch='{pitch_pct:+d}%' "
                f"rate='{rate_pct:+d}%'>{safe}</prosody></speak>")
        script = ("Add-Type -AssemblyName System.Speech; "
                  "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                  f"$s.SpeakSsml(\"{ssml.replace(chr(34), '`' + chr(34))}\")")
        runner(["powershell", "-NoProfile", "-Command", script], timeout=60)
        return "sapi"

    raise VoiceUnavailable(
        "no native TTS engine found (say / espeak-ng / SAPI). Hear the voice in the "
        "browser instead: the webview /identity page or `polyrob pfp studio`."
    )
