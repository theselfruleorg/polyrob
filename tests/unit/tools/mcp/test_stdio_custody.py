from unittest.mock import Mock

import pytest

from tools.mcp.protocol import MCPStdioTransport


@pytest.mark.asyncio
@pytest.mark.parametrize("credential", ["AGENT_WALLET_ENABLED", "PAYMENT_MASTER_SEED", "MASTER_SEED"])
async def test_stdio_cannot_spawn_in_custody_process_when_the_owner_refuses(monkeypatch, credential):
    """D1 (2026-10-10): MCP stdio runs under custody by default
    (tests/unit/core/security/test_host_exec_under_custody.py); the owner's
    host_exec_under_custody=refuse row still refuses it."""
    for name in ("AGENT_WALLET_ENABLED", "AGENT_WALLET_MASTER_SEED", "PAYMENT_MASTER_SEED", "MASTER_SEED"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SECURITY_SETTINGS", "host_exec_under_custody=refuse")
    monkeypatch.setenv(credential, "true" if credential == "AGENT_WALLET_ENABLED" else "test-only-seed")
    popen = Mock(side_effect=AssertionError("must not spawn"))
    monkeypatch.setattr("tools.mcp.protocol.subprocess.Popen", popen)
    transport = MCPStdioTransport(command=["python", "server.py"])
    with pytest.raises(Exception, match="custody"):
        await transport.connect()
    popen.assert_not_called()
