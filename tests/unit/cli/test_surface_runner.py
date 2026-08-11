"""Contract tests for the shared single-surface runner (cli/commands/_surface_runner.py).

The seven network-surface commands (slack/discord/signal/x/whatsapp/telegram/email)
delegate their common envelope to ``run_surface``. These tests pin the envelope's
load-bearing behavior: the hook order (preflight before the bus install), the ONE
data_dir computation handed to the harness AND autonomy, the autonomy precheck gate,
the teardown order (autonomy -> dispatcher -> harness), and that a ``build_harness``
failure propagates WITHOUT running teardown (the harness-capture tests for
telegram/whatsapp rely on exactly that).
"""
import os
from unittest.mock import AsyncMock, MagicMock

import pytest


def _patch_bootstrap(monkeypatch, data_dir="/tmp/polyrob-instanceX"):
    """Isolate os.environ + stub the heavy bootstrap seams (same style as the
    telegram/whatsapp/email command tests). Returns the fake container."""
    monkeypatch.setattr(os, "environ", dict(os.environ))

    fake_container = MagicMock()
    fake_container.config.data_dir = data_dir
    fake_container.get_agent.return_value = MagicMock(name="task_agent")
    fake_container.get_service.return_value = None  # no dispatcher unless a test wires one

    async def _fake_build(**kwargs):
        return fake_container

    monkeypatch.setattr("core.bootstrap.build_cli_container", _fake_build)
    monkeypatch.setattr("core.bootstrap.setup_project_path", lambda: None)
    monkeypatch.setattr("core.bootstrap.setup_sqlite_compat", lambda: None)
    monkeypatch.setattr("cli.keys.preflight_or_onboard", lambda **k: True)
    monkeypatch.setattr("core.surfaces.bootstrap.install_surface_bus", lambda c: None)
    return fake_container


class _Harness:
    def __init__(self, calls):
        self._calls = calls

    async def run(self):
        self._calls.append("run")

    async def stop(self):
        self._calls.append("harness.stop")


@pytest.mark.asyncio
async def test_run_surface_data_dir_env_defaults_and_teardown_order(monkeypatch):
    """data_dir is computed once from container.config.data_dir and handed to BOTH
    the harness ctx and start_autonomy; env defaults setdefault'd; teardown runs
    autonomy.stop -> dispatcher.stop -> harness.stop after the main loop returns."""
    from cli.commands._surface_runner import SurfaceJob, run_surface

    container = _patch_bootstrap(monkeypatch)
    calls = []

    dispatcher = MagicMock()
    dispatcher.start = MagicMock(side_effect=lambda: calls.append("dispatcher.start"))
    dispatcher.stop = AsyncMock(side_effect=lambda: calls.append("dispatcher.stop"))
    container.get_service.side_effect = (
        lambda name: dispatcher if name == "outbound_dispatcher" else None)
    container.get_service.return_value = None

    handles = MagicMock()
    handles.stop = AsyncMock(side_effect=lambda: calls.append("autonomy.stop"))
    started = {}

    def _start_autonomy(**kwargs):
        calls.append("autonomy.start")
        started.update(kwargs)
        return handles

    monkeypatch.setattr("agents.task.constants.local_mode_enabled", lambda: True)
    monkeypatch.setattr("core.autonomy_runtime.start_autonomy", _start_autonomy)

    seen = {}
    harness = _Harness(calls)

    async def _build(ctx):
        seen["data_dir"] = ctx.data_dir
        seen["creds"] = ctx.creds
        seen["task_agent"] = ctx.task_agent
        return SurfaceJob(harness=harness, run=harness.run)

    await run_surface(
        extra_env={"FAKE_SURFACE_ENABLED": "true"},
        verbose=True,
        resolve_credentials=lambda: "tok",
        build_harness=_build,
        stopping_message="stopping fake surface…",
    )

    assert seen["data_dir"] == "/tmp/polyrob-instanceX"
    assert seen["creds"] == "tok"
    assert started["data_dir"] == "/tmp/polyrob-instanceX"  # same single computation
    assert started["task_agent"] is seen["task_agent"]
    assert os.environ.get("SINGULAR_CHAT_ENABLED") == "true"
    assert os.environ.get("FAKE_SURFACE_ENABLED") == "true"
    assert calls == ["dispatcher.start", "autonomy.start", "run",
                     "autonomy.stop", "dispatcher.stop", "harness.stop"]


