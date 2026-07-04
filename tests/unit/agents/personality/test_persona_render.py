"""S1 — pure persona renderer + gated resolver.

render_persona_block turns a character dict (Character.to_dict()) into a terse
plain-text block. It is PURE (no Character construction, no container) so the
task-agent core never imports the chat stack — it only ever sees a `str`.
"""
import agents.task.constants as constants
from agents.personality.persona_render import render_persona_block, resolve_persona_block


ROB = {
    "name": "Rob",
    "bio": "I am Rob, a terse and witty automation agent.",
    "adjectives": ["terse", "witty", "precise"],
    "topics": ["automation", "crypto"],
    "style": {"all": ["Be concise.", "No fluff."]},
    "lore": ["Built to get things done."],
}


def test_render_includes_name_and_bio():
    out = render_persona_block(ROB)
    assert "Rob" in out
    assert "terse and witty" in out


def test_render_is_deterministic():
    assert render_persona_block(ROB) == render_persona_block(dict(ROB))


def test_render_empty_character_is_empty_string():
    assert render_persona_block({}) == ""
    assert render_persona_block(None) == ""


def test_resolve_gated_off_returns_empty(monkeypatch):
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.setenv("TASK_PERSONALITY_BLOCK", "false")
    assert resolve_persona_block(ROB) == ""


def test_resolve_gated_on_returns_rendered(monkeypatch):
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.setenv("TASK_PERSONALITY_BLOCK", "true")
    out = resolve_persona_block(ROB)
    assert "Rob" in out
    assert out == render_persona_block(ROB)


def test_resolve_on_but_no_character_is_empty(monkeypatch):
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.setenv("TASK_PERSONALITY_BLOCK", "true")
    assert resolve_persona_block(None) == ""
