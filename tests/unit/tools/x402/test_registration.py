"""Task 10 — gated registration of the x402_pay tool."""


def test_register_x402_tool_noop_when_disabled(monkeypatch):
    monkeypatch.delenv("X402_CLIENT_ENABLED", raising=False)
    from tools.x402 import register_x402_tool
    assert register_x402_tool() is False


def test_register_x402_tool_registers_when_forced():
    from tools.x402 import register_x402_tool
    from tools.descriptors import TOOL_DESCRIPTORS
    assert register_x402_tool(force=True) is True
    assert "x402_pay" in TOOL_DESCRIPTORS
    assert TOOL_DESCRIPTORS["x402_pay"].tool_class is not None
