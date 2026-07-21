"""Native voice speaking (modules/pfp/voice.py) — engine command construction only.

Nothing here actually speaks: the subprocess runner and `which` are injected. What's
locked down: the signature→engine parameter mapping (pitch→semitones/-p/SSML,
rate→wpm/%, timbre→persona pick) and the honest no-engine failure.
"""
import pytest

from modules.pfp.voice import speak_voice, VoiceUnavailable, _pick

VOICE = {"pitch": 1.29, "rate": 1.02, "timbre": 0.78}


class _Runner:
    """Captures every subprocess invocation; serves a canned `say -v ?` listing."""

    def __init__(self):
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)

        class R:
            stdout = ("Samantha            en_US    # Hello!\n"
                      "Daniel               en_GB    # Hello!\n"
                      "Zarvox               en_US    # I am a robot\n"
                      "Amelie               fr_CA    # Bonjour\n")
        return R()


def test_macos_say_maps_the_signature():
    r = _Runner()
    engine = speak_voice(VOICE, "test line", runner=r, platform="darwin",
                         which=lambda exe: "/usr/bin/say" if exe == "say" else None)
    assert engine.startswith("say")
    speak = r.calls[-1]
    assert speak[0] == "say"
    assert speak[speak.index("-r") + 1] == "178"          # round(175 * 1.02)
    # timbre picks from the CLEAR en pool (Zarvox filtered, fr excluded) — stable
    persona = speak[speak.index("-v") + 1]
    assert persona in ("Daniel", "Samantha")
    assert "[[pbas +4.4]]" in speak[-1]                   # 12·log2(1.29)
    assert "test line" in speak[-1]


def test_espeak_maps_the_signature():
    r = _Runner()
    engine = speak_voice(VOICE, "hi", runner=r, platform="linux",
                         which=lambda exe: "/usr/bin/espeak-ng" if exe == "espeak-ng" else None)
    assert engine.startswith("espeak-ng")
    cmd = r.calls[-1]
    assert cmd[cmd.index("-p") + 1] == "64"               # 50 * 1.29 rounded
    assert cmd[cmd.index("-s") + 1] == "178"
    assert cmd[cmd.index("-v") + 1].startswith("en+")


def test_no_engine_raises_with_web_pointers():
    with pytest.raises(VoiceUnavailable) as e:
        speak_voice(VOICE, "hi", runner=_Runner(), platform="linux",
                    which=lambda exe: None)
    assert "/identity" in str(e.value)
    assert "studio" in str(e.value)


def test_timbre_pick_is_stable_and_in_range():
    pool = ["a", "b", "c", "d"]
    assert _pick(pool, 0.0) == "a"
    assert _pick(pool, 0.99) == "d"
    assert _pick(pool, 1.0) == "a"                        # studio's modulo wrap
    assert _pick([], 0.5) is None
