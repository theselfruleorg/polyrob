"""The CLI container must register the dependency-free tools a session can load.

Root cause (debugged 2026-06-05): `build_cli_container` registered only llm +
task_agent, so a session's controller `load_tools_from_container(['filesystem',...])`
found nothing and `polyrob run --tools filesystem` silently had only the core
done/send_message actions ("✗ Tool 'filesystem' not found in container").
"""
import pytest


def _temp_config(monkeypatch, tmp_path):
    # Avoid the production /opt/rob paths baked into the dev env file.
    for k in ("DATA_DIR", "DATA_ROOT", "CHARACTERS_DIR", "KNOWLEDGE_DIR",
              "CACHE_DIR", "DB_PATH", "TELEMETRY_DATA_DIR"):
        monkeypatch.setenv(k, str(tmp_path / k.lower()))
    from core.config import BotConfig
    return BotConfig()


@pytest.mark.asyncio
async def test_register_cli_tools_registers_tools_and_their_dependency(monkeypatch, tmp_path):
    config = _temp_config(monkeypatch, tmp_path)
    from core.container import DependencyContainer
    from core.bootstrap import register_cli_tools

    DependencyContainer._instance = None
    container = DependencyContainer.get_instance(config)

    await register_cli_tools(container)

    assert container.has_service("filesystem"), "filesystem tool must be in the CLI container"
    assert container.has_service("task"), "task tool must be in the CLI container"
    # filesystem/task declare rate_limit_manager as a required dependency.
    assert container.has_service("rate_limit_manager"), "tools' dependency must be registered too"


@pytest.mark.asyncio
async def test_cli_filesystem_tool_initializes(monkeypatch, tmp_path):
    """The real failure: the tool was found but initialize() raised
    'Required dependency rate_limit_manager not available'. It must init cleanly."""
    config = _temp_config(monkeypatch, tmp_path)
    from core.container import DependencyContainer
    from core.bootstrap import register_cli_tools

    DependencyContainer._instance = None
    container = DependencyContainer.get_instance(config)
    await register_cli_tools(container)

    fs = container.get_service("filesystem")
    await fs.initialize()  # must NOT raise
    assert getattr(fs, "is_initialized", False) is True
