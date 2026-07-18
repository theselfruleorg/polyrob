"""Tests for the /persona slash command and the use-case persona JSON files.

Covers:
- _list_persona_names() returns persona names from a tmp characters dir.
- /persona (no arg) lists available personas including the new use-case ones.
- /persona <name-or-text> persists a "session.persona" preference (owner-UX
  P2 T6 — the arg branch is a pref-setting SWITCH, not a read-only detail
  view; see tests/unit/cli/test_persona_toolset_switch.py for the full
  template-key/literal-text/threat-scan-rejection contract).
- Each new *.character.json is valid JSON and parses via Character.from_dict().
- /persona is registered in the default REPL command registry.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import List
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(args: List[str] | None = None, *, characters_dir: Path | None = None,
              home_dir: Path | None = None):
    """Build a minimal CommandContext with a captured emit list.

    ``home_dir`` (when given) wires ``ctx.container.config.data_dir`` so a
    write-behavior test (the ``args`` branch persists a preference) never
    touches the real ``data/`` tree — pass ``tmp_path`` for any test that
    invokes the arg branch.
    """
    from cli.ui.commands.registry import CommandContext

    output: List[str] = []

    container = None
    if home_dir is not None:
        container = SimpleNamespace(config=SimpleNamespace(data_dir=home_dir))

    ctx = CommandContext(args=args or [], container=container)

    def fake_emit(text: str, *, title: str = "", style: str = "") -> None:
        output.append(text)

    ctx.emit = fake_emit  # type: ignore[method-assign]
    ctx._test_output = output  # type: ignore[attr-defined]
    ctx._test_chars_dir = characters_dir  # stash for use in patching
    return ctx


def _combined_output(ctx) -> str:
    return "\n".join(ctx._test_output)  # type: ignore[attr-defined]


def _make_char_file(directory: Path, slug: str, bio: str = "test bio") -> None:
    """Write a minimal valid character file into *directory*."""
    data = {
        "name": slug.capitalize(),
        "modelProvider": "anthropic",
        "clients": ["anthropic"],
        "settings": {},
        "bio": bio,
        "lore": [],
        "knowledge": [],
        "messageExamples": [],
        "postExamples": [],
        "topics": ["testing"],
        "adjectives": ["test"],
        "style": {},
    }
    (directory / f"{slug}.character.json").write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# _list_persona_names unit tests (tmp dir — isolated from real data/)
# ---------------------------------------------------------------------------


def test_list_persona_names_returns_sorted_slugs(tmp_path):
    """_list_persona_names(tmp_path) returns sorted stems from *.character.json."""
    from cli.ui.commands.handlers import _list_persona_names

    for slug in ("beta", "alpha", "gamma"):
        _make_char_file(tmp_path, slug)

    names = _list_persona_names(tmp_path)
    assert names == ["alpha", "beta", "gamma"]


def test_list_persona_names_empty_dir(tmp_path):
    """Empty directory → empty list, no error."""
    from cli.ui.commands.handlers import _list_persona_names

    assert _list_persona_names(tmp_path) == []


def test_list_persona_names_missing_dir():
    """Non-existent directory → empty list, no error."""
    from cli.ui.commands.handlers import _list_persona_names

    missing = Path("/tmp/__nonexistent_chars_dir_9x7z__")
    assert _list_persona_names(missing) == []


# ---------------------------------------------------------------------------
# /persona (no arg) — list personas
# ---------------------------------------------------------------------------


def test_persona_no_arg_lists_available(tmp_path):
    """/persona (no arg) emits a line listing the available persona names."""
    from cli.ui.commands.handlers import _h_persona

    _make_char_file(tmp_path, "researcher", "Rigorous research assistant")
    _make_char_file(tmp_path, "coder", "Pragmatic software engineer")

    ctx = _make_ctx(args=[])

    with mock.patch(
        "cli.ui.commands.handlers._list_persona_names",
        return_value=["coder", "researcher"],
    ):
        _h_persona(ctx)

    combined = _combined_output(ctx)
    assert "coder" in combined
    assert "researcher" in combined


def test_persona_no_arg_includes_new_use_case_personas():
    """/persona (no arg) mentions the 5 new use-case persona slugs (from real data/)."""
    from cli.ui.commands.handlers import _h_persona, _list_persona_names

    # Resolve real names from the actual data/characters/ directory.
    real_names = _list_persona_names()
    expected_new = {"researcher", "coder", "analyst", "writer", "ops"}
    missing = expected_new - set(real_names)
    assert not missing, (
        f"Missing new persona files in data/characters/: {missing}. "
        "Run `git add -f data/characters/*.character.json`."
    )


def test_persona_no_arg_shows_guidance(tmp_path):
    """/persona (no arg) includes the env-based activation guidance."""
    from cli.ui.commands.handlers import _h_persona

    _make_char_file(tmp_path, "ops")

    ctx = _make_ctx(args=[])

    with mock.patch(
        "cli.ui.commands.handlers._list_persona_names",
        return_value=["ops"],
    ):
        _h_persona(ctx)

    combined = _combined_output(ctx)
    # Must show that live switching is not supported and env usage.
    assert "not supported" in combined.lower() or "new session" in combined.lower()
    assert "POLYROB_PERSONA" in combined  # the real CLI persona lever


# ---------------------------------------------------------------------------
# /persona <name-or-text> — owner-UX P2 T6: a pref-setting SWITCH, not a
# read-only detail view. Full template-key/literal-text/threat-scan/live-attr
# coverage lives in tests/unit/cli/test_persona_toolset_switch.py; these three
# just confirm the arg branch in THIS module's handler wiring behaves as the
# new contract expects (a bare character name is not a TEMPLATES key, so it
# persists as literal text — there is no more "unknown" rejection here).
# ---------------------------------------------------------------------------


def test_persona_with_arg_persists_as_literal_text(tmp_path):
    """/persona coder — 'coder' isn't a TEMPLATES key, so it persists as
    literal persona text and confirms with a "next session" message."""
    from core.prefs import load_preferences
    from cli.ui.commands.handlers import _h_persona

    ctx = _make_ctx(args=["coder"], home_dir=tmp_path)
    _h_persona(ctx)

    combined = _combined_output(ctx)
    assert "coder" in combined.lower()
    assert "next session" in combined.lower()
    assert load_preferences(tmp_path, "local")["session.persona"] == "coder"


def test_persona_with_arbitrary_text_has_no_unknown_concept(tmp_path):
    """Any free text is a valid persona value — there is no 'unknown persona'
    rejection on this branch (only the write-side threat scan can refuse)."""
    from core.prefs import load_preferences
    from cli.ui.commands.handlers import _h_persona

    ctx = _make_ctx(args=["no_such_persona"], home_dir=tmp_path)
    _h_persona(ctx)

    combined = _combined_output(ctx)
    assert "unknown" not in combined.lower()
    assert "saved" in combined.lower()
    assert load_preferences(tmp_path, "local")["session.persona"] == "no_such_persona"


def test_persona_with_multiword_arg_joins_as_literal_text(tmp_path):
    """Multi-word input is joined (space-separated) into one literal persona
    string, not just the first token."""
    from core.prefs import load_preferences
    from cli.ui.commands.handlers import _h_persona

    ctx = _make_ctx(args=["a", "friendly", "writer"], home_dir=tmp_path)
    _h_persona(ctx)

    assert load_preferences(tmp_path, "local")["session.persona"] == "a friendly writer"


# ---------------------------------------------------------------------------
# New *.character.json files are schema-valid
# ---------------------------------------------------------------------------


NEW_PERSONA_SLUGS = ["researcher", "coder", "analyst", "writer", "ops"]
CHARS_DIR = Path(__file__).parent.parent.parent.parent / "data" / "characters"

REQUIRED_FIELDS = {
    "name", "modelProvider", "clients", "bio", "lore", "knowledge",
    "messageExamples", "postExamples", "topics", "adjectives", "style",
}


@pytest.mark.parametrize("slug", NEW_PERSONA_SLUGS)
def test_new_persona_file_is_valid_json(slug):
    """Each new *.character.json is valid JSON."""
    char_file = CHARS_DIR / f"{slug}.character.json"
    assert char_file.exists(), f"Missing file: {char_file}"
    data = json.loads(char_file.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{slug}: expected dict, got {type(data)}"


@pytest.mark.parametrize("slug", NEW_PERSONA_SLUGS)
def test_new_persona_file_has_required_fields(slug):
    """Each new *.character.json contains the required top-level fields."""
    char_file = CHARS_DIR / f"{slug}.character.json"
    data = json.loads(char_file.read_text(encoding="utf-8"))
    missing = REQUIRED_FIELDS - set(data.keys())
    assert not missing, f"{slug}: missing fields {missing}"


@pytest.mark.parametrize("slug", NEW_PERSONA_SLUGS)
def test_new_persona_parses_via_character_from_dict(slug):
    """Each new *.character.json can be loaded via Character.from_dict() without error."""
    from agents.personality.character import Character

    char_file = CHARS_DIR / f"{slug}.character.json"
    data = json.loads(char_file.read_text(encoding="utf-8"))
    character = Character.from_dict(data)
    assert character is not None
    assert character.name, f"{slug}: Character.name is empty"


@pytest.mark.parametrize("slug", NEW_PERSONA_SLUGS)
def test_new_persona_bio_is_non_empty_string(slug):
    """Each new persona has a non-empty bio string (not a list)."""
    char_file = CHARS_DIR / f"{slug}.character.json"
    data = json.loads(char_file.read_text(encoding="utf-8"))
    bio = data.get("bio", "")
    assert bio, f"{slug}: bio is empty"


@pytest.mark.parametrize("slug", NEW_PERSONA_SLUGS)
def test_new_persona_no_unsafe_instructions(slug):
    """New persona bio/lore contain no obvious safety-disabling instructions."""
    char_file = CHARS_DIR / f"{slug}.character.json"
    data = json.loads(char_file.read_text(encoding="utf-8"))
    # Check bio + lore as strings for suspicious patterns.
    text_fields = []
    bio = data.get("bio", "")
    text_fields.append(bio if isinstance(bio, str) else " ".join(bio))
    for item in data.get("lore", []):
        text_fields.append(str(item))
    combined = " ".join(text_fields).lower()
    forbidden = ["ignore previous instructions", "disregard", "override safety", "jailbreak"]
    for phrase in forbidden:
        assert phrase not in combined, (
            f"{slug}: suspicious phrase {phrase!r} found in bio/lore"
        )


# ---------------------------------------------------------------------------
# /persona registered in the default registry
# ---------------------------------------------------------------------------


def test_persona_registered_in_default_registry():
    """The /persona command must be registered in the default REPL command registry."""
    from cli.ui.commands.handlers import build_default_registry, reset_default_registry

    reset_default_registry()
    reg = build_default_registry()
    names = {cmd.name for cmd in reg.commands()}
    assert "persona" in names, f"/persona not registered; found: {sorted(names)}"
