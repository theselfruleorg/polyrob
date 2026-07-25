def test_gate_default_off(monkeypatch):
    monkeypatch.delenv("MCP_SERVE_ENABLED", raising=False)
    from api.mcp_serve.router import mcp_serve_enabled
    assert mcp_serve_enabled() is False


def test_gate_on(monkeypatch):
    monkeypatch.setenv("MCP_SERVE_ENABLED", "true")
    from api.mcp_serve.router import mcp_serve_enabled
    assert mcp_serve_enabled() is True
