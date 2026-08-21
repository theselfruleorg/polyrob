"""`polyrob --version` / `-V` should print the version and exit 0.

Fresh-install finding (2026-07-19): `polyrob --version` errored
with "No such option" — only `polyrob version` (a subcommand) or `pip show polyrob`
revealed it. Click's standard `--version` convention was simply never wired onto
the top-level group. Trivial fix: `@click.version_option` on `cli`.
"""
from click.testing import CliRunner


def test_version_flag_prints_version_and_exits_zero():
    from cli.polyrob import cli, VERSION
    runner = CliRunner()
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert VERSION in result.output
    assert "polyrob" in result.output.lower()


def test_short_version_flag_also_works():
    from cli.polyrob import cli, VERSION
    runner = CliRunner()
    result = runner.invoke(cli, ["-V"])
    assert result.exit_code == 0
    assert VERSION in result.output


def test_version_subcommand_still_works_unchanged():
    """The pre-existing `polyrob version` subcommand is a separate, richer
    (env-info) command — --version must not shadow or break it."""
    from cli.polyrob import cli, VERSION
    runner = CliRunner()
    result = runner.invoke(cli, ["version"])
    assert result.exit_code == 0
    assert VERSION in result.output
    assert "python" in result.output.lower()
