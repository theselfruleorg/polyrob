"""026 P1.3 — a file-set surface flag beats the runner's convenience default.

Failure mode D: `_surface_runner.py`/`gateway.py` seeded surface/bus flags into
the process env BEFORE the preflight's ``load_env``; local-mode layering is
``override=False``, so a `polyrob config set CORRESPONDENT_ACCESS_ENABLED
false`-style file value was silently ignored on those paths. The setdefault
now runs AFTER the preflight (which loads the env ladder), so the file value
is already in ``os.environ`` and setdefault never clobbers it.
"""
import asyncio

import pytest


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("SINGULAR_CHAT_ENABLED", "TELEGRAM_SURFACE_ENABLED",
                "CORRESPONDENT_ACCESS_ENABLED"):
        monkeypatch.delenv(var, raising=False)


def _fake_preflight_setting(monkeypatch, key: str, value: str):
    """Stand-in for preflight_or_onboard: 'loads' KEY=VALUE from the env files."""
    import os

    def fake(interactive=False):
        os.environ[key] = value  # what load_env would have layered in
        return True

    import cli.keys
    monkeypatch.setattr(cli.keys, "preflight_or_onboard", fake)


def _fail_container_build(monkeypatch):
    import core.bootstrap

    async def boom(*a, **k):
        raise RuntimeError("stop before heavy build")

    monkeypatch.setattr(core.bootstrap, "build_cli_container", boom)


def test_run_surface_file_value_beats_extra_env_default(monkeypatch):
    import os

    from cli.commands._surface_runner import run_surface

    _fake_preflight_setting(monkeypatch, "TELEGRAM_SURFACE_ENABLED", "false")
    _fail_container_build(monkeypatch)

    async def build_harness(ctx):  # never reached
        raise AssertionError("container build should have failed first")

    with pytest.raises(SystemExit):
        asyncio.run(run_surface(
            extra_env={"TELEGRAM_SURFACE_ENABLED": "true"},
            verbose=False,
            build_harness=build_harness,
            stopping_message="stopping",
        ))
    assert os.environ.get("TELEGRAM_SURFACE_ENABLED") == "false"


def test_gateway_file_value_beats_correspondent_default(monkeypatch):
    import os

    from cli.commands.gateway import _run_gateway

    _fake_preflight_setting(monkeypatch, "CORRESPONDENT_ACCESS_ENABLED", "false")
    _fail_container_build(monkeypatch)

    with pytest.raises(SystemExit):
        asyncio.run(_run_gateway(8080, None, False))
    assert os.environ.get("CORRESPONDENT_ACCESS_ENABLED") == "false"


def test_setdefault_order_is_pinned_in_source():
    """Structural pin: the preflight (env load) must precede the first
    os.environ.setdefault in both launchers."""
    from pathlib import Path
    for rel in ("cli/commands/_surface_runner.py", "cli/commands/gateway.py"):
        src = (Path(__file__).resolve().parents[3] / rel).read_text()
        preflight_at = src.index("preflight_or_onboard(interactive=False)")
        setdefault_at = src.index("os.environ.setdefault(")
        assert preflight_at < setdefault_at, (
            f"{rel}: os.environ.setdefault runs before the preflight's "
            "load_env — file values would be silently ignored (026 P1.3)")
