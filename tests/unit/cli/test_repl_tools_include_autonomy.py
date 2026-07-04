import inspect
import cli.commands.chat as chat


def test_repl_appends_autonomy_tools_under_local_mode():
    src = inspect.getsource(chat._repl_main)
    # the REPL must conditionally add goal/cronjob to its tool list (not hardcode only filesystem/task)
    assert "repl_tools" in src
    assert 'container.has_service("goal")' in src
    assert 'container.has_service("cronjob")' in src
    assert "local_mode_enabled" in src
