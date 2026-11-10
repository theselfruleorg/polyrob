"""The prompt-cache TTL seam and its session-class decision — F4 (2026-09-22).

``modules/llm/cache_hints.py`` owns the scope + the reader; the decision lives in
``agents/task/session_class.py`` because ``modules`` may not import ``agents``
(the same split as the 057 output budget).
"""

import asyncio

import pytest

from modules.llm.call_scope import set_cache_ttl
from modules.llm.cache_hints import (
    apply_cache_ttl,
    cache_control_marker,
    cache_ttl_for,
    cache_ttl_setting,
    resolve_cache_ttl,
)


@pytest.fixture(autouse=True)
def _clean_scope():
    set_cache_ttl(None)
    yield
    set_cache_ttl(None)


def test_setting_defaults_to_auto(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_CACHE_TTL", raising=False)
    assert cache_ttl_setting() == "auto"


@pytest.mark.parametrize("raw", ["5m", "1H", " 1h ", "5M"])
def test_setting_reads_explicit_values(monkeypatch, raw):
    monkeypatch.setenv("ANTHROPIC_CACHE_TTL", raw)
    assert cache_ttl_setting() == raw.strip().lower()


@pytest.mark.parametrize("raw", ["", "10m", "nonsense", "true"])
def test_unknown_setting_falls_back_to_auto(monkeypatch, raw):
    monkeypatch.setenv("ANTHROPIC_CACHE_TTL", raw)
    assert cache_ttl_setting() == "auto"


def test_auto_is_one_hour_interactive_five_minutes_autonomous(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_CACHE_TTL", raising=False)
    assert resolve_cache_ttl(autonomous=False) == "1h"
    assert resolve_cache_ttl(autonomous=True) == "5m"


@pytest.mark.parametrize("override,expected", [("5m", "5m"), ("1h", "1h")])
def test_explicit_override_wins_over_session_class(monkeypatch, override, expected):
    monkeypatch.setenv("ANTHROPIC_CACHE_TTL", override)
    assert resolve_cache_ttl(autonomous=False) == expected
    assert resolve_cache_ttl(autonomous=True) == expected


def test_one_hour_reads_back():
    assert apply_cache_ttl("1h") == "1h"
    assert cache_ttl_for() == "1h"


def test_five_minutes_reads_back_as_no_ttl():
    """5m IS the API default, so the reader hands back None and the client emits
    the bare marker — byte-identical to the pre-F4 request."""
    apply_cache_ttl("5m")
    assert cache_ttl_for() is None


def test_unscoped_and_garbage_are_no_ops():
    assert cache_ttl_for() is None
    assert apply_cache_ttl("10m") is None
    assert cache_ttl_for() is None


def test_a_cron_run_and_a_chat_keep_their_own_ttl():
    """S4: two tasks on one client read their own window."""
    async def _session(ttl, gate_set, gate_wait):
        apply_cache_ttl(ttl)
        gate_set.set()
        await gate_wait.wait()
        return cache_ttl_for()

    async def _run():
        chat_set, cron_set = asyncio.Event(), asyncio.Event()
        return await asyncio.gather(_session("1h", chat_set, cron_set), _session("5m", cron_set, chat_set))

    assert asyncio.run(_run()) == ["1h", None]


def test_marker_shape():
    assert cache_control_marker() == {"type": "ephemeral"}
    assert cache_control_marker("5m") == {"type": "ephemeral"}
    assert cache_control_marker("1h") == {"type": "ephemeral", "ttl": "1h"}


def test_session_class_wrapper_uses_the_marker(monkeypatch):
    from agents.task import session_class

    from core import autonomy_marker

    monkeypatch.delenv("ANTHROPIC_CACHE_TTL", raising=False)
    monkeypatch.setattr(autonomy_marker, "is_autonomous", lambda sid: False)
    assert session_class.apply_session_cache_ttl("sess-1") == "1h"
    assert cache_ttl_for() == "1h"

    monkeypatch.setattr(autonomy_marker, "is_autonomous", lambda sid: True)
    assert session_class.apply_session_cache_ttl("cron-1") == "5m"
    assert cache_ttl_for() is None


def test_session_class_cache_ttl_fails_open_to_interactive(monkeypatch):
    """The cache TTL is a BUDGET reading: an unreadable marker means the chat
    window (1h), not the autonomous one — only security gates fail closed."""
    from agents.task import session_class
    from core import autonomy_marker

    def _boom(sid):
        raise RuntimeError("marker unreadable")

    monkeypatch.delenv("ANTHROPIC_CACHE_TTL", raising=False)
    monkeypatch.setattr(autonomy_marker, "is_autonomous", _boom)
    assert session_class.budget_class_autonomous("sess-x") is False
    assert session_class.is_autonomous_session("sess-x") is True
    assert session_class.apply_session_cache_ttl("sess-x") == "1h"
