"""Coherency review 2026-10-13 E-A11: <memory-system> must not claim
cross-session recall when MEMORY_BACKEND=none registers no provider."""

import pytest

from agents.task.agent.prompts import SystemPrompt


def _text():
    return SystemPrompt.__new__(SystemPrompt)._get_memory_system_content()


@pytest.mark.parametrize("value", ["none", "off", ""])
def test_recall_off_is_said_and_not_claimed(monkeypatch, value):
    monkeypatch.setenv("MEMORY_BACKEND", value)
    text = _text()
    assert "Cross-session recall finds" not in text
    assert "OFF on this deployment" in text


@pytest.mark.parametrize("value", ["sqlite", "local_vector", "SQLite "])
def test_recall_on_keeps_the_claim(monkeypatch, value):
    monkeypatch.setenv("MEMORY_BACKEND", value)
    text = _text()
    assert "Cross-session recall finds" in text
    assert "OFF on this deployment" not in text
