"""Proposal 010 option A: `polyrob email` must NOT start the shared autonomy runtime
by default (EMAIL_AUTONOMY_RUNTIME unset/off), so the goal/cron dispatcher runs only
in the telegram process and a telegram-outbound goal can never be claimed by the
email-only process (whose MessageRouter has no telegram surface). Setting
EMAIL_AUTONOMY_RUNTIME=true restores the legacy dual-runtime behavior."""
import logging
import os
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _isolate_surface_env():
    """_run_email os.environ.setdefault()s surface flags (SINGULAR_CHAT /
    EMAIL_SURFACE / CORRESPONDENT_ACCESS) process-wide. Restore them after each
    test — a leaked CORRESPONDENT_ACCESS_ENABLED=true flips route_inbound to
    the fail-closed three-tier model and broke 6 unrelated telegram routing
    tests in the full-suite run (order-dependent pollution)."""
    keys = ("SINGULAR_CHAT_ENABLED", "EMAIL_SURFACE_ENABLED",
            "CORRESPONDENT_ACCESS_ENABLED")
    saved = {k: os.environ.get(k) for k in keys}
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _awaitable(value):
    async def _coro():
        return value
    return _coro()


def _make_container(data_dir=".rob"):
    container = MagicMock()
    container.config.data_dir = data_dir
    return container


def _make_harness():
    harness = MagicMock(name="harness")
    harness.start = MagicMock(return_value=_awaitable(None))
    harness.stop = MagicMock(return_value=_awaitable(None))
    # run_polling completes immediately so the command returns
    harness.run_polling = MagicMock(return_value=_awaitable(None))
    return harness


async def _run_email(container, harness, start_autonomy_mock):
    """Drive cli.commands.email._run_email with every seam mocked except the gate."""
    from cli.commands import email as em

    with ExitStack() as stack:
        stack.enter_context(patch("core.bootstrap.build_cli_container",
                                  return_value=container))
        stack.enter_context(patch("core.bootstrap.setup_project_path"))
        stack.enter_context(patch("core.bootstrap.setup_sqlite_compat"))
        stack.enter_context(patch("cli.keys.preflight_or_onboard", return_value=True))
        stack.enter_context(patch("core.surfaces.bootstrap.install_surface_bus"))
        stack.enter_context(patch("tools.email_tool.EmailTool",
                                  return_value=MagicMock(name="email_tool")))
        stack.enter_context(patch("surfaces.email.harness.build_email_harness",
                                  return_value=harness))
        stack.enter_context(patch("agents.task.constants.local_mode_enabled",
                                  return_value=True))
        stack.enter_context(patch("core.autonomy_runtime.start_autonomy",
                                  start_autonomy_mock))
        await em._run_email(poll_opt=7, verbose=True)


@pytest.mark.asyncio
async def test_email_does_not_start_autonomy_by_default(monkeypatch, caplog):
    monkeypatch.delenv("EMAIL_AUTONOMY_RUNTIME", raising=False)
    container = _make_container(".rob")
    container.get_agent.return_value = MagicMock(name="task_agent")
    start_mock = MagicMock(name="start_autonomy")

    with caplog.at_level(logging.INFO, logger="cli.commands.email"):
        await _run_email(container, _make_harness(), start_mock)

    start_mock.assert_not_called()
    assert any(
        "autonomy runtime disabled in email process (EMAIL_AUTONOMY_RUNTIME=off)"
        in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_email_does_not_start_autonomy_when_flag_explicitly_off(monkeypatch):
    monkeypatch.setenv("EMAIL_AUTONOMY_RUNTIME", "off")
    container = _make_container(".rob")
    container.get_agent.return_value = MagicMock(name="task_agent")
    start_mock = MagicMock(name="start_autonomy")

    await _run_email(container, _make_harness(), start_mock)

    start_mock.assert_not_called()


@pytest.mark.asyncio
async def test_email_starts_autonomy_when_flag_enabled(monkeypatch):
    monkeypatch.setenv("EMAIL_AUTONOMY_RUNTIME", "true")
    container = _make_container(".rob")
    task_agent = MagicMock(name="task_agent")
    container.get_agent.return_value = task_agent

    handles = MagicMock(name="autonomy_handles")
    handles.stop = MagicMock(return_value=_awaitable(None))
    start_mock = MagicMock(name="start_autonomy", return_value=handles)

    await _run_email(container, _make_harness(), start_mock)

    # legacy behavior restored: autonomy started with this container's data home
    # and stopped on shutdown
    start_mock.assert_called_once()
    _, kwargs = start_mock.call_args
    assert kwargs["task_agent"] is task_agent
    assert kwargs["data_dir"] == ".rob"
    handles.stop.assert_called_once()
