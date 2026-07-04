"""Regression: a missing required ${VAR} disables ONLY its server, not all MCP."""
import pytest

from tools.mcp.config import (
    MCPConfig, MCPServerConfig, MCPServerType,
    resolve_config_environment_variables,
)


def _http_server(url):
    return MCPServerConfig(type=MCPServerType.HTTP, url=url)


def test_missing_secret_drops_only_affected_server(monkeypatch):
    monkeypatch.delenv("ROB_TEST_MISSING_SECRET", raising=False)
    monkeypatch.setenv("ROB_TEST_PRESENT", "https://present.example")
    cfg = MCPConfig(
        enabled=True,
        servers={
            "good": _http_server("${ROB_TEST_PRESENT}"),
            "bad": _http_server("${ROB_TEST_MISSING_SECRET}"),
        },
    )
    resolved = resolve_config_environment_variables(cfg)
    # The good server survives; the bad one is dropped (not an all-or-nothing abort).
    assert "good" in resolved.servers
    assert "bad" not in resolved.servers
    assert resolved.servers["good"].url == "https://present.example"


def test_all_present_resolves_all(monkeypatch):
    monkeypatch.setenv("ROB_TEST_A", "https://a.example")
    cfg = MCPConfig(enabled=True, servers={"a": _http_server("${ROB_TEST_A}")})
    resolved = resolve_config_environment_variables(cfg)
    assert resolved.servers["a"].url == "https://a.example"
