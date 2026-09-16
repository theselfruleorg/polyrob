"""F6/F7/F8/F10/F11 — five places the CLI said something untrue or nothing at all.

None of these change what the runtime does. Each one changes what the operator is
told about it, which is why every one of them survived a release: the behaviour
was right and the report was wrong.
"""

import os
from pathlib import Path

import pytest
from click.testing import CliRunner


# ---------------------------------------------------------------------------
# F6 — the flags catalog documents TASK_PERSONALITY_BLOCK's default as OFF.
# The live seam returns local_mode_enabled(): ON for every local CLI user.
# `config get` and `config list` read the catalog, so the CLI stated the
# opposite of the runtime to anyone debugging a persona.
# ---------------------------------------------------------------------------


def _catalog_row(name: str):
    from core.flags_catalog import CATALOG

    for row in CATALOG:
        if row[0] == name:
            return row
    raise AssertionError(f"{name} not in FLAGS catalog")


def test_personality_gate_default_text_matches_the_seam():
    default_text = _catalog_row("TASK_PERSONALITY_BLOCK")[2]
    assert "OFF (`'false'`)" != default_text
    lowered = default_text.lower()
    assert "polyrob_local" in lowered and "server" in lowered, default_text


@pytest.mark.parametrize("local,expected", [("1", True), ("false", False)])
def test_personality_gate_seam_is_local_mode(monkeypatch, local, expected):
    from agents.task.constants import task_personality_block_enabled

    monkeypatch.delenv("TASK_PERSONALITY_BLOCK", raising=False)
    monkeypatch.setenv("POLYROB_LOCAL", local)
    assert task_personality_block_enabled() is expected


def test_configuration_md_and_catalog_agree():
    """The catalog is GENERATED from docs/CONFIGURATION.md — they must not drift."""
    repo = Path(__file__).resolve().parents[3]
    doc = (repo / "docs" / "CONFIGURATION.md").read_text(encoding="utf-8")
    row = [ln for ln in doc.splitlines()
           if ln.startswith("| `TASK_PERSONALITY_BLOCK`")]
    assert row, "TASK_PERSONALITY_BLOCK row missing from docs/CONFIGURATION.md"
    assert _catalog_row("TASK_PERSONALITY_BLOCK")[2] in row[0]


# ---------------------------------------------------------------------------
# F7 — `profile adopt` from a folder with nothing to adopt printed no "Copied:"
# line and no negative. "Created profile X" + "Pinned this folder to it" reads
# as though an existing bot was adopted.
# ---------------------------------------------------------------------------


def test_adopt_with_nothing_to_copy_says_so(tmp_path, monkeypatch):
    from cli.commands.profile import profile

    monkeypatch.setenv("POLYROB_HOME", str(tmp_path / "home"))
    proj = tmp_path / "proj"
    (proj / ".polyrob" / "sessions").mkdir(parents=True)
    (proj / ".polyrob" / "memory.db").write_text("", encoding="utf-8")
    monkeypatch.chdir(proj)

    res = CliRunner().invoke(profile, ["adopt", "blank"])
    assert res.exit_code == 0, res.output
    assert "Adopted nothing" in res.output
    assert "persona init" in res.output and "soul init" in res.output


def test_adopt_with_an_identity_still_reports_what_it_copied(tmp_path, monkeypatch):
    from cli.commands.profile import profile

    monkeypatch.setenv("POLYROB_HOME", str(tmp_path / "home"))
    proj = tmp_path / "proj"
    (proj / ".polyrob" / "identity").mkdir(parents=True)
    (proj / ".polyrob" / "identity" / "identity.md").write_text("# me", encoding="utf-8")
    monkeypatch.chdir(proj)

    res = CliRunner().invoke(profile, ["adopt", "real"])
    assert res.exit_code == 0, res.output
    assert "Copied:" in res.output
    assert "Adopted nothing" not in res.output


def test_adopt_mentions_include_data_when_sidecar_dbs_are_present(tmp_path, monkeypatch):
    from cli.commands.profile import profile

    monkeypatch.setenv("POLYROB_HOME", str(tmp_path / "home"))
    proj = tmp_path / "proj"
    (proj / ".polyrob").mkdir(parents=True)
    (proj / ".polyrob" / "memory.db").write_text("", encoding="utf-8")
    monkeypatch.chdir(proj)

    res = CliRunner().invoke(profile, ["adopt", "dbs"])
    assert res.exit_code == 0, res.output
    assert "--include-data" in res.output


# ---------------------------------------------------------------------------
# F8 — `config set --global` resolves through polyrob_home(), which profile
# activation repoints, so it writes to the ACTIVE PROFILE's .env. The behaviour
# is right; the help text said "~/.polyrob/.env" unconditionally.
# ---------------------------------------------------------------------------


def test_global_help_text_says_the_target_moves_with_the_profile():
    from cli.commands.config import config

    for sub in ("set", "unset"):
        help_text = CliRunner().invoke(config, [sub, "--help"]).output
        assert "POLYROB_HOME" in help_text, (sub, help_text)
        assert "profile" in help_text.lower(), (sub, help_text)


def test_home_is_an_alias_of_global():
    from cli.commands.config import set_cmd

    names = {opt.name for opt in set_cmd.params}
    assert "is_global" in names
    opt = next(o for o in set_cmd.params if o.name == "is_global")
    assert "--home" in opt.opts


# ---------------------------------------------------------------------------
# F10 — `soul init` always prompted, so the SOUL step could not be scripted,
# containerised, or driven from a profile-distribution install.
# ---------------------------------------------------------------------------


