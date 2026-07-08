"""T1-07 (2026-07-06 structural review): two live persona SSOTs split by surface.

The chat_once path rendered the character JSON while the CLI rendered the
templates.py persona — same instance, contradictory voice (and the trading
template's "Never executes trades" line shipped only on CLI). One resolver now
serves every surface with a single precedence:

  1. gate: TASK_PERSONALITY_BLOCK off -> ""
  2. explicit POLYROB_PERSONA (template key -> template persona; other text ->
     literal free-form persona)
  3. the default character, rendered via render_persona_block
  4. ""
"""
import json

import pytest

from agents.personality.persona_resolver import (
    resolve_persona,
    resolve_persona_sync,
)


@pytest.fixture(autouse=True)
def _persona_env(monkeypatch):
    monkeypatch.setenv("TASK_PERSONALITY_BLOCK", "true")
    monkeypatch.delenv("POLYROB_PERSONA", raising=False)
    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)


def test_gate_off_returns_empty(monkeypatch):
    monkeypatch.setenv("TASK_PERSONALITY_BLOCK", "false")
    assert resolve_persona_sync() == ""


def test_explicit_literal_persona_wins(monkeypatch):
    monkeypatch.setenv("POLYROB_PERSONA", "You are a terse pirate.")
    assert resolve_persona_sync() == "You are a terse pirate."


def test_template_key_renders_template_persona(monkeypatch):
    monkeypatch.setenv("POLYROB_PERSONA", "trading")
    out = resolve_persona_sync()
    assert "Never executes trades" in out


def test_character_fallback_when_no_explicit_persona(tmp_path, monkeypatch):
    # Point the data-dir character tier at a temp character set.
    chars = tmp_path / "characters"
    chars.mkdir()
    (chars / "rob.character.json").write_text(json.dumps({
        "name": "Rob", "bio": "Bold virtual concierge.",
        "adjectives": ["bold", "fearless"],
    }))
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    out = resolve_persona_sync()
    assert "Rob" in out
    assert "bold" in out


def test_async_resolver_prefers_container_character_manager(monkeypatch):
    class _Char:
        def to_dict(self):
            return {"name": "Rob", "bio": "Bold concierge.", "adjectives": ["bold"]}

    class _CM:
        async def get_default_character(self):
            return _Char()

    class _Container:
        def get_service(self, name):
            assert name == "character_manager"
            return _CM()

    import asyncio
    out = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        resolve_persona(container=_Container())
    )
    assert "Rob" in out and "bold" in out


def test_both_surfaces_delegate_to_the_resolver():
    import inspect

    import agents.task_agent_lite as tal
    import cli.persona as cp

    assert "persona_resolver" in inspect.getsource(tal)
    assert "persona_resolver" in inspect.getsource(cp)


def test_character_lines_are_not_self_sabotaging():
    # "voice can be bold without 'glitchy'" — the shipped character must not
    # instruct the agent to malfunction.
    from pathlib import Path

    src = Path("agents/personality/characters/rob.character.json").read_text().lower()
    assert "glitchy" not in src
    assert "borderline chaotic" not in src
