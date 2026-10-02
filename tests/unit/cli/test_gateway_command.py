"""Tests for gateway CLI command."""
import os

import pytest
from unittest.mock import MagicMock, patch


def _patch_gateway_bootstrap(monkeypatch, data_dir="/tmp/polyrob-instanceX"):
    """Isolate os.environ + stub the heavy bootstrap so _run_gateway reaches the surface
    wiring hermetically. Returns the fake container. Callers set the surface flags they
    want AFTER calling this (env is already copied)."""
    monkeypatch.setattr(os, "environ", dict(os.environ))
    for v in ("TELEGRAM_SURFACE_ENABLED", "WHATSAPP_SURFACE_ENABLED", "EMAIL_SURFACE_ENABLED",
              "DISCORD_SURFACE_ENABLED", "SLACK_SURFACE_ENABLED", "SIGNAL_SURFACE_ENABLED",
              "X_SURFACE_ENABLED",
              "DISCORD_BOT_TOKEN", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SIGNAL_ACCOUNT",
              "TWITTER_API_KEY", "TWITTER_API_SECRET_KEY",
              "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_TOKEN_SECRET",
              "TWITTER_OAUTH2_ACCESS_TOKEN"):
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
    assert "gateway" in cli.list_commands(None)


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
    # 064 F1: the gateway launches each surface through its package's launch.py.
    import inspect
    import surfaces.whatsapp.launch as wa_launch
    assert "build_whatsapp_harness" in inspect.getsource(wa_launch.launch)


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


# ---------------------------------------------------------------------------
# 3.1 / H2 (2026-07-14 review): the gateway launches Discord/Slack/Signal/X
# when their flags are on — previously it silently ignored them, so an
# enabled-but-unlaunched surface gave the operator NO signal at all.
# ---------------------------------------------------------------------------

# (surface name, flag, creds to set, harness patch target)
_CONNECTORS = [
    ("discord", "DISCORD_SURFACE_ENABLED",
     {"DISCORD_BOT_TOKEN": "d-tok"},
     "surfaces.discord.harness.build_discord_harness"),
    ("slack", "SLACK_SURFACE_ENABLED",
     {"SLACK_BOT_TOKEN": "xoxb-1", "SLACK_APP_TOKEN": "xapp-1"},
     "surfaces.slack.harness.build_slack_harness"),
    ("signal", "SIGNAL_SURFACE_ENABLED",
     {"SIGNAL_ACCOUNT": "+15550001111"},
     "surfaces.signal.harness.build_signal_harness"),
    ("x", "X_SURFACE_ENABLED",
     {"TWITTER_API_KEY": "k", "TWITTER_API_SECRET_KEY": "s",
      "TWITTER_ACCESS_TOKEN": "t", "TWITTER_ACCESS_TOKEN_SECRET": "ts"},
     "polyrob_x.surface.harness.build_x_harness"),
]


