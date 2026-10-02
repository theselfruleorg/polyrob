"""Releasing the latch EARLY, for one provider, when the credits came back.

⚠️ 2026-09-24. OpenRouter went to −$0.14 at 06:22Z and the sentinel latched it
for six hours. That window is right when nobody is watching — it is one paid
probe per window instead of a permanent halt. But when the owner tops the
account up at 09:30, the reason for the latch is GONE and Rob still sits dead
until 12:22Z for no reason at all.

The documented remedy was `rm <data>/CREDIT_SENTINEL`, which is wrong in a way
that only bites on a bad day: the latch is PER PROVIDER, so removing the file
also releases a provider that is still dead, and the next dispatch spends a
paid probe finding that out again. `release("openrouter")` drops exactly the
one whose account was refilled.

Everything here is fail-open, like the rest of the module: a latch we cannot
write is never allowed to become a latch that blocks forever.
"""
import json
import os

import pytest

from core import credit_sentinel as cs


@pytest.fixture()
def latch(tmp_path, monkeypatch):
    path = tmp_path / "CREDIT_SENTINEL"
    monkeypatch.setattr(cs, "_sentinel_path", lambda: str(path))
    monkeypatch.delenv("CREDIT_SENTINEL_ENABLED", raising=False)
    return path


def _write(path, providers):
    path.write_text(json.dumps({"providers": providers}))


def _fresh(reason="402"):
    import time
    return {"ts": time.time(), "release_ts": None, "reason": reason}


# --- the point ------------------------------------------------------------- #

def test_releasing_one_provider_leaves_the_other_latched(latch):
    _write(latch, {"openrouter": _fresh(), "zai-coding": _fresh()})
    assert cs.release("openrouter") is True
    assert cs.credit_sentinel_active("openrouter") is False
    assert cs.credit_sentinel_active("zai-coding") is True, \
        "a top-up on one account says nothing about the other"


def test_releasing_the_last_provider_removes_the_file(latch):
    _write(latch, {"openrouter": _fresh()})
    assert cs.release("openrouter") is True
    assert not os.path.exists(latch), "an empty latch file is a latch"


def test_releasing_everything_clears_the_latch(latch):
    _write(latch, {"openrouter": _fresh(), "zai-coding": _fresh()})
    assert cs.release() is True
    assert not os.path.exists(latch)
    assert cs.credit_sentinel_active() is False


def test_a_global_trip_is_released_by_name(latch):
    """An unattributable 402 latches under the global key; the operator who
    knows what happened must be able to clear it."""
    _write(latch, {cs.GLOBAL_KEY: _fresh()})
    assert cs.release(cs.GLOBAL_KEY) is True
    assert cs.credit_sentinel_active("openrouter") is False


# --- honest returns -------------------------------------------------------- #

def test_releasing_a_provider_that_is_not_latched_reports_nothing_done(latch):
    _write(latch, {"openrouter": _fresh()})
    assert cs.release("zai-coding") is False
    assert cs.credit_sentinel_active("openrouter") is True, "untouched"


def test_releasing_when_there_is_no_latch_reports_nothing_done(latch):
    assert cs.release("openrouter") is False
    assert cs.release() is False


def test_releasing_a_global_trip_by_provider_name_does_not_pretend(latch):
    """A global trip pauses `openrouter` too, but releasing "openrouter" has
    not cleared it — saying True there would send a caller off believing the
    provider is free when the next dispatch still refuses."""
    _write(latch, {cs.GLOBAL_KEY: _fresh()})
    assert cs.release("openrouter") is False
    assert cs.credit_sentinel_active("openrouter") is True


# --- fail-open ------------------------------------------------------------- #

def test_an_unreadable_latch_never_raises(latch, monkeypatch):
    _write(latch, {"openrouter": _fresh()})

    def _boom(*a, **kw):
        raise OSError("disk gone")

    monkeypatch.setattr(cs, "_read_latch", _boom)
    assert cs.release("openrouter") is False


def test_release_is_a_noop_when_the_sentinel_is_disabled(latch, monkeypatch):
    monkeypatch.setenv("CREDIT_SENTINEL_ENABLED", "false")
    _write(latch, {"openrouter": _fresh()})
    assert cs.release("openrouter") is False
    assert latch.exists(), "disabled means the module does not act at all"
