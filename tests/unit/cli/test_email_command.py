"""Tests for `polyrob email` (IMAP/SMTP correspondent surface)."""
from unittest.mock import AsyncMock, MagicMock

from click.testing import CliRunner


def test_email_command_registered():
    from cli.polyrob import cli
    assert "email" in cli.commands


def test_email_exits_cleanly_when_no_creds(monkeypatch):
    """With no gmail_email/gmail_app_password the surface must print a clear message
    and exit 1 — NOT print 'online (unconfigured)' and silently poll forever."""
    import os
    # Isolate os.environ so the command's os.environ.setdefault(SINGULAR_CHAT_ENABLED,
    # EMAIL_SURFACE_ENABLED, ...) doesn't leak surface flags into later tests.
    monkeypatch.setattr(os, "environ", dict(os.environ))
    from cli.commands import email as email_mod

    fake_container = MagicMock()
    fake_container.config.gmail_email = None
    fake_container.config.gmail_app_password = None
    fake_container.config.data_dir = "/tmp/polyrob-test"
    fake_container.get_agent.return_value = MagicMock()  # task_agent present

    async def _fake_build(**kwargs):
        return fake_container

    monkeypatch.setattr("core.bootstrap.build_cli_container", _fake_build)
    monkeypatch.setattr("cli.keys.preflight_or_onboard", lambda **k: True)

    res = CliRunner().invoke(email_mod.email, [])
    assert res.exit_code == 1, res.output
    assert "not configured" in res.output.lower()


def test_email_starts_and_stops_outbound_dispatcher(monkeypatch):
    """The email surface MUST start the outbound dispatcher before polling and stop it
    in the teardown finally — otherwise (durable outbound on) correspondent replies
    enqueue and NEVER send. Mirrors telegram.py's dispatcher lifecycle."""
    import os
    monkeypatch.setattr(os, "environ", dict(os.environ))
    from cli.commands import email as email_mod

    # sync start() + async stop(), matching core.surfaces.outbound_dispatcher.
    dispatcher = MagicMock()
    dispatcher.start = MagicMock()
    dispatcher.stop = AsyncMock()

    def _get_service(name):
        if name == "outbound_dispatcher":
            return dispatcher
        # non-None correspondent_registry → skip the registration branch.
        return MagicMock()

    fake_container = MagicMock()
    fake_container.config.gmail_email = "bot@example.com"
    fake_container.config.gmail_app_password = "app-pw"
    fake_container.config.data_dir = "/tmp/polyrob-test"
    fake_container.get_agent.return_value = MagicMock()  # task_agent present
    fake_container.get_service.side_effect = _get_service

    async def _fake_build(**kwargs):
        return fake_container

    class _FakeHarness:
        async def start(self):
            return None

        async def run_polling(self):
            return None  # return immediately → clean teardown, no real IMAP

        async def stop(self):
            return None

    monkeypatch.setattr("core.bootstrap.build_cli_container", _fake_build)
    monkeypatch.setattr("cli.keys.preflight_or_onboard", lambda **k: True)
    monkeypatch.setattr("core.surfaces.bootstrap.install_surface_bus", lambda *a, **k: None)
    monkeypatch.setattr("tools.email_tool.EmailTool", lambda *a, **k: MagicMock())
    monkeypatch.setattr("surfaces.email.harness.build_email_harness",
                        lambda *a, **k: _FakeHarness())
    # Skip the autonomy runtime — not under test here.
    monkeypatch.setattr("agents.task.constants.local_mode_enabled", lambda: False)

    res = CliRunner().invoke(email_mod.email, ["--poll", "1"])

    assert res.exit_code == 0, res.output
    dispatcher.start.assert_called_once_with()
    dispatcher.stop.assert_awaited_once_with()
