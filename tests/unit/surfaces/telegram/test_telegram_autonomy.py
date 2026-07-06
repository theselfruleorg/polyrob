"""polyrob telegram must start the autonomy runtime (goals/cron/curator) under local
mode and stop it on shutdown, using container.config.data_dir as the data home so the
goal dispatcher reads the same goals.db the GoalTool writes."""
import asyncio
import types
from unittest.mock import MagicMock, patch

import pytest


def _awaitable(value):
    async def _coro():
        return value
    return _coro()


def _make_container(data_dir=".rob"):
    container = MagicMock()
    container.config.data_dir = data_dir
    return container


@pytest.mark.asyncio
async def test_telegram_starts_and_stops_autonomy_under_local_mode():
    from cli.commands import telegram as tg

    container = _make_container(".rob")
    task_agent = MagicMock(name="task_agent")
    container.get_agent.return_value = task_agent

    harness = MagicMock(name="harness")
    harness.start = MagicMock(return_value=_awaitable(None))
    harness.stop = MagicMock(return_value=_awaitable(None))
    me = types.SimpleNamespace(username="testestovichbot")
    harness.bot.get_me = MagicMock(return_value=_awaitable(me))
    # run_polling completes immediately so the command returns
    harness.run_polling = MagicMock(return_value=_awaitable(None))

    handles = MagicMock(name="autonomy_handles")
    handles.stop = MagicMock(return_value=_awaitable(None))

    with patch("core.bootstrap.build_cli_container", return_value=container), \
         patch("core.bootstrap.setup_project_path"), \
         patch("core.bootstrap.setup_sqlite_compat"), \
         patch("cli.keys.preflight_or_onboard", return_value=True), \
         patch("core.surfaces.bootstrap.install_surface_bus"), \
         patch("surfaces.telegram.harness.build_telegram_harness", return_value=harness), \
         patch("agents.task.constants.local_mode_enabled", return_value=True), \
         patch("core.autonomy_runtime.start_autonomy", return_value=handles) as start_mock:
        await tg._run_telegram("dummy-token", verbose=True)

    # autonomy started with the container's data_dir, and stopped on shutdown
    start_mock.assert_called_once()
    _, kwargs = start_mock.call_args
    assert kwargs["task_agent"] is task_agent
    assert kwargs["data_dir"] == ".rob"
    handles.stop.assert_called_once()
