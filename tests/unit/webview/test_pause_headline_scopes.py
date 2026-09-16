"""043 — the head truth tells the truth about a SCOPED pause.

A pause has scopes (031). ``/pause trading`` leaves everything else running,
and every other seat says so. The console's head truth rendered four states and
never read ``PauseState.scopes``, so a trading-only pause showed *"Rob is
paused."* on every screen — confidently wrong, on the one line that may never
be, about the one control an owner reaches for when something is going wrong.

The record is written directly under ``tmp_path`` rather than through
``autonomy_control.pause``: ``state_bases`` always appends the resolved data
home, so a write without isolating it lands in the developer's real one (see
``tests/conftest.py::_isolate_autonomy_pause_record``).
"""
import json
import time

import pytest

from core.autonomy_control import PAUSE_FILENAME


@pytest.fixture()
def headline(monkeypatch, tmp_path):
    import webview.pages_new as mod
    import webview.webgate as webgate
    monkeypatch.setattr(webgate, "data_dir", lambda: str(tmp_path))

    def _write(**record):
        record.setdefault("paused", True)
        record.setdefault("since", time.time() - 60)
        (tmp_path / PAUSE_FILENAME).write_text(json.dumps(record))
        return mod._pause_headline()

    return _write


def test_a_scoped_pause_names_its_scope(headline):
    assert "paused for trading" in headline(scopes=["trading"])


def test_a_pause_of_everything_does_not_say_for(headline):
    text = headline(scopes=["all"])
    assert "paused" in text
    assert " for " not in text


def test_two_scopes_are_both_named(headline):
    text = headline(scopes=["trading", "social"])
    assert "trading" in text and "social" in text


def test_a_scoped_pause_with_an_end_says_both(headline):
    text = headline(scopes=["trading"], until=time.time() + 600)
    assert "paused for trading" in text and "until" in text


def test_no_record_is_running(monkeypatch, tmp_path):
    import webview.pages_new as mod
    import webview.webgate as webgate
    monkeypatch.setattr(webgate, "data_dir", lambda: str(tmp_path))
    assert mod._pause_headline() == "Rob is running."


def test_an_unreadable_record_says_paused_and_why(monkeypatch, tmp_path):
    import webview.pages_new as mod
    import webview.webgate as webgate
    monkeypatch.setattr(webgate, "data_dir", lambda: str(tmp_path))
    (tmp_path / PAUSE_FILENAME).write_text("{not json")
    text = mod._pause_headline()
    assert "paused" in text and "could not read" in text


def test_the_words_are_the_terminals_own(headline):
    """One pause, one vocabulary. Two seats naming it differently is how an
    owner stops trusting either."""
    from core.surfaces.owner_admin import _scope_words
    assert _scope_words(("trading",)) in headline(scopes=["trading"])


# --- when a pause ends ------------------------------------------------------ #

#: A fixed moment, so "tomorrow" is a fact about the arithmetic rather than
#: about what time it happens to be when the suite runs. ⚠️ `time.time() + 26h`
#: lands on the day AFTER tomorrow between 22:00 and 24:00 UTC, which is a test
#: that passes for 22 hours a day.
_NOON = 1_700_000_000.0 - (1_700_000_000.0 % 86400) + 12 * 3600  # 12:00 UTC


def _frozen(monkeypatch, at):
    """Pin the renderer's idea of `now` — it reads the clock itself."""
    import webview.pages_new as mod
    real_gmtime = time.gmtime
    monkeypatch.setattr(mod.time, "gmtime",
                        lambda ts=None: real_gmtime(at if ts is None else ts))
    return mod


def test_a_pause_ending_today_shows_the_clock_only(monkeypatch):
    mod = _frozen(monkeypatch, _NOON)
    rendered = mod._when(_NOON + 3600)
    assert "UTC" in rendered and "tomorrow" not in rendered and " on " not in rendered


def test_a_pause_ending_tomorrow_says_tomorrow(monkeypatch):
    mod = _frozen(monkeypatch, _NOON)
    assert "tomorrow" in mod._when(_NOON + 26 * 3600)


def test_the_late_evening_hour_does_not_change_the_answer(monkeypatch):
    """23:30 UTC + 26h is the day after tomorrow, and must render as a DATE
    rather than silently as "tomorrow"."""
    late = _NOON + 11 * 3600 + 30 * 60
    mod = _frozen(monkeypatch, late)
    assert "tomorrow" in mod._when(late + 12 * 3600)
    assert " on " in mod._when(late + 30 * 3600)


def test_a_pause_ending_next_week_carries_its_date(monkeypatch):
    mod = _frozen(monkeypatch, _NOON)
    assert " on " in mod._when(_NOON + 7 * 86400)


def test_an_unusable_moment_is_a_question_mark():
    import webview.pages_new as mod
    assert mod._when(None) == "?"
    assert mod._when("soon") == "?"
