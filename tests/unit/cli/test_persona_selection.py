"""F1/F2/F3/F13 — the persona SETTER must consume what the LISTER prints.

Before this contract, ``/persona`` unioned every tier of
``character_search_dirs()`` into its listing, then persisted the argument as::

    persisted = key_candidate if key_candidate in TEMPLATES else value

No character slug is a TEMPLATES key, so ``/persona researcher`` stored the
literal string ``"researcher"`` and ``resolve_cli_persona`` returned that single
word as the entire ``<identity>`` persona block — discarding the character the
operator picked, and printing ``persona saved``.

One resolution order now holds on every surface:
**template key -> character slug -> literal text.**
"""

import json

import pytest

from agents.personality.persona_render import render_persona_block


def _write_character(chars_dir, slug, *, name=None, bio="A test bio."):
    chars_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "name": name or slug.capitalize(),
        "modelProvider": "anthropic",
        "clients": [],
        "settings": {},
        "bio": bio,
        "lore": [],
        "knowledge": [],
        "messageExamples": [],
        "postExamples": [],
        "topics": ["testing"],
        "adjectives": ["precise"],
        "style": {"all": ["Be terse."]},
    }
    (chars_dir / f"{slug}.character.json").write_text(json.dumps(data), encoding="utf-8")
    return data


@pytest.fixture
def local_gate(monkeypatch):
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.delenv("TASK_PERSONALITY_BLOCK", raising=False)
    monkeypatch.delenv("POLYROB_PERSONA", raising=False)
    monkeypatch.delenv("PERSONALITY_DEFAULT_CHARACTER", raising=False)


