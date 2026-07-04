"""rob telegram — token resolution contract.

The command orchestrates a live polling loop (build container -> install bus ->
build harness -> poll), which is integration-verified. The unit-testable contract
is token resolution: --token wins, else TELEGRAM_BOT_TOKEN env, else a clear error.
"""
import pytest

from cli.commands.telegram import resolve_telegram_token, TelegramTokenError


def test_resolve_token_prefers_explicit_arg(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "from_env")
    assert resolve_telegram_token("from_arg") == "from_arg"


def test_resolve_token_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "from_env")
    assert resolve_telegram_token(None) == "from_env"


def test_resolve_token_missing_raises_clear_error(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(TelegramTokenError):
        resolve_telegram_token(None)


def test_resolve_token_blank_env_raises(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "   ")
    with pytest.raises(TelegramTokenError):
        resolve_telegram_token(None)


def test_telegram_builds_harness_with_container_data_dir(monkeypatch):
    """Isolation regression: the harness (dedup / user directory DBs) MUST be built with
    the container's data_dir, not the literal ./data default, so per-instance isolation
    holds under POLYROB_DATA_DIR."""
    import os
    from unittest.mock import MagicMock
    from click.testing import CliRunner
    from cli.commands import telegram as tg_mod

    # Isolate os.environ so setdefault(...SURFACE_ENABLED) doesn't leak into other tests.
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok123")

    fake_container = MagicMock()
    fake_container.config.data_dir = "/tmp/polyrob-instanceX"
    fake_container.get_agent.return_value = MagicMock()

    async def _fake_build(**kwargs):
        return fake_container

    monkeypatch.setattr("core.bootstrap.build_cli_container", _fake_build)
    monkeypatch.setattr("cli.keys.preflight_or_onboard", lambda **k: True)
    monkeypatch.setattr("core.surfaces.bootstrap.install_surface_bus", lambda c: None)

    captured = {}

    class _Stop(Exception):
        pass

    def _fake_harness(container, task_agent, **kwargs):
        captured.update(kwargs)
        raise _Stop()  # abort the (otherwise blocking) polling flow after capture

    monkeypatch.setattr("surfaces.telegram.harness.build_telegram_harness", _fake_harness)

    CliRunner().invoke(tg_mod.telegram, [])
    assert captured.get("data_dir") == "/tmp/polyrob-instanceX"
