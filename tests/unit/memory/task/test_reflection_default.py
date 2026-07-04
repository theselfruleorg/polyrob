"""UP-09 Step 9.1 — reflection gate is a single, working source of truth.

Historical bug (Fusion-validated 2026-06-16): the runtime guard read
`BotConfig.get("REFLECTION_LLM_ENABLED", False)` where `BotConfig.get` is
`getattr(self, key, default)` and BotConfig has no such attribute => ALWAYS False,
while construction.py read `os.getenv` (a different source). Reflection never fired.

Fix: one helper `constants.reflection_llm_enabled_default()` read by both sites,
default ON, falsey-disable {none, off, false, 0, no, ''}.
"""
import os

from agents.task.constants import reflection_llm_enabled_default


def _set(monkeypatch, val):
    if val is None:
        monkeypatch.delenv("REFLECTION_LLM_ENABLED", raising=False)
    else:
        monkeypatch.setenv("REFLECTION_LLM_ENABLED", val)


def test_default_on_when_unset(monkeypatch):
    _set(monkeypatch, None)
    assert reflection_llm_enabled_default() is True


def test_explicit_true(monkeypatch):
    _set(monkeypatch, "true")
    assert reflection_llm_enabled_default() is True


def test_falsey_disables(monkeypatch):
    for val in ("off", "none", "false", "0", "no", ""):
        _set(monkeypatch, val)
        assert reflection_llm_enabled_default() is False, val


def test_case_insensitive(monkeypatch):
    _set(monkeypatch, "OFF")
    assert reflection_llm_enabled_default() is False
    _set(monkeypatch, "True")
    assert reflection_llm_enabled_default() is True