@pytest.mark.parametrize("name,flag,creds,target", _CONNECTORS)
def test_gateway_builds_connector_harness_when_enabled(monkeypatch, name, flag, creds, target):
    """Flag on + creds present → the gateway builds the harness with the container data_dir."""
    from click.testing import CliRunner
    from cli.commands import gateway as gw_mod

    _patch_gateway_bootstrap(monkeypatch)
    monkeypatch.setenv(flag, "true")
    for k, v in creds.items():
        monkeypatch.setenv(k, v)

    captured = {}

    def _fake_harness(container, task_agent, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop after capture")  # gateway try/except swallows -> skips

    monkeypatch.setattr(target, _fake_harness)

    CliRunner().invoke(gw_mod.gateway, [])
    assert captured.get("data_dir") == "/tmp/polyrob-instanceX", (
        f"{name}: harness not built (or built without the container data_dir)")


@pytest.mark.parametrize("name,flag,creds,target", _CONNECTORS)
def test_gateway_warns_and_skips_connector_without_creds(monkeypatch, name, flag, creds, target):
    """Flag on but creds missing → loud WARN + skip; the harness must NOT be built."""
    from click.testing import CliRunner
    from cli.commands import gateway as gw_mod

    _patch_gateway_bootstrap(monkeypatch)
    monkeypatch.setenv(flag, "true")
    # creds deliberately absent (cleared by _patch_gateway_bootstrap)

    def _fail(*a, **k):
        raise AssertionError(f"build_{name}_harness must NOT be called without creds")

    monkeypatch.setattr(target, _fail)

    res = CliRunner().invoke(gw_mod.gateway, [])
    assert f"skipping {name}" in res.output.lower()


def test_gateway_accepts_x_oauth2_user_token(monkeypatch):
    from click.testing import CliRunner
    from cli.commands import gateway as gw_mod

    _patch_gateway_bootstrap(monkeypatch)
    monkeypatch.setenv("X_SURFACE_ENABLED", "true")
    monkeypatch.setenv("TWITTER_OAUTH2_ACCESS_TOKEN", "oauth2-user-token")
    captured = {}

    def _fake_harness(container, task_agent, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop after capture")

    monkeypatch.setattr(
        "polyrob_x.surface.harness.build_x_harness", _fake_harness)
    CliRunner().invoke(gw_mod.gateway, [])
    assert captured.get("data_dir") == "/tmp/polyrob-instanceX"


def test_gateway_no_surfaces_message_lists_all_flags(monkeypatch):
    """The 'no surfaces enabled' guidance names every launchable surface flag."""
    from click.testing import CliRunner
    from cli.commands import gateway as gw_mod

    _patch_gateway_bootstrap(monkeypatch)
    res = CliRunner().invoke(gw_mod.gateway, [])
    for flag in ("TELEGRAM_SURFACE_ENABLED", "WHATSAPP_SURFACE_ENABLED",
                 "EMAIL_SURFACE_ENABLED", "DISCORD_SURFACE_ENABLED",
                 "SLACK_SURFACE_ENABLED", "SIGNAL_SURFACE_ENABLED",
                 "X_SURFACE_ENABLED"):
        assert flag in res.output


def test_gateway_skips_email_without_credentials(monkeypatch):
    """064 revalidation: the email launch ran with no credentials and reported the
    surface online; it now WARNs and skips like every other surface."""
    from click.testing import CliRunner
    from cli.commands import gateway as gw_mod

    fake = _patch_gateway_bootstrap(monkeypatch)
    fake.config.gmail_email = None
    fake.config.gmail_app_password = None
    monkeypatch.setenv("EMAIL_SURFACE_ENABLED", "true")
    monkeypatch.delenv("AGENTMAIL_API_KEY", raising=False)
    monkeypatch.setenv("EMAIL_PROVIDER", "smtp")

    def _fail(*a, **k):
        raise AssertionError("build_email_harness must NOT be called without creds")

    monkeypatch.setattr("surfaces.email.harness.build_email_harness", _fail)
    res = CliRunner().invoke(gw_mod.gateway, [])
    assert "skipping email" in res.output.lower()


@pytest.mark.asyncio
async def test_webhook_replay_helper_runs_only_journaling_surfaces():
    from surfaces._launch import recover_webhook_surfaces

    class _WS:
        def __init__(self, journaling, n):
            self.ack_before_turn, self.n, self.called = journaling, n, False

        async def recover(self, container, task_agent):
            self.called = True
            return self.n

    a, b = _WS(True, 2), _WS(False, 5)

    class _C:
        def get_service(self, name):
            return {"a": a, "b": b} if name == "webhook_surfaces" else None

    notes = []
    assert await recover_webhook_surfaces(_C(), None, note=notes.append, warn=notes.append) == 2
    assert a.called and not b.called and "replayed 2" in notes[0]


def test_every_webhook_entry_point_replays_before_serving():
    """The gateway and `polyrob whatsapp` both call the ONE replay helper."""
    import inspect
    import cli.commands.gateway as gw
    import cli.commands.whatsapp as wa
    for src in (inspect.getsource(gw._run_gateway), inspect.getsource(wa)):
        assert "recover_webhook_surfaces" in src
        assert src.index("recover_webhook_surfaces(") < src.rindex("serve")


# --- configured = on; the prod gateway unit's options (064 factory set-up) ----

def test_a_configured_surface_starts_with_no_enable_flag(monkeypatch):
    from click.testing import CliRunner
    from cli.commands import gateway as gw_mod

    _patch_gateway_bootstrap(monkeypatch)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "d-tok")          # no DISCORD_SURFACE_ENABLED
    captured = {}

    def _fake(container, task_agent, **kw):
        captured.update(kw)
        raise RuntimeError("stop after capture")

    monkeypatch.setattr("surfaces.discord.harness.build_discord_harness", _fake)
    CliRunner().invoke(gw_mod.gateway, [])
    assert captured.get("token") == "d-tok"


def test_an_explicit_false_keeps_a_configured_surface_off(monkeypatch):
    from click.testing import CliRunner
    from cli.commands import gateway as gw_mod

    _patch_gateway_bootstrap(monkeypatch)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "d-tok")
    monkeypatch.setenv("DISCORD_SURFACE_ENABLED", "false")

    def _fail(*a, **k):
        raise AssertionError("an explicit false must keep the surface off")

    monkeypatch.setattr("surfaces.discord.harness.build_discord_harness", _fail)
    res = CliRunner().invoke(gw_mod.gateway, [])
    assert "No surfaces enabled" in res.output


