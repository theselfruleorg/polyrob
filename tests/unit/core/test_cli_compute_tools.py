"""Compute-posture tools (shell/process/self_env) must be CLI-container-registered.

Root cause (live prod 2026-07-07): WS-2/WS-5 registered the tool DESCRIPTORS/classes
but `register_cli_tools` (the headless/POLYROB_LOCAL container builder) never
instantiated them as container SERVICES — so a goal's controller
`load_tools_from_container(['shell', ...])` logged "✗ Tool 'shell' not found in
container" and ran without them. This mirrors the SB-02 git/knowledge fix. The server
API path (initialize_tools via get_tool_init_order) already picks them up; this closes
the CLI/bootstrap path.
"""
import pytest

import agents.task.constants as c


def _temp_config(monkeypatch, tmp_path):
    for k in ("DATA_DIR", "DATA_ROOT", "CHARACTERS_DIR", "KNOWLEDGE_DIR",
              "CACHE_DIR", "DB_PATH", "TELEMETRY_DATA_DIR"):
        monkeypatch.setenv(k, str(tmp_path / k.lower()))
    from core.config import BotConfig
    return BotConfig()


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("AGENT_COMPUTE_POSTURE", "SHELL_TOOLS_ENABLED", "SELF_ENV_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    c._refreeze_compute_posture_for_tests()
    yield
    c._refreeze_compute_posture_for_tests()


def test_shell_and_process_in_cli_registerable_set(monkeypatch):
    import core.bootstrap as bootstrap
    assert "shell" in bootstrap._CLI_REGISTERABLE_TOOLS
    assert "process" in bootstrap._CLI_REGISTERABLE_TOOLS
    assert "self_env" in bootstrap._CLI_REGISTERABLE_TOOLS


@pytest.mark.asyncio
async def test_register_cli_tools_registers_shell_process_at_posture_1(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "1")
    c._refreeze_compute_posture_for_tests()
    config = _temp_config(monkeypatch, tmp_path)
    from core.container import DependencyContainer
    from core.bootstrap import register_cli_tools

    DependencyContainer._instance = None
    container = DependencyContainer.get_instance(config)
    await register_cli_tools(container)

    assert container.has_service("shell"), "shell must be a CLI container service at posture 1"
    assert container.has_service("process"), "process must be a CLI container service at posture 1"
    # self_env needs posture 2 — not at posture 1
    assert not container.has_service("self_env")


@pytest.mark.asyncio
async def test_register_cli_tools_omits_compute_tools_at_posture_0(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_COMPUTE_POSTURE", raising=False)
    c._refreeze_compute_posture_for_tests()
    config = _temp_config(monkeypatch, tmp_path)
    from core.container import DependencyContainer
    from core.bootstrap import register_cli_tools

    DependencyContainer._instance = None
    container = DependencyContainer.get_instance(config)
    await register_cli_tools(container)

    assert not container.has_service("shell")
    assert not container.has_service("process")
    assert not container.has_service("self_env")


@pytest.mark.asyncio
async def test_register_cli_tools_registers_self_env_at_posture_2(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "2")
    c._refreeze_compute_posture_for_tests()
    config = _temp_config(monkeypatch, tmp_path)
    from core.container import DependencyContainer
    from core.bootstrap import register_cli_tools

    DependencyContainer._instance = None
    container = DependencyContainer.get_instance(config)
    await register_cli_tools(container)

    assert container.has_service("shell"), "shell available at posture 2 too"
    assert container.has_service("self_env"), "self_env must be a CLI container service at posture 2"
