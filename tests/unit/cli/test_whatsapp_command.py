"""Task 4.6 — polyrob whatsapp CLI command.

The command is a webhook-server worker (no long-polling).  The unit-testable contract
is that the click command is importable, has the right name, and the surface-config
flag is readable without env.
"""


def test_whatsapp_command_is_registered():
    from cli.commands.whatsapp import whatsapp  # click command

    assert whatsapp.name == "whatsapp"


def test_whatsapp_exits_cleanly_when_no_creds(monkeypatch):
    """With no Meta WhatsApp creds the worker must print a clear message and exit 1,
    not report 'online' and then 404/401 only when Meta calls."""
    import os
    from unittest.mock import MagicMock
    from click.testing import CliRunner
    from cli.commands import whatsapp as wa_mod

    # Isolate os.environ so the command's os.environ.setdefault(WHATSAPP_SURFACE_ENABLED,
    # SINGULAR_CHAT_ENABLED) doesn't leak surface flags into later tests.
    monkeypatch.setattr(os, "environ", dict(os.environ))
    for v in ("WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_VERIFY_TOKEN"):
        monkeypatch.delenv(v, raising=False)

    fake_container = MagicMock()
    fake_container.config.data_dir = "/tmp/polyrob-test"
    fake_container.get_agent.return_value = MagicMock()

    async def _fake_build(**kwargs):
        return fake_container

    monkeypatch.setattr("core.bootstrap.build_cli_container", _fake_build)
    monkeypatch.setattr("cli.keys.preflight_or_onboard", lambda **k: True)

    res = CliRunner().invoke(wa_mod.whatsapp, [])
    assert res.exit_code == 1, res.output
    assert "not configured" in res.output.lower()


def test_whatsapp_command_has_port_option():
    from cli.commands.whatsapp import whatsapp

    opt_names = {p.name for p in whatsapp.params}
    assert "port" in opt_names
    assert "verbose" in opt_names


def test_whatsapp_surface_enabled_flag_default_off():
    """whatsapp_surface_enabled() must default to False (multi-tenant safe)."""
    import os

    from agents.task.surface_config import SurfaceConfig

    # Remove the env var if another session set it, verify we read False.
    original = os.environ.pop("WHATSAPP_SURFACE_ENABLED", None)
    try:
        assert SurfaceConfig.whatsapp_surface_enabled() is False
    finally:
        if original is not None:
            os.environ["WHATSAPP_SURFACE_ENABLED"] = original


def test_whatsapp_surface_enabled_reads_env(monkeypatch):
    monkeypatch.setenv("WHATSAPP_SURFACE_ENABLED", "true")
    from agents.task.surface_config import SurfaceConfig

    assert SurfaceConfig.whatsapp_surface_enabled() is True


def test_whatsapp_builds_harness_with_container_data_dir(monkeypatch):
    """Isolation regression: the harness (dedup / window DBs) MUST be built with the
    container's data_dir, not the literal ./data default, so per-instance isolation
    holds under POLYROB_DATA_DIR."""
    import os
    from unittest.mock import MagicMock
    from click.testing import CliRunner
    from cli.commands import whatsapp as wa_mod

    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "t")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "p")
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "v")

    fake_container = MagicMock()
    fake_container.config.data_dir = "/tmp/polyrob-instanceX"
    fake_container.get_agent.return_value = MagicMock()

    async def _fake_build(**kwargs):
        return fake_container

    monkeypatch.setattr("core.bootstrap.build_cli_container", _fake_build)
    monkeypatch.setattr("cli.keys.preflight_or_onboard", lambda **k: True)
    monkeypatch.setattr("core.surfaces.bootstrap.install_surface_bus", lambda c: None)
    monkeypatch.setattr("core.surfaces.transcription.log_transcription_readiness", lambda c: None)

    captured = {}

    class _Stop(Exception):
        pass

    def _fake_harness(container, task_agent, **kwargs):
        captured.update(kwargs)
        raise _Stop()  # abort before uvicorn.serve() (which would block) after capture

    monkeypatch.setattr("surfaces.whatsapp.harness.build_whatsapp_harness", _fake_harness)

    CliRunner().invoke(wa_mod.whatsapp, [])
    assert captured.get("data_dir") == "/tmp/polyrob-instanceX"
