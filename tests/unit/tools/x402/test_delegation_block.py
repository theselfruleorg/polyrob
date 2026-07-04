"""Task 10 — crypto tools are blocked from sub-agent children."""
from tools.controller.delegation import get_blocked_child_tools, narrow_child_tools, LEAF


def test_x402_pay_blocked_for_children():
    assert "x402_pay" in get_blocked_child_tools()


def test_narrow_strips_crypto_tools():
    out = narrow_child_tools(
        parent_tools=["filesystem", "x402_pay", "hyperliquid", "polymarket"],
        requested_tools=None, child_role=LEAF,
    )
    assert out == ["filesystem"]
