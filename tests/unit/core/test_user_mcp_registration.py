"""The per-tenant MCP server store must exist wherever MCP runs.

`user_mcp_service` is the ONLY durable home for "the owner added this MCP
server" — the row `MCPTool.load_user_servers` reads at session start. Before
this test it was registered inside `initialize_auth_services`, behind
`ENABLE_AUTH` (the *billing* gate, default OFF) and only on the server boot
path. Prod (2026-09-12: `ENABLE_AUTH` unset on the live box) and every CLI
session therefore had no store at all, so an MCP server the owner added could
not persist even in principle.

Per-tenant MCP config has nothing to do with billing. The gate is MCP.
"""
import asyncio

import pytest

from core.config import BotConfig


class _StubDBManager:
    """The real DatabaseManager exposes a live aiosqlite connection; an
    in-memory one is enough for ``ensure_tables`` to do real DDL."""

    def __init__(self, connection):
        self.tables = {}
        self.user_profiles = None
        self.connection = connection


class _StubContainer:
    def __init__(self, config, connection=None):
        self.config = config
        self.registered = {}
        self._services = {"database_manager": _StubDBManager(connection)}

    def get_service(self, name):
        return self._services.get(name)

    def register_service(self, name, service):
        self.registered[name] = service
        self._services[name] = service


def _config(monkeypatch, *, mcp_enabled: bool, enable_auth: bool) -> BotConfig:
    monkeypatch.setenv("MCP_ENABLED", "true" if mcp_enabled else "false")
    monkeypatch.setenv("ENABLE_AUTH", "true" if enable_auth else "false")
    cfg = BotConfig()
    cfg.mcp_enabled = mcp_enabled
    cfg.enable_auth = enable_auth
    return cfg


def test_user_mcp_service_registers_with_auth_off(monkeypatch):
    """The live prod shape: MCP on, ENABLE_AUTH off. The store must exist."""
    import aiosqlite

    from core.initialization import initialize_user_mcp_service

    async def _run():
        async with aiosqlite.connect(":memory:") as conn:
            container = _StubContainer(
                _config(monkeypatch, mcp_enabled=True, enable_auth=False), conn)
            await initialize_user_mcp_service(container)
            return container

    container = asyncio.run(_run())

    assert "user_mcp_service" in container.registered, (
        "per-tenant MCP config is not a billing feature — ENABLE_AUTH must not "
        "decide whether the owner's MCP servers can persist")


def test_user_mcp_service_skipped_when_mcp_disabled(monkeypatch):
    from core.initialization import initialize_user_mcp_service

    container = _StubContainer(_config(monkeypatch, mcp_enabled=False, enable_auth=True))
    asyncio.run(initialize_user_mcp_service(container))

    assert "user_mcp_service" not in container.registered


def test_user_mcp_service_is_fail_open_without_a_database(monkeypatch):
    """No DB (a degraded CLI boot) must not kill startup — just no store."""
    from core.initialization import initialize_user_mcp_service

    container = _StubContainer(_config(monkeypatch, mcp_enabled=True, enable_auth=False))
    container._services["database_manager"] = None
    asyncio.run(initialize_user_mcp_service(container))  # must not raise

    assert "user_mcp_service" not in container.registered


@pytest.mark.asyncio
async def test_cli_container_registers_the_store_when_mcp_is_on(monkeypatch, tmp_path):
    """`build_cli_container` never called auth-services, so the terminal-native
    agent had no per-tenant MCP store at all — an MCP server added from the REPL
    could not outlive the process."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MCP_ENABLED", "true")
    from core.container import DependencyContainer
    from core.bootstrap import build_cli_container

    DependencyContainer._instance = None
    container = await build_cli_container(require_llm=False)

    assert container.has_service("user_mcp_service"), (
        "the CLI boot path must register the per-tenant MCP store, or nothing "
        "the owner adds from the REPL can persist")
