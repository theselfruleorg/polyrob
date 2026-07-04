"""Tests for gateway CLI command."""
import os

import pytest
from unittest.mock import MagicMock, patch


def _patch_gateway_bootstrap(monkeypatch, data_dir="/tmp/polyrob-instanceX"):
    """Isolate os.environ + stub the heavy bootstrap so _run_gateway reaches the surface
    wiring hermetically. Returns the fake container. Callers set the surface flags they
    want AFTER calling this (env is already copied)."""
    monkeypatch.setattr(os, "environ", dict(os.environ))
    for v in ("TELEGRAM_SURFACE_ENABLED", "WHATSAPP_SURFACE_ENABLED", "EMAIL_SURFACE_ENABLED"):
        monkeypatch.delenv(v, raising=False)

    fake_container = MagicMock()
    fake_container.config.data_dir = data_dir
    fake_container.get_agent.return_value = MagicMock()

    async def _fake_build(**kwargs):
        return fake_container

    monkeypatch.setattr("core.bootstrap.build_cli_container", _fake_build)
    monkeypatch.setattr("cli.keys.preflight_or_onboard", lambda **k: True)
    monkeypatch.setattr("core.surfaces.bootstrap.install_surface_bus", lambda c: None)
    monkeypatch.setattr("core.surfaces.transcription.log_transcription_readiness", lambda c: None)
    return fake_container


def test_gateway_command_registered():
    """Gateway command exists and is registered in polyrob CLI."""
    from cli.polyrob import cli
    command_names = [cmd.name for cmd in cli.commands.values()]
    assert "gateway" in command_names


def test_gateway_command_name():
    """Gateway command name is 'gateway'."""
    from cli.commands.gateway import gateway
    assert gateway.name == "gateway"


def test_gateway_imports_cleanly():
    """cli.commands.gateway imports without any surface env set."""
    import importlib
    mod = importlib.import_module("cli.commands.gateway")
    assert hasattr(mod, "gateway")


def test_gateway_wires_whatsapp_harness():
    """Regression: the gateway WhatsApp path MUST build the harness — without it,
    webhook_surfaces['whatsapp'] is never registered and every Meta verify/inbound
    POST 404s while the CLI claims the surface is online."""
    import inspect
    import cli.commands.gateway as g
    src = inspect.getsource(g._run_gateway)
    assert "build_whatsapp_harness" in src


def test_gateway_guards_each_surface_setup():
    """A single surface failing to start must be skipped, not crash the whole gateway
    (leaking the already-started dispatcher/autonomy). Each surface block is guarded."""
    import inspect
    import cli.commands.gateway as g
    src = inspect.getsource(g._run_gateway)
    # crude but effective: the tg/wa/em setup blocks each sit inside a try/except.
    assert src.count("failed to start, skipping") >= 2


def test_gateway_warns_on_empty_whatsapp_creds(monkeypatch):
    """BUG 2 regression: WHATSAPP_SURFACE_ENABLED=true with empty Meta creds must emit a
    local WARN (and skip WhatsApp) rather than serving a webhook that silently 401/404s."""
    from click.testing import CliRunner
    from cli.commands import gateway as gw_mod

    _patch_gateway_bootstrap(monkeypatch)
    monkeypatch.setenv("WHATSAPP_SURFACE_ENABLED", "true")
    for v in ("WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_VERIFY_TOKEN"):
        monkeypatch.delenv(v, raising=False)

    # Harden: if the preflight were missing, this would blow up loudly instead of building.
    def _fail(*a, **k):
        raise AssertionError("build_whatsapp_harness must NOT be called with empty creds")

    monkeypatch.setattr("surfaces.whatsapp.harness.build_whatsapp_harness", _fail)

    res = CliRunner().invoke(gw_mod.gateway, [])
    out = res.output.lower()
    assert "skipping whatsapp" in out
    assert "whatsapp" in out


def test_gateway_builds_whatsapp_harness_with_container_data_dir(monkeypatch):
    """BUG 1 regression: the WhatsApp harness MUST be built with the container's data_dir."""
    from click.testing import CliRunner
    from cli.commands import gateway as gw_mod

    _patch_gateway_bootstrap(monkeypatch)
    monkeypatch.setenv("WHATSAPP_SURFACE_ENABLED", "true")
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "t")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "p")
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "v")

    captured = {}

    def _fake_harness(container, task_agent, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop after capture")  # gateway try/except swallows -> skips

    monkeypatch.setattr("surfaces.whatsapp.harness.build_whatsapp_harness", _fake_harness)

    CliRunner().invoke(gw_mod.gateway, [])
    assert captured.get("data_dir") == "/tmp/polyrob-instanceX"


def test_gateway_builds_telegram_harness_with_container_data_dir(monkeypatch):
    """BUG 1 regression: the Telegram harness MUST be built with the container's data_dir."""
    from click.testing import CliRunner
    from cli.commands import gateway as gw_mod

    _patch_gateway_bootstrap(monkeypatch)
    monkeypatch.setenv("TELEGRAM_SURFACE_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok123")

    captured = {}

    def _fake_harness(container, task_agent, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop after capture")  # gateway try/except swallows -> skips

    monkeypatch.setattr("surfaces.telegram.harness.build_telegram_harness", _fake_harness)

    CliRunner().invoke(gw_mod.gateway, [])
    assert captured.get("data_dir") == "/tmp/polyrob-instanceX"
