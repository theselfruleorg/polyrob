"""Top-level help + machine-readable read commands (027 WP6).

`polyrob --help` printed 40+ commands flat and alphabetical with alias rows
(`models`, `sessions`, `webgate`) rendered as full duplicates and no "start
here"; doctor / model list / auth status had no --json despite 7 other
commands carrying one.
"""

import json
import os

from click.testing import CliRunner


def _help_output():
    from cli.polyrob import cli

    result = CliRunner().invoke(cli, ["--help"], catch_exceptions=False)
    assert result.exit_code == 0
    return result.output


def test_version_flag_works():
    from cli.polyrob import cli

    result = CliRunner().invoke(cli, ["--version"], catch_exceptions=False)
    assert result.exit_code == 0
    assert "polyrob v" in result.output


def test_help_is_grouped_with_start_here_first():
    out = _help_output()
    assert "Start here" in out
    assert out.index("Start here") < out.index("Surfaces")


def test_help_collapses_alias_rows():
    out = _help_output()
    # Aliases show on the canonical row, not as duplicate entries.
    assert "alias: sessions" in out
    assert "alias: models" in out
    lines = [ln.strip() for ln in out.splitlines()]
    assert not any(ln.startswith("sessions ") for ln in lines), (
        "alias rows must not render as separate commands"
    )


def test_module_docstring_not_stale():
    import cli.polyrob as mod

    doc = mod.__doc__ or ""
    assert "model set-default" not in doc, (
        "the module docstring listed 8 of 40+ commands — point at --help instead"
    )


def test_doctor_json():
    from cli.commands.doctor import doctor

    result = CliRunner().invoke(doctor, ["--json"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    # Click 8.2 interleaves stderr into `output`; on a box with a deployed
    # instance the admin-home NOTE (correctly on stderr) precedes the JSON.
    # `--json` contracts stdout only.
    payload = json.loads(getattr(result, "stdout", result.output))
    assert isinstance(payload.get("report"), list) and payload["report"]


def test_model_list_json():
    from cli.commands.model import model as model_group

    result = CliRunner().invoke(model_group, ["list", "--json"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "providers" in payload


def test_plain_doctor_surfaces_frozen_flag_disagreement():
    # A frozen policy flag whose env value disagrees was only visible in
    # `doctor --flags`; the plain report silently implied the env value works.
    from cli.commands.doctor import doctor_report

    env = dict(os.environ)
    env["AGENT_COMPUTE_POSTURE"] = "3"  # frozen at import as 0 in this process
    lines = doctor_report(env)
    joined = "\n".join(lines)
    assert "INERT" in joined and "AGENT_COMPUTE_POSTURE" in joined


def test_auth_status_json():
    from cli.commands.auth import auth as auth_group

    result = CliRunner().invoke(auth_group, ["status", "--json"], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "providers" in payload