@pytest.fixture
def chars_home(tmp_path, monkeypatch):
    """A data home whose characters/ dir is tier 1 of the search order."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    return tmp_path / "characters"


# ---------------------------------------------------------------------------
# F1 — POLYROB_PERSONA=<character slug> renders the CHARACTER, not the slug
# ---------------------------------------------------------------------------


def test_env_persona_character_slug_renders_the_character(local_gate, chars_home, monkeypatch):
    from agents.personality.persona_resolver import resolve_persona_sync

    data = _write_character(chars_home, "researcher", name="Researcher",
                            bio="Rigorous research assistant.")
    monkeypatch.setenv("POLYROB_PERSONA", "researcher")

    text = resolve_persona_sync()
    assert text == render_persona_block(data)
    assert text != "researcher"


def test_env_persona_unknown_value_stays_literal(local_gate, chars_home, monkeypatch):
    from agents.personality.persona_resolver import resolve_persona_sync

    _write_character(chars_home, "researcher")
    monkeypatch.setenv("POLYROB_PERSONA", "You are a terse pirate.")
    assert resolve_persona_sync() == "You are a terse pirate."


def test_env_persona_template_key_still_wins_over_a_same_named_character(
        local_gate, chars_home, monkeypatch):
    """Template keys keep priority — the order is template > character > literal."""
    from agents.personality.persona_resolver import resolve_persona_sync
    from agents.task.templates import resolve_template_persona

    _write_character(chars_home, "coding", bio="A decoy character.")
    monkeypatch.setenv("POLYROB_PERSONA", "coding")
    assert resolve_persona_sync() == resolve_template_persona("coding")


@pytest.mark.parametrize("hostile", ["../secrets", "a/b", "..", "", "  "])
def test_character_lookup_refuses_unsafe_slugs(hostile):
    """A slug is a filename component — never a path."""
    from agents.personality.persona_resolver import (
        character_persona_text, is_safe_character_slug,
    )

    assert not is_safe_character_slug(hostile)
    assert character_persona_text(hostile) is None


# ---------------------------------------------------------------------------
# F1 — the session.persona PREF resolves the same way
# ---------------------------------------------------------------------------


def test_pref_character_slug_renders_the_character(local_gate, chars_home, tmp_path):
    from core.prefs import write_preference
    from cli.persona import resolve_cli_persona

    data = _write_character(chars_home, "writer", name="Writer", bio="A careful writer.")
    home = tmp_path / "prefs"
    ok, err = write_preference(home, "u1", "session.persona", "writer")
    assert ok, err

    assert resolve_cli_persona(user_id="u1", home_dir=home) == render_persona_block(data)


def test_pref_literal_text_is_still_literal(local_gate, chars_home, tmp_path):
    from core.prefs import write_preference
    from cli.persona import resolve_cli_persona

    _write_character(chars_home, "writer")
    home = tmp_path / "prefs"
    write_preference(home, "u1", "session.persona", "You are a terse pirate.")
    assert resolve_cli_persona(user_id="u1", home_dir=home) == "You are a terse pirate."


def test_gate_off_still_returns_empty_for_a_character_slug(chars_home, tmp_path, monkeypatch):
    """The server path must stay byte-identical: gate off => ''."""
    from core.prefs import write_preference
    from cli.persona import resolve_cli_persona

    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.setenv("TASK_PERSONALITY_BLOCK", "false")
    _write_character(chars_home, "writer")
    home = tmp_path / "prefs"
    write_preference(home, "u1", "session.persona", "writer")
    assert resolve_cli_persona(user_id="u1", home_dir=home) == ""


# ---------------------------------------------------------------------------
# F1 — /persona <slug> reports what it actually did
# ---------------------------------------------------------------------------


def _ctx(args, home_dir):
    from types import SimpleNamespace
    from cli.ui.commands.registry import CommandContext

    container = SimpleNamespace(config=SimpleNamespace(data_dir=home_dir))
    ctx = CommandContext(args=args, container=container)
    out: list = []
    ctx.emit = lambda text, *, title="", style="": out.append(text)  # type: ignore
    ctx._out = out  # type: ignore
    return ctx


def test_persona_set_character_confirms_as_a_character(local_gate, chars_home, tmp_path):
    from core.prefs import load_preferences
    from cli.ui.commands.handlers import _h_persona

    _write_character(chars_home, "analyst", name="Analyst")
    home = tmp_path / "prefs"
    ctx = _ctx(["analyst"], home)
    _h_persona(ctx)

    combined = "\n".join(ctx._out)  # type: ignore
    assert "character" in combined.lower()
    assert "analyst" in combined
    assert load_preferences(home, "local")["session.persona"] == "analyst"


def test_persona_set_free_text_still_says_literal(local_gate, chars_home, tmp_path):
    from cli.ui.commands.handlers import _h_persona

    _write_character(chars_home, "analyst")
    ctx = _ctx(["a", "friendly", "writer"], tmp_path / "prefs")
    _h_persona(ctx)
    combined = "\n".join(ctx._out)  # type: ignore
    assert "saved" in combined.lower()
    assert "a friendly writer" in combined


# ---------------------------------------------------------------------------
# F2 + F3 + F13 — the listing
# ---------------------------------------------------------------------------


def test_persona_listing_names_the_character_flag(local_gate, chars_home, tmp_path):
    """F2: the guidance under a table of CHARACTERS must name the character knob."""
    from cli.ui.commands.handlers import _h_persona

    _write_character(chars_home, "analyst")
    ctx = _ctx([], tmp_path / "prefs")
    _h_persona(ctx)
    combined = "\n".join(ctx._out)  # type: ignore
    assert "PERSONALITY_DEFAULT_CHARACTER" in combined
    assert "POLYROB_PERSONA" in combined


def test_persona_listing_shows_bios_from_the_search_dirs(local_gate, chars_home, tmp_path,
                                                         monkeypatch):
    """F3: the bio column read a cwd-relative path and was blank everywhere."""
    from cli.ui.commands.handlers import _h_persona

    _write_character(chars_home, "analyst", bio="Reads numbers carefully.")
    monkeypatch.chdir(tmp_path)  # NOT the repo root — the old code found nothing here
    ctx = _ctx([], tmp_path / "prefs")
    _h_persona(ctx)
    assert "Reads numbers carefully." in "\n".join(ctx._out)  # type: ignore


def test_persona_listing_marks_the_active_character(local_gate, chars_home, tmp_path,
                                                    monkeypatch):
    """F13: nothing told the operator which persona was live."""
    from cli.ui.commands.handlers import _h_persona

    _write_character(chars_home, "analyst")
    _write_character(chars_home, "writer")
    monkeypatch.setenv("PERSONALITY_DEFAULT_CHARACTER", "writer")
    ctx = _ctx([], tmp_path / "prefs")
    _h_persona(ctx)
    combined = "\n".join(ctx._out)  # type: ignore
    assert "active" in combined.lower()


# ---------------------------------------------------------------------------
# F13 — doctor names the live character and its file
# ---------------------------------------------------------------------------


def test_doctor_setup_lines_name_the_active_character_and_path(
        local_gate, chars_home, monkeypatch):
    import os

    from cli.commands.doctor import setup_lines

    _write_character(chars_home, "writer", name="Writer")
    monkeypatch.setenv("PERSONALITY_DEFAULT_CHARACTER", "writer")
    lines = setup_lines(dict(os.environ))
    persona = [ln for ln in lines if ln.startswith("persona:")]
    assert persona, lines
    assert "writer" in persona[0]
    assert str(chars_home / "writer.character.json") in persona[0]


def test_doctor_setup_lines_report_the_gate_when_off(chars_home, monkeypatch):
    import os

    from cli.commands.doctor import setup_lines

    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.setenv("TASK_PERSONALITY_BLOCK", "false")
    lines = [ln for ln in setup_lines(dict(os.environ)) if ln.startswith("persona:")]
    assert lines and "off" in lines[0].lower()


# ---------------------------------------------------------------------------
# F13 — the banner names a non-default character
# ---------------------------------------------------------------------------


def test_banner_shows_a_named_character():
    from cli.ui.banner import banner_plain

    text = banner_plain(version="0.13.1", model="m", provider="p", tool_ids=["task"],
                        session_id="abcdef12", framework="polyrob", character="writer")
    assert "persona writer" in text


def test_banner_omits_the_neutral_default_character():
    from cli.ui.banner import banner_plain

    text = banner_plain(version="0.13.1", model="m", provider="p", tool_ids=["task"],
                        session_id="abcdef12", framework="polyrob", character="")
    assert "persona" not in text
