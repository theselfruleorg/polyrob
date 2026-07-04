import inspect
import core.bootstrap as bootstrap


def test_register_cli_tools_registers_cronjob_and_goal_when_enabled():
    src = inspect.getsource(bootstrap.register_cli_tools)
    assert "cron_enabled" in src
    assert "goals_enabled" in src
    assert "cronjob" in src
    assert "goal" in src