def test_soul_init_is_scriptable(tmp_path, monkeypatch):
    from cli.commands.soul import soul

    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    res = CliRunner().invoke(
        soul, ["init", "--name", "Aria", "--mission", "ship things", "--no-prompt",
               "--no-edit"],
        input="",  # no prompt may consume stdin
    )
    assert res.exit_code == 0, res.output
    identity = (tmp_path / "identity" / "identity.md").read_text(encoding="utf-8")
    assert "Aria" in identity
    assert "ship things" in identity


def test_soul_init_no_prompt_uses_defaults_without_flags(tmp_path, monkeypatch):
    from cli.commands.soul import soul
    from core.instance import resolve_instance_id

    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    res = CliRunner().invoke(soul, ["init", "--no-prompt", "--no-edit"], input="")
    assert res.exit_code == 0, res.output
    identity = (tmp_path / "identity" / "identity.md").read_text(encoding="utf-8")
    assert resolve_instance_id() in identity


# ---------------------------------------------------------------------------
# F11 — the profile alias wrapper was `exec polyrob -P <n>`, resolved through
# PATH. On a machine with a venv + an editable install + pipx it bound to
# whichever polyrob PATH happened to resolve, not the one that created it.
# ---------------------------------------------------------------------------


def test_alias_wrapper_pins_the_creating_interpreters_polyrob(tmp_path, monkeypatch):
    """The wrapper must name an absolute polyrob, not one PATH may re-resolve."""
    from cli.commands import profile as profile_mod

    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").write_text("", encoding="utf-8")
    console_script = venv_bin / "polyrob"
    console_script.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(profile_mod.sys, "executable", str(venv_bin / "python"))

    bin_dir = tmp_path / "bin"
    monkeypatch.setattr(profile_mod, "_bin_dir", lambda: bin_dir)

    body = profile_mod._write_wrapper("aria", "aria").read_text(encoding="utf-8")
    assert str(console_script) in body, body
    assert "-P aria" in body


def test_alias_wrapper_falls_back_to_the_bare_name(tmp_path, monkeypatch):
    """No console script beside this interpreter => the legacy PATH form, which
    must still be a working wrapper rather than a broken absolute path."""
    from cli.commands import profile as profile_mod

    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    monkeypatch.setattr(profile_mod.sys, "executable", str(venv_bin / "python"))
    monkeypatch.setattr(profile_mod, "_bin_dir", lambda: tmp_path / "bin")

    body = profile_mod._write_wrapper("aria", "aria").read_text(encoding="utf-8")
    assert "polyrob -P aria" in body
    assert str(venv_bin) not in body


def test_alias_wrapper_still_refuses_to_clobber_a_foreign_file(tmp_path, monkeypatch):
    from cli.commands import profile as profile_mod

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "aria").write_text("#!/bin/sh\necho not ours\n", encoding="utf-8")
    monkeypatch.setattr(profile_mod, "_bin_dir", lambda: bin_dir)

    with pytest.raises(Exception) as exc:
        profile_mod._write_wrapper("aria", "aria")
    assert "refusing" in str(exc.value)


# ---------------------------------------------------------------------------
# F13 rider — a standalone `polyrob persona`/`doctor` process never runs
# build_cli_container, so it never sees the os.environ.setdefault("POLYROB_LOCAL")
# that `polyrob run` and the REPL rely on. Reading the gate raw there reports
# "persona: off" over a persona that IS live in every session — the same
# confident-and-wrong shape F13 exists to kill.
# ---------------------------------------------------------------------------


def test_cli_gate_treats_an_absent_local_flag_as_on(monkeypatch):
    from cli.persona import cli_gate_on

    monkeypatch.delenv("TASK_PERSONALITY_BLOCK", raising=False)
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    assert cli_gate_on() is True


def test_cli_gate_honours_an_explicit_flag(monkeypatch):
    from cli.persona import cli_gate_on

    monkeypatch.setenv("TASK_PERSONALITY_BLOCK", "false")
    assert cli_gate_on() is False
    monkeypatch.setenv("TASK_PERSONALITY_BLOCK", "true")
    monkeypatch.setenv("POLYROB_LOCAL", "0")
    assert cli_gate_on() is True


def test_cli_gate_honours_an_explicit_local_off(monkeypatch):
    from cli.persona import cli_gate_on

    monkeypatch.delenv("TASK_PERSONALITY_BLOCK", raising=False)
    monkeypatch.setenv("POLYROB_LOCAL", "false")
    assert cli_gate_on() is False


def test_persona_list_reports_the_active_character_without_a_container(
        tmp_path, monkeypatch):
    """The clean-room symptom: `polyrob persona list` said the gate was off."""
    from cli.commands.persona import persona

    monkeypatch.setenv("POLYROB_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.delenv("TASK_PERSONALITY_BLOCK", raising=False)
    monkeypatch.delenv("POLYROB_PERSONA", raising=False)
    monkeypatch.setenv("PERSONALITY_DEFAULT_CHARACTER", "writer")

    res = CliRunner().invoke(persona, ["list"])
    assert res.exit_code == 0, res.output
    assert "active: character 'writer'" in res.output
    assert "TASK_PERSONALITY_BLOCK is off" not in res.output


def test_doctor_persona_line_uses_the_same_gate_rule(tmp_path, monkeypatch):
    from cli.commands.doctor import setup_lines

    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("POLYROB_LOCAL", raising=False)
    monkeypatch.delenv("TASK_PERSONALITY_BLOCK", raising=False)
    monkeypatch.delenv("POLYROB_PERSONA", raising=False)
    monkeypatch.delenv("PERSONALITY_DEFAULT_CHARACTER", raising=False)

    line = [ln for ln in setup_lines(dict(os.environ)) if ln.startswith("persona:")]
    assert line and "off" not in line[0].lower(), line
