"""Tests that resolve_cli_persona() (used by polyrob run) honours the gate and template."""
from cli.persona import resolve_cli_persona


def test_run_persona_empty_when_gate_off(monkeypatch):
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.delenv("TASK_PERSONALITY_BLOCK", raising=False)
    monkeypatch.setenv("POLYROB_PERSONA", "research")
    assert resolve_cli_persona() == ""


def test_run_persona_text_when_local(monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.delenv("TASK_PERSONALITY_BLOCK", raising=False)
    monkeypatch.setenv("POLYROB_PERSONA", "research")
    assert "research" in resolve_cli_persona().lower()


def test_run_persona_blank_template(monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.setenv("POLYROB_PERSONA", "blank")
    assert resolve_cli_persona() == ""
