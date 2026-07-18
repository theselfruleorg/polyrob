"""Regression (P0): ~30 bool Fields on AgentConfig/ServerConfig crashed
construction on the codebase's own canonical falsey token "none" — only 5
memory fields had a coercing validator. A value like MCP_ENABLED=none raised
pydantic ValidationError at ServerConfig()/AgentConfig() construction. Every
bool field must accept the POLYROB falsey-set (none/off/false/0/no/'').
"""
import pytest

from core.config import ServerConfig, AgentConfig


@pytest.mark.parametrize("falsey", ["none", "off", "false", "0", "no", ""])
def test_bool_fields_accept_falsey_tokens_without_crashing(monkeypatch, falsey):
    monkeypatch.setenv("MCP_ENABLED", falsey)
    monkeypatch.setenv("SUB_AGENTS_ENABLED", falsey)
    monkeypatch.setenv("SSL_VERIFY", falsey)
    cfg = ServerConfig()  # must NOT raise
    assert cfg.mcp_enabled is False
    assert cfg.sub_agents_enabled is False
    assert cfg.ssl_verify is False


@pytest.mark.parametrize("truthy", ["true", "1", "yes", "on"])
def test_bool_fields_accept_truthy_tokens(monkeypatch, truthy):
    monkeypatch.setenv("MCP_ENABLED", truthy)
    cfg = ServerConfig()
    assert cfg.mcp_enabled is True


def test_agent_config_none_does_not_crash(monkeypatch):
    # sub_agents_enabled is a bool field on the BASE AgentConfig (mcp_enabled is
    # ServerConfig-only). The base class must coerce falsey tokens too.
    monkeypatch.setenv("SUB_AGENTS_ENABLED", "none")
    cfg = AgentConfig()  # must NOT raise
    assert cfg.sub_agents_enabled is False


def test_memory_flags_still_coerced(monkeypatch):
    # The 5 previously-covered memory flags must keep working (no regression).
    monkeypatch.setenv("HIERARCHICAL_MEMORY_ENABLED", "none")
    cfg = ServerConfig()
    assert cfg.HIERARCHICAL_MEMORY_ENABLED is False
