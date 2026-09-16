"""F4/F0 — `polyrob persona` is the authoring path that did not exist.

`polyrob soul init` scaffolds the SOUL and opens $EDITOR; nothing did the same
for the layer directly above it. To give an instance a character an operator had
to infer, unaided: the `<slug>.character.json` naming convention, the four-tier
directory precedence, the field set, and which of those fields actually render.

`persona init` mirrors `soul init` exactly; `persona list` shares the listing
`/persona` prints; `persona show` prints the rendered block the model receives.
"""

import json

import pytest
from click.testing import CliRunner


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An isolated config home + data home; the gate on, no persona chosen."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("POLYROB_HOME", str(h))
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.delenv("TASK_PERSONALITY_BLOCK", raising=False)
    monkeypatch.delenv("POLYROB_PERSONA", raising=False)
    monkeypatch.delenv("PERSONALITY_DEFAULT_CHARACTER", raising=False)
    return h


def _run(args, **kw):
    from cli.commands.persona import persona

    return CliRunner().invoke(persona, args, **kw)


# ---------------------------------------------------------------------------
# persona init
# ---------------------------------------------------------------------------


def test_init_scaffolds_sets_the_flag_and_prints_the_rendered_block(home):
    res = _run(["init", "mybot", "--no-edit"])
    assert res.exit_code == 0, res.output

    char = home / "characters" / "mybot.character.json"
    assert char.is_file(), res.output
    data = json.loads(char.read_text(encoding="utf-8"))
    assert data["name"]
    # The author must see the rendered/stored-only split where they will read it.
    assert "_rendered_fields" in data

    env = (home / ".env").read_text(encoding="utf-8")
    assert "PERSONALITY_DEFAULT_CHARACTER=mybot" in env

    # It prints what the model will actually receive.
    from agents.personality.persona_render import render_persona_block
    block = render_persona_block(data)
    assert block
    assert block.splitlines()[0] in res.output


def test_init_from_a_preset_copies_its_fields(home):
    res = _run(["init", "mybot", "--from", "writer", "--no-edit"])
    assert res.exit_code == 0, res.output

    data = json.loads((home / "characters" / "mybot.character.json").read_text())
    preset = json.loads(
        (__import__("pathlib").Path("data/characters/writer.character.json")).read_text()
    )
    assert data["topics"] == preset["topics"]
    assert data["name"] == "Mybot"  # the copy is renamed to the new slug


def test_init_refuses_an_unknown_preset(home):
    res = _run(["init", "mybot", "--from", "no_such_preset", "--no-edit"])
    assert res.exit_code != 0
    assert "no_such_preset" in res.output


@pytest.mark.parametrize("bad", ["../escape", "a/b", ".."])
def test_init_refuses_an_unsafe_slug(home, bad):
    res = _run(["init", bad, "--no-edit"])
    assert res.exit_code != 0
    assert not list((home / "characters").glob("*.json")) if (home / "characters").exists() else True


def test_init_refuses_to_clobber_without_force(home):
    assert _run(["init", "mybot", "--no-edit"]).exit_code == 0
    res = _run(["init", "mybot", "--no-edit"])
    assert res.exit_code != 0
    assert "--force" in res.output
    assert _run(["init", "mybot", "--no-edit", "--force"]).exit_code == 0


# ---------------------------------------------------------------------------
# persona list / show
# ---------------------------------------------------------------------------


def test_list_shows_both_namespaces_and_the_active_marker(home, monkeypatch):
    _run(["init", "mybot", "--no-edit"])
    monkeypatch.setenv("PERSONALITY_DEFAULT_CHARACTER", "mybot")
    res = _run(["list"])
    assert res.exit_code == 0, res.output
    assert "mybot" in res.output
    assert "coding" in res.output          # a template key
    assert "PERSONALITY_DEFAULT_CHARACTER" in res.output
    assert "active" in res.output.lower()


def test_show_a_named_character_prints_its_block(home):
    res = _run(["show", "writer"])
    assert res.exit_code == 0, res.output
    assert "Writer" in res.output


def test_show_names_the_fields_that_will_not_render(home):
    res = _run(["show", "writer"])
    # writer.character.json populates `knowledge`, which never reaches the model.
    assert "knowledge" in res.output


def test_show_without_a_slug_describes_the_active_persona(home, monkeypatch):
    _run(["init", "mybot", "--no-edit"])
    monkeypatch.setenv("PERSONALITY_DEFAULT_CHARACTER", "mybot")
    res = _run(["show"])
    assert res.exit_code == 0, res.output
    assert "mybot" in res.output
    assert str(home / "characters" / "mybot.character.json") in res.output


def test_show_an_unknown_character_fails_loudly(home):
    res = _run(["show", "no_such_character"])
    assert res.exit_code != 0
    assert "no_such_character" in res.output


# ---------------------------------------------------------------------------
# reachable from the top-level CLI
# ---------------------------------------------------------------------------


def test_persona_is_a_lazy_subcommand():
    from cli.polyrob import _LAZY_SUBCOMMANDS

    assert _LAZY_SUBCOMMANDS.get("persona") == "cli.commands.persona:persona"


# ---------------------------------------------------------------------------
# F0 — `polyrob init` must OFFER a character; seven manual steps, three of them
# in no --help output, was the whole reason this audit exists.
# ---------------------------------------------------------------------------


def test_init_offers_a_character_and_scaffolds_it(home, tmp_path, monkeypatch):
    from cli.commands.init import init_cmd

    monkeypatch.chdir(tmp_path)
    # sections: keys(skip) -> model -> toolset -> template -> CHARACTER -> owner -> autonomy
    res = CliRunner().invoke(
        init_cmd, ["--skip-keys", "--no-prompt", "--character", "mybot"],
    )
    assert res.exit_code == 0, res.output
    char = home / "characters" / "mybot.character.json"
    assert char.is_file(), res.output
    assert "PERSONALITY_DEFAULT_CHARACTER=mybot" in (home / ".env").read_text()


def test_init_character_can_copy_a_preset(home, tmp_path, monkeypatch):
    from cli.commands.init import init_cmd

    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(
        init_cmd,
        ["--skip-keys", "--no-prompt", "--character", "mybot", "--character-from", "coder"],
    )
    assert res.exit_code == 0, res.output
    data = json.loads((home / "characters" / "mybot.character.json").read_text())
    assert data["topics"]
    assert data["name"] == "Mybot"


def test_init_without_the_flag_writes_no_character(home, tmp_path, monkeypatch):
    """Default is unchanged — a non-interactive init must stay byte-identical."""
    from cli.commands.init import init_cmd

    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(init_cmd, ["--skip-keys", "--no-prompt"])
    assert res.exit_code == 0, res.output
    assert not (home / "characters").exists()
    assert "PERSONALITY_DEFAULT_CHARACTER" not in (home / ".env").read_text()


def test_init_names_the_persona_next_steps(home, tmp_path, monkeypatch):
    """The wizard must point at the commands, not leave them to be inferred."""
    from cli.commands.init import init_cmd

    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(init_cmd, ["--skip-keys", "--no-prompt"])
    assert "polyrob persona init" in res.output
    assert "polyrob soul init" in res.output
