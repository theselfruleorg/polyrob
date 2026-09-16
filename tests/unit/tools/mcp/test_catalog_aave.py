"""Aave ships an official MCP server; the install catalog should name it.

The catalog is the allowlist `mcp_install` checks: only a NAMED, reviewed entry
can be installed, never an agent-written config. That rule is correct — a
compromised MCP package is the entire threat model — but it means a protocol the
agent SHOULD be able to read stays unreachable until someone reviews it.

Aave's server is the reference case the owner asked about: official, HTTP (not a
local command), and non-custodial by design — it prepares unsigned transactions
and never holds a key, so installing it grants read reach and no spend authority.
"""
import pytest


def test_aave_is_a_named_catalog_entry():
    from tools.mcp.catalog import MCPCatalog
    assert MCPCatalog().get("aave") is not None


def test_aave_is_an_https_network_transport_not_a_local_command():
    """A builtin that shipped a `command` would be a shipped arbitrary exec."""
    from tools.mcp.catalog import MCPCatalog
    entry = MCPCatalog().get("aave")
    assert entry.transport in ("http", "sse")
    assert entry.url and entry.url.startswith("https://")
    assert entry.command is None


def test_no_builtin_catalog_entry_carries_a_local_command():
    """Pins the standing rule for the whole builtin set, not just Aave."""
    from tools.mcp.catalog import _BUILTIN
    offenders = [e.server_id for e in _BUILTIN.values() if e.command]
    assert offenders == [], (
        f"builtin catalog entries must be network transports only: {offenders}")
