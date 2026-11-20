"""`polyrob doctor` names the filled character fields the model never sees.

Before, the drop reached only the log; the owner looks at doctor."""
import json


def _setup(monkeypatch, tmp_path, char):
    chars = tmp_path / "characters"
    chars.mkdir()
    (chars / "nova.character.json").write_text(json.dumps(char))
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PERSONALITY_DEFAULT_CHARACTER", "nova")
    monkeypatch.delenv("POLYROB_PERSONA", raising=False)


def test_doctor_line_names_a_stored_only_field(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, {"name": "Nova", "bio": "b", "knowledge": ["k"]})
    from cli.persona import active_persona_line
    line = active_persona_line(None, tmp_path, gate=True)
    assert "character 'nova'" in line
    assert "NOT rendered" in line and "knowledge" in line


def test_doctor_line_is_one_line_when_everything_renders(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path, {"name": "Nova", "bio": "b", "postExamples": ["p"]})
    from cli.persona import active_persona_line
    line = active_persona_line(None, tmp_path, gate=True)
    assert "character 'nova'" in line
    assert "\n" not in line
