"""The prompt-cache TTL seam and its session-class decision — F4 (2026-09-22).

``modules/llm/cache_hints.py`` owns the stamp + the reader; the decision lives in
``agents/task/session_class.py`` because ``modules`` may not import ``agents``
(the same split as the 057 output budget).
"""

import pytest

from modules.llm.cache_hints import (
    apply_cache_ttl,
    cache_control_marker,
    cache_ttl_for,
    cache_ttl_setting,
    resolve_cache_ttl,
)


class _Adapter:
    def __init__(self, client=None):
        self._client = client


class _Client:
    pass


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


def test_stamp_lands_on_adapter_and_client():
    client = _Client()
    adapter = _Adapter(client)
    assert apply_cache_ttl(adapter, "1h") == "1h"
    assert cache_ttl_for(adapter) == "1h"
    assert cache_ttl_for(client) == "1h"


def test_five_minutes_reads_back_as_no_ttl():
    """5m IS the API default, so the reader hands back None and the client emits
    the bare marker — byte-identical to the pre-F4 request."""
    client = _Client()
    apply_cache_ttl(client, "5m")
    assert cache_ttl_for(client) is None


def test_unstamped_and_garbage_are_no_ops():
    assert cache_ttl_for(None) is None
    assert cache_ttl_for(_Client()) is None
    assert apply_cache_ttl(None, "1h") is None
    assert apply_cache_ttl(_Client(), "10m") is None


def test_marker_shape():
    assert cache_control_marker() == {"type": "ephemeral"}
    assert cache_control_marker("5m") == {"type": "ephemeral"}
    assert cache_control_marker("1h") == {"type": "ephemeral", "ttl": "1h"}


def test_session_class_wrapper_uses_the_marker(monkeypatch):
    from agents.task import session_class

    from agents.task.goals import autonomy_marker

    monkeypatch.delenv("ANTHROPIC_CACHE_TTL", raising=False)
    monkeypatch.setattr(autonomy_marker, "is_autonomous", lambda sid: False)
    client = _Client()
    assert session_class.apply_session_cache_ttl(client, "sess-1") == "1h"
    assert cache_ttl_for(client) == "1h"

    monkeypatch.setattr(autonomy_marker, "is_autonomous", lambda sid: True)
    cron = _Client()
    assert session_class.apply_session_cache_ttl(cron, "cron-1") == "5m"
    assert cache_ttl_for(cron) is None


def test_session_class_cache_ttl_fails_open_to_interactive(monkeypatch):
    """The cache TTL is a BUDGET reading: an unreadable marker means the chat
    window (1h), not the autonomous one — only security gates fail closed."""
    from agents.task import session_class
    from agents.task.goals import autonomy_marker

    def _boom(sid):
        raise RuntimeError("marker unreadable")

    monkeypatch.delenv("ANTHROPIC_CACHE_TTL", raising=False)
    monkeypatch.setattr(autonomy_marker, "is_autonomous", _boom)
    assert session_class.budget_class_autonomous("sess-x") is False
    assert session_class.is_autonomous_session("sess-x") is True
    client = _Client()
    assert session_class.apply_session_cache_ttl(client, "sess-x") == "1h"
