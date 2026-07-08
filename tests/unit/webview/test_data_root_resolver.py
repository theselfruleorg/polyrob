"""RC-1 (2026-07-07): ONE session data root for every process.

The agent resolves its session tree via build_cli_container
(POLYROB_DATA_DIR → {data_home}/sessions) and installs it as the global pm();
the webview never ran that bootstrap, so its pm() fell back to env DATA_ROOT
(./data/task) — a stale, different tree on prod. These tests pin:

  1. core.runtime_paths.resolve_session_data_root — the shared resolver.
  2. Parity with the agent's own _resolve_cli_data_home for the deployed
     (POLYROB_DATA_DIR) case.
  3. The webview startup installs the resolved root as the process-global pm().
"""
import asyncio
import importlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.runtime_paths import resolve_session_data_root


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("DATA_ROOT", raising=False)
    monkeypatch.delenv("POLYROB_DATA_DIR", raising=False)
    monkeypatch.delenv("POLYROB_PROJECT_DIR", raising=False)
    yield


def test_explicit_data_root_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "explicit"))
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    assert resolve_session_data_root() == (tmp_path / "explicit").resolve()


def test_polyrob_data_dir_maps_to_sessions_subdir(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    assert resolve_session_data_root() == (tmp_path / "home").resolve() / "sessions"


def test_unset_falls_back_to_legacy_default():
    assert resolve_session_data_root() == Path("./data/task").resolve()


def test_blank_envs_treated_as_unset(monkeypatch):
    monkeypatch.setenv("DATA_ROOT", "  ")
    monkeypatch.setenv("POLYROB_DATA_DIR", "")
    assert resolve_session_data_root() == Path("./data/task").resolve()


def test_parity_with_agent_bootstrap_resolution(monkeypatch, tmp_path):
    """The agent's resolution is the REFERENCE: with POLYROB_DATA_DIR set,
    build_cli_container derives pm data_root = _resolve_cli_data_home()[0] /
    'sessions'. The shared resolver must produce the identical path."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "vlp"))
    from core.bootstrap import _resolve_cli_data_home

    data_home, _ws_is_root, _project = _resolve_cli_data_home()
    assert resolve_session_data_root() == data_home / "sessions"


def test_parity_holds_with_project_dir_set(monkeypatch, tmp_path):
    """POLYROB_PROJECT_DIR (shared-workspace mode) must not change where the
    session tree lives when POLYROB_DATA_DIR is set."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "vlp"))
    monkeypatch.setenv("POLYROB_PROJECT_DIR", str(tmp_path / "proj"))
    from core.bootstrap import _resolve_cli_data_home

    data_home, _ws_is_root, _project = _resolve_cli_data_home()
    assert resolve_session_data_root() == data_home / "sessions"


# ── webview startup installs the resolved root as the global pm() ────────────


@pytest.fixture
def _stub_heavy_startup(monkeypatch):
    """No-op the heavy startup collaborators (container/config/core init and
    the late auth/task half) so we test only the pm() install."""
    import core.container
    import core.config
    import core.initialization

    monkeypatch.setattr(core.container.DependencyContainer, "get_instance",
                        classmethod(lambda cls, *a, **k: MagicMock()), raising=False)
    monkeypatch.setattr(core.config, "BotConfig", lambda *a, **k: MagicMock())

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(core.initialization, "initialize_core", _noop)
    import webview.server as server
    monkeypatch.setattr(server, "_startup_late_services", _noop)
    return server


def test_webview_startup_installs_polyrob_data_dir_sessions(
        monkeypatch, tmp_path, _stub_heavy_startup):
    """With POLYROB_DATA_DIR set and DATA_ROOT unset, the webview's global
    pm() must point at {POLYROB_DATA_DIR}/sessions after startup — the same
    tree the agent process writes."""
    server = _stub_heavy_startup
    monkeypatch.delenv("WEBGATE_MULTITENANT", raising=False)
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "vlp"))

    from agents.task.path import pm, reset_path_manager
    reset_path_manager()
    try:
        asyncio.run(server.startup_event())
        assert pm().data_root == (tmp_path / "vlp").resolve() / "sessions"
    finally:
        reset_path_manager()


def test_webview_startup_explicit_data_root_still_wins(
        monkeypatch, tmp_path, _stub_heavy_startup):
    server = _stub_heavy_startup
    monkeypatch.delenv("WEBGATE_MULTITENANT", raising=False)
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "explicit"))
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "vlp"))

    from agents.task.path import pm, reset_path_manager
    reset_path_manager()
    try:
        asyncio.run(server.startup_event())
        assert pm().data_root == (tmp_path / "explicit").resolve()
    finally:
        reset_path_manager()


def test_webview_startup_unset_is_legacy_byte_identical(
        monkeypatch, _stub_heavy_startup):
    """Neither env set → pm() must resolve exactly what a bare PathManager()
    would (./data/task), so local-dev behavior does not move."""
    server = _stub_heavy_startup
    monkeypatch.delenv("WEBGATE_MULTITENANT", raising=False)

    from agents.task.path import pm, reset_path_manager
    reset_path_manager()
    try:
        asyncio.run(server.startup_event())
        assert pm().data_root == Path("./data/task").resolve()
    finally:
        reset_path_manager()
