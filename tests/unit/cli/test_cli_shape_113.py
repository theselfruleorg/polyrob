"""113 §2 item 10 (T-08, T-09): one name per job on the command line.

`skill` folds under `skills`, `surface pause|resume` under `surfaces`, `autonomy halt`
and `owner halt` are hidden old names of `pause`; the old names keep working. One
`--json` and one `--yes`/`-y` (``cli/commands/_options.py``) for every command."""
from pathlib import Path

import click
from click.testing import CliRunner

_COMMANDS = Path(__file__).resolve().parents[3] / "cli" / "commands"


def test_skills_holds_the_install_verbs_and_skill_is_a_hidden_old_name():
    from cli.commands.skill_install import skill
    from cli.commands.skills import skills
    for name in ("install", "approve", "info", "remove", "tap", "search", "update"):
        assert skills.commands[name] is skill.commands[name]
    assert skills.commands["status"] is skill.commands["list"]
    assert skills.commands["list"] is not skill.commands["list"]  # the registered ids
    assert skill.hidden
    assert CliRunner().invoke(skill, ["install", "--help"]).exit_code == 0


def test_surfaces_holds_pause_resume_state_and_surface_is_hidden(monkeypatch):
    from cli.commands import surface as old
    from cli.commands.surfaces import surfaces
    assert surfaces.commands["pause"] is old.surface.commands["pause"]
    assert surfaces.commands["resume"] is old.surface.commands["resume"]
    assert surfaces.commands["state"] is old.surface.commands["list"]
    assert old.surface.hidden

    calls = []

    class _Store:
        def pause(self, sid):
            calls.append(("pause", sid))

        def resume(self, sid):
            calls.append(("resume", sid))

    monkeypatch.setattr(old, "_store", lambda write=None: _Store())
    monkeypatch.setattr(old, "_warn_if_breaker_inert", lambda: None)
    monkeypatch.setattr("cli.commands._bootstrap.ensure_env_loaded", lambda: None)
    assert CliRunner().invoke(surfaces, ["pause", "slack"]).exit_code == 0
    assert CliRunner().invoke(surfaces, ["resume", "slack"]).exit_code == 0
    assert calls == [("pause", "slack"), ("resume", "slack")]


def test_halt_is_a_hidden_old_name_of_pause():
    from cli.commands.autonomy import autonomy
    from cli.commands.owner import owner
    assert autonomy.commands["halt"].hidden and not autonomy.commands["pause"].hidden
    assert owner.commands["halt"].hidden
    for group in (autonomy, owner):
        help_text = CliRunner().invoke(group, ["--help"]).output
        assert "  halt " not in help_text


def test_one_spelling_of_json_and_yes():
    """A command module spells `--json` / `--yes` through `_options` only."""
    offenders = []
    for path in sorted(_COMMANDS.glob("*.py")):
        if path.name in ("_options.py", "chat.py"):
            continue
        text = path.read_text()
        for needle in ('click.option("--json"', 'click.option("--yes"'):
            if needle in text:
                offenders.append(f"{path.name}: {needle}")
    assert not offenders, offenders


def test_yes_option_has_the_short_form_and_a_dest():
    from cli.commands._options import json_option, yes_option

    @click.command()
    @yes_option(dest="assume_yes")
    @json_option
    def cmd(assume_yes, as_json):
        click.echo(f"{assume_yes} {as_json}")

    assert CliRunner().invoke(cmd, ["-y", "--json"]).output.strip() == "True True"
    assert CliRunner().invoke(cmd, []).output.strip() == "False False"