@pytest.mark.asyncio
async def test_run_surface_preflight_runs_before_bus_install(monkeypatch):
    """The surface preflight hook (whatsapp/email cred checks) must run BEFORE
    install_surface_bus so a misconfigured surface exits without side effects."""
    from cli.commands._surface_runner import SurfaceJob, run_surface

    _patch_bootstrap(monkeypatch)
    order = []
    monkeypatch.setattr("core.surfaces.bootstrap.install_surface_bus",
                        lambda c: order.append("bus"))
    monkeypatch.setattr("agents.task.constants.local_mode_enabled", lambda: False)

    harness = _Harness(order)

    async def _build(ctx):
        return SurfaceJob(harness=harness, run=harness.run)

    await run_surface(
        extra_env={},
        verbose=True,
        preflight=lambda container, task_agent: order.append("preflight"),
        build_harness=_build,
        stopping_message="stopping fake surface…",
    )

    assert order[:2] == ["preflight", "bus"]


@pytest.mark.asyncio
async def test_run_surface_autonomy_precheck_false_skips_start(monkeypatch):
    """autonomy_precheck returning False (email's EMAIL_AUTONOMY_RUNTIME gate) must
    skip start_autonomy entirely, even under local mode."""
    from cli.commands._surface_runner import SurfaceJob, run_surface

    _patch_bootstrap(monkeypatch)
    started = []
    monkeypatch.setattr("agents.task.constants.local_mode_enabled", lambda: True)
    monkeypatch.setattr("core.autonomy_runtime.start_autonomy",
                        lambda **k: started.append(k) or MagicMock())

    harness = _Harness([])

    async def _build(ctx):
        return SurfaceJob(harness=harness, run=harness.run)

    await run_surface(
        extra_env={},
        verbose=True,
        autonomy_precheck=lambda: False,
        build_harness=_build,
        stopping_message="stopping fake surface…",
    )

    assert not started


@pytest.mark.asyncio
async def test_run_surface_build_harness_failure_propagates_without_teardown(monkeypatch):
    """A build_harness exception must propagate BEFORE the run/teardown phase — the
    telegram/whatsapp harness-capture tests raise from the harness builder and rely
    on dispatcher.stop never being awaited (matching the pre-refactor flow)."""
    from cli.commands._surface_runner import run_surface

    container = _patch_bootstrap(monkeypatch)
    dispatcher = MagicMock()
    dispatcher.start = MagicMock()
    dispatcher.stop = AsyncMock()
    container.get_service.side_effect = (
        lambda name: dispatcher if name == "outbound_dispatcher" else None)
    container.get_service.return_value = None
    monkeypatch.setattr("agents.task.constants.local_mode_enabled", lambda: False)

    class _Stop(Exception):
        pass

    async def _build(ctx):
        raise _Stop()

    with pytest.raises(_Stop):
        await run_surface(
            extra_env={},
            verbose=True,
            build_harness=_build,
            stopping_message="stopping fake surface…",
        )

    dispatcher.start.assert_called_once_with()
    dispatcher.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_surface_guarded_harness_stop_swallows_errors(monkeypatch):
    """guard_harness_stop=True (whatsapp) wraps the final harness.stop() in
    try/except so a failing stop cannot mask a clean shutdown."""
    from cli.commands._surface_runner import SurfaceJob, run_surface

    _patch_bootstrap(monkeypatch)
    monkeypatch.setattr("agents.task.constants.local_mode_enabled", lambda: False)

    class _ExplodingHarness:
        async def run(self):
            return None

        async def stop(self):
            raise RuntimeError("boom")

    harness = _ExplodingHarness()

    async def _build(ctx):
        return SurfaceJob(harness=harness, run=harness.run, guard_harness_stop=True)

    # Must NOT raise despite harness.stop() exploding.
    await run_surface(
        extra_env={},
        verbose=True,
        build_harness=_build,
        stopping_message="stopping fake surface…",
    )
