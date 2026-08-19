"""026 P0.4 — feature-off warnings for verbs whose effect needs a gated loop."""
import click
import click.testing
import pytest

from cli._flag_warn import warn_if_flag_off


def _run(fn):
    """Capture the helper's stderr output through a tiny click command."""
    @click.command()
    def cmd():
        fn()
    runner = click.testing.CliRunner()
    return runner.invoke(cmd, [])


def test_warns_when_off():
    fired = {}

    def call():
        fired["v"] = warn_if_flag_off(
            "GOALS_ENABLED", "no dispatcher will pick goals up.",
            enabled_fn=lambda: False)

    result = _run(call)
    assert fired["v"] is True
    assert "GOALS_ENABLED is off" in result.output
    assert "polyrob config set GOALS_ENABLED true --global" in result.output
    assert "restart" in result.output


def test_silent_when_on():
    fired = {}

    def call():
        fired["v"] = warn_if_flag_off(
            "GOALS_ENABLED", "irrelevant.", enabled_fn=lambda: True)

    result = _run(call)
    assert fired["v"] is False
    assert "GOALS_ENABLED" not in result.output


def test_resolver_error_counts_as_off():
    def boom():
        raise RuntimeError("resolver broken")

    fired = {}

    def call():
        fired["v"] = warn_if_flag_off("CRON_ENABLED", "x.", enabled_fn=boom)

    result = _run(call)
    assert fired["v"] is True
    assert "CRON_ENABLED is off" in result.output


def test_custom_remedy_is_used():
    def call():
        warn_if_flag_off("CRON_ENABLED", "x.", enabled_fn=lambda: False,
                         remedy="polyrob config set CRON_ENABLED true --global "
                                "(or AUTONOMY_POSTURE=full)")

    result = _run(call)
    assert "AUTONOMY_POSTURE=full" in result.output


def test_goals_create_warns_when_goals_off(monkeypatch, tmp_path):
    """End-to-end: `polyrob goals create` must say the dispatcher is off."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    for var in ("GOALS_ENABLED", "AUTONOMY_ENABLED", "POLYROB_LOCAL",
                "AUTONOMY_MODE", "AUTONOMY_POSTURE"):
        monkeypatch.delenv(var, raising=False)
    from cli.commands.goals import goals
    runner = click.testing.CliRunner()
    result = runner.invoke(goals, ["create", "test goal title"])
    assert result.exit_code == 0, result.output
    assert "Created goal" in result.output
    assert "GOALS_ENABLED is off" in result.output


def test_goals_create_no_warning_when_goals_on(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GOALS_ENABLED", "true")
    from cli.commands.goals import goals
    runner = click.testing.CliRunner()
    result = runner.invoke(goals, ["create", "another goal title"])
    assert result.exit_code == 0, result.output
    assert "GOALS_ENABLED is off" not in result.output