def test_shared_credential_surfaces_never_auto_start(monkeypatch):
    """X shares the posting tool's keys, email the email tool's, telegram runs as
    its own primary process: configured is NOT enough for these."""
    from core.surfaces.config import SurfaceConfig
    monkeypatch.setattr(os, "environ", dict(os.environ))
    for k in ("X_SURFACE_ENABLED", "TELEGRAM_SURFACE_ENABLED", "EMAIL_SURFACE_ENABLED",
              "AUTONOMY_MODE"):
        monkeypatch.delenv(k, raising=False)
    for k in ("TWITTER_API_KEY", "TWITTER_API_SECRET_KEY", "TWITTER_ACCESS_TOKEN",
              "TWITTER_ACCESS_TOKEN_SECRET", "TELEGRAM_BOT_TOKEN"):
        monkeypatch.setenv(k, "v")
    assert not SurfaceConfig.surface_enabled("x")
    assert not SurfaceConfig.surface_enabled("telegram")


def test_skip_leaves_a_surface_to_its_own_process(monkeypatch):
    from click.testing import CliRunner
    from cli.commands import gateway as gw_mod

    _patch_gateway_bootstrap(monkeypatch)
    monkeypatch.setenv("TELEGRAM_SURFACE_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")

    def _fail(*a, **k):
        raise AssertionError("--skip telegram must not build the telegram harness")

    monkeypatch.setattr("surfaces.telegram.harness.build_telegram_harness", _fail)
    res = CliRunner().invoke(gw_mod.gateway, ["--skip", "telegram,email"])
    assert "No surfaces enabled" in res.output


def test_idle_when_empty_waits_before_building_the_container(monkeypatch):
    from click.testing import CliRunner
    from cli.commands import gateway as gw_mod

    _patch_gateway_bootstrap(monkeypatch)
    built = []

    async def _build(**k):
        built.append(1)

    monkeypatch.setattr("core.bootstrap.build_cli_container", _build)
    waited = []

    async def _wait(self):
        waited.append(1)

    monkeypatch.setattr("asyncio.Event.wait", _wait)
    res = CliRunner().invoke(gw_mod.gateway, ["--idle-when-empty", "--skip", "telegram,email"])
    assert waited == [1] and built == []
    assert "idling" in res.output


def test_no_autonomy_never_starts_the_loops(monkeypatch):
    from click.testing import CliRunner
    from cli.commands import gateway as gw_mod

    _patch_gateway_bootstrap(monkeypatch)
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "d-tok")
    started = []
    monkeypatch.setattr("core.autonomy_runtime.start_autonomy",
                        lambda **k: started.append(1))
    monkeypatch.setattr("surfaces.discord.harness.build_discord_harness",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop")))
    CliRunner().invoke(gw_mod.gateway, ["--no-autonomy"])
    assert started == []
