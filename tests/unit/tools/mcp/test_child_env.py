"""MCP stdio children must not inherit the process environment.

H2 (audit 2026-08-22): tools/mcp/protocol.py did
`{**os.environ, **(self.env or {})}`, so every stdio MCP server the agent
installs inherited AGENT_WALLET_MASTER_SEED, EIP8004_AGENT_PRIVATE_KEY,
PAYMENT_MASTER_SEED and every API key.

Unlike code_exec's build_child_env, an EXPLICITLY CONFIGURED secret must still
pass: config/mcp_config.json legitimately carries MCP_GATEWAY_TOKEN / ANYSITE_JWT
(resolved from ${VAR} by tools/mcp/config.py). The operator wrote that file; the
agent did not write os.environ. Ambient inheritance is what is refused.
"""
import pytest

from tools.mcp.child_env import build_mcp_child_env


def test_wallet_seed_is_never_inherited(monkeypatch):
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", "correct horse battery staple")
    env = build_mcp_child_env(None)
    assert "AGENT_WALLET_MASTER_SEED" not in env


@pytest.mark.parametrize("name", [
    "AGENT_WALLET_MASTER_SEED", "PAYMENT_MASTER_SEED", "MASTER_SEED",
    "EIP8004_AGENT_PRIVATE_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
    "TELEGRAM_BOT_TOKEN", "GMAIL_PASSWORD", "SOME_SECRET", "AWS_ACCESS_KEY_ID",
    "WALLET_MNEMONIC", "DB_CREDENTIAL",
])
def test_no_secret_named_ambient_var_is_inherited(monkeypatch, name):
    monkeypatch.setenv(name, "leaked")
    assert name not in build_mcp_child_env(None)


def test_no_non_allowlisted_ambient_var_is_inherited(monkeypatch):
    """Deny by default: even a harmless-looking ambient var does not pass."""
    monkeypatch.setenv("POLYROB_DATA_DIR", "/var/lib/polyrob")
    monkeypatch.setenv("SOMETHING_ARBITRARY", "x")
    env = build_mcp_child_env(None)
    assert "SOMETHING_ARBITRARY" not in env
    assert "POLYROB_DATA_DIR" not in env


def test_process_launch_vars_survive(monkeypatch):
    """npx/uvx-launched servers need these or they cannot start."""
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/rob")
    env = build_mcp_child_env(None)
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == "/home/rob"


def test_explicitly_configured_secret_is_passed(monkeypatch):
    """The operator's mcp_config.json is the sanctioned channel — this is what
    makes an authenticated MCP server work at all."""
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", "leaked")
    env = build_mcp_child_env({"MCP_GATEWAY_TOKEN": "gw-123", "ANYSITE_JWT": "jwt-456"})
    assert env["MCP_GATEWAY_TOKEN"] == "gw-123"
    assert env["ANYSITE_JWT"] == "jwt-456"
    assert "AGENT_WALLET_MASTER_SEED" not in env


def test_configured_value_wins_over_an_allowlisted_ambient_one(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    env = build_mcp_child_env({"PATH": "/opt/custom/bin"})
    assert env["PATH"] == "/opt/custom/bin"


def test_configured_values_are_stringified():
    env = build_mcp_child_env({"PORT": 8080, "FLAG": True})
    assert env["PORT"] == "8080"
    assert env["FLAG"] == "True"


def test_windows_launch_vars_are_allowlisted():
    from tools.mcp.child_env import MCP_ENV_ALLOWLIST
    for name in ("SystemRoot", "APPDATA", "LOCALAPPDATA", "COMSPEC", "PATHEXT"):
        assert name in MCP_ENV_ALLOWLIST, f"{name} missing — npx fails on Windows"
