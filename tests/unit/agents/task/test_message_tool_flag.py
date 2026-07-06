"""Unit tests for the ``MESSAGE_TOOL_ENABLED`` flag helper.

``message_tool_enabled()`` defaults to ``local_mode_enabled()`` because
``MESSAGE_TOOL_ENABLED`` is in ``_SAFE_LOCAL_FLAGS`` (via ``_safe_autonomy_default``).
An explicit ``MESSAGE_TOOL_ENABLED`` env value always wins over the local-mode default.
"""

from agents.task.constants import message_tool_enabled


def test_off_by_default(monkeypatch):
    for k in ("MESSAGE_TOOL_ENABLED", "POLYROB_LOCAL", "ROB_LOCAL"):
        monkeypatch.delenv(k, raising=False)
    assert message_tool_enabled() is False


def test_explicit_on(monkeypatch):
    for k in ("POLYROB_LOCAL", "ROB_LOCAL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MESSAGE_TOOL_ENABLED", "true")
    assert message_tool_enabled() is True


def test_on_under_local(monkeypatch):
    monkeypatch.delenv("MESSAGE_TOOL_ENABLED", raising=False)
    monkeypatch.setenv("POLYROB_LOCAL", "true")
    assert message_tool_enabled() is True


def test_explicit_off_beats_local(monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL", "true")
    monkeypatch.setenv("MESSAGE_TOOL_ENABLED", "false")
    assert message_tool_enabled() is False
