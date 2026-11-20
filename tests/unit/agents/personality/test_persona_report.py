"""`/persona` on a chat seat: the voice the running agent uses, read only."""
import json

import pytest

from agents.personality.persona_report import persona_report


@pytest.fixture
def home(monkeypatch, tmp_path):
    chars = tmp_path / "characters"
    chars.mkdir()
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.delenv("TASK_PERSONALITY_BLOCK", raising=False)
    monkeypatch.delenv("POLYROB_PERSONA", raising=False)
    return chars


def _write(chars, slug, data):
    (chars / f"{slug}.character.json").write_text(json.dumps(data))


def test_names_the_file_and_shows_what_the_model_receives(home, monkeypatch):
    _write(home, "nova", {"name": "Nova", "bio": "A dry test agent.", "knowledge": ["k"]})
    monkeypatch.setenv("PERSONALITY_DEFAULT_CHARACTER", "nova")
    out = persona_report()
    assert "character 'nova'" in out
    assert str(home / "nova.character.json") in out
    assert "A dry test agent." in out
    assert "NOT rendered" in out and "knowledge" in out


def test_gate_off_says_so(home, monkeypatch):
    monkeypatch.setenv("TASK_PERSONALITY_BLOCK", "false")
    assert "OFF" in persona_report()


def test_a_missing_character_names_the_fallback(home, monkeypatch):
    _write(home, "other", {"name": "Other"})
    monkeypatch.setenv("PERSONALITY_DEFAULT_CHARACTER", "ghost")
    out = persona_report()
    assert "'ghost' is not in any characters directory" in out


def test_a_literal_persona_override_is_named(home, monkeypatch):
    monkeypatch.setenv("POLYROB_PERSONA", "Speak like a lighthouse keeper.")
    out = persona_report()
    assert "the operator's setting" in out and "lighthouse keeper" in out
    assert "POLYROB_PERSONA" not in out  # owner copy names the verb, never an env flag


def test_telegram_routes_persona_to_the_shared_body():
    from surfaces.telegram.admin_verbs import handler_for
    assert handler_for("/persona") is not None
    from core.verbs import room_refused, verb_for
    row = verb_for("/persona")
    assert row.runs_on("telegram") and row.runs_on("repl")
    assert room_refused("/persona")  # the owner's voice settings stay out of a room
    from core.verbs import owner_gated_names, routed_core_names
    assert "/persona" in routed_core_names()  # a seat-local row is never routed
    assert "/persona" in owner_gated_names()  # only the owner reads it


def test_the_report_names_the_verb_never_an_env_flag(home, monkeypatch):
    _write(home, "nova", {"name": "Nova"})
    monkeypatch.setenv("PERSONALITY_DEFAULT_CHARACTER", "nova")
    out = persona_report()
    assert "character 'nova'" in out
    assert "/persona <name>" in out and "/persona reset" in out
    for flag in ("PERSONALITY_DEFAULT_CHARACTER", "POLYROB_PERSONA", "TASK_PERSONALITY_BLOCK"):
        assert flag not in out
    monkeypatch.setenv("TASK_PERSONALITY_BLOCK", "false")
    assert "TASK_PERSONALITY_BLOCK" not in persona_report()
