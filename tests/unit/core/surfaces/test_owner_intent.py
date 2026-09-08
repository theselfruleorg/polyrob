"""031 T3: the deterministic (LLM-free) owner stop/resume intent parser."""
import pytest

from core.surfaces.owner_intent import owner_stop_intent, parse_pause_args, strip_voice_prefix

FULL = [
    "Stop",
    "Stop\nAutonomy cancel\nAll goals",
    "[voice message, auto-transcribed] Stop all ghosts today, please.",
    "stop everything",
    "halt",
    "pause",
    "Stop the agent",
    "autonomy off",
    "STOP ALL GOALS NOW",
    "dude stop it",
    "Stop!",
    "ok stop",
    "kill switch",
    "shut it down",
    "stand down",
    "stop stop stop",
    "STOP STOP",
]
SCOPED = [
    "stop for now all autonomopus trading, close all positions for up untill we will do some updates",
    "Stop rendering endless videos",
    "stop all x402 related goals. dude ou need tio test",
    "Haha baby stop posting here what's your issue",
    "pause the dev loop",
    "stop trading",
]
NONE = [
    "havent we stopped all tradings ?",
    "don't stop the exit monitor",
    "why did you stop?",
    "the stop loss is wrong",
    "I did not decide to pause trades unpause them dude",
    "I want you to stop the trading",
    "",
    "hello",
    "continue",            # everyday "keep going with your answer" — chat, never a resume
    "continue please",
]


@pytest.mark.parametrize("text", FULL)
def test_full_stop(text):
    i = owner_stop_intent(text)
    assert i is not None and i.kind == "full_stop", text


@pytest.mark.parametrize("text", SCOPED)
def test_scoped(text):
    i = owner_stop_intent(text)
    assert i is not None and i.kind == "scoped", text


@pytest.mark.parametrize("text", NONE)
def test_none(text):
    assert owner_stop_intent(text) is None, text


def test_resume_variants():
    for t in ("resume", "unpause", "ok resume everything", "resume all", "continue autonomy",
              "continue everything", "continue the goals"):
        assert owner_stop_intent(t).kind == "resume", t
    assert owner_stop_intent("resume trading").kind == "scoped"
    assert owner_stop_intent("continue") is None


def test_prose_duration_makes_a_timed_full_stop():
    i = owner_stop_intent("stop everything for 6 hours")
    assert i.kind == "full_stop" and i.duration_minutes == 360
    i = owner_stop_intent("pause for 90 min")
    assert i.kind == "full_stop" and i.duration_minutes == 90
    assert owner_stop_intent("stop").duration_minutes is None
    assert owner_stop_intent("stop for 0 minutes").duration_minutes is None


def test_owner_phrases_extend_the_object_free_set():
    assert owner_stop_intent("stop the bots", extra_phrases=("bots",)).kind == "full_stop"
    assert owner_stop_intent("stop the bots").kind == "scoped"


def test_voice_prefix_is_stripped_and_raw_keeps_the_text():
    assert strip_voice_prefix("[voice message, auto-transcribed] Stop") == "Stop"
    i = owner_stop_intent("[voice message, auto-transcribed] Stop")
    assert i.raw == "Stop"


def test_parse_pause_args():
    assert parse_pause_args([]) == (("all",), None)
    assert parse_pause_args(["trading", "streams"]) == (("trading", "streams"), None)
    assert parse_pause_args(["for", "6h"]) == (("all",), 360)
    assert parse_pause_args(["cron", "for", "90m"]) == (("cron",), 90)
    assert parse_pause_args(["for", "2d"]) == (("all",), 2880)
    assert parse_pause_args(["ALL"]) == (("all",), None)
    with pytest.raises(ValueError):
        parse_pause_args(["bogus"])
    with pytest.raises(ValueError):
        parse_pause_args(["for", "soon"])
    with pytest.raises(ValueError):
        parse_pause_args(["for", "0m"])
