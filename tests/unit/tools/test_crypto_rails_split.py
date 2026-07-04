"""T5 — the read tools are low-risk/delegatable/in toolsets; the trade tools keep
every gate."""
from core.tool_catalog import _PERMISSIONS, _HIGH_RISK_TOOLS
from tools.controller.delegation import DELEGATE_BLOCKED_TOOLS
from agents.task.agent.skill_manager import VALID_TOOL_IDS
from agents.task.tool_defaults import TOOLSETS
from agents.task.agent.core.correspondent_gate import HIGH_IMPACT_TOOLS

READ = ("polymarket_data", "hyperliquid_data")
TRADE = ("polymarket", "hyperliquid")


def test_read_tools_are_read_only_capability():
    for t in READ:
        assert _PERMISSIONS.get(t) == ["network.read"]


def test_read_tools_not_high_risk_but_trade_tools_are():
    for t in READ:
        assert t not in _HIGH_RISK_TOOLS
    for t in TRADE:
        assert t in _HIGH_RISK_TOOLS


def test_read_tools_delegatable_trade_tools_blocked():
    for t in READ:
        assert t not in DELEGATE_BLOCKED_TOOLS
    for t in TRADE:
        assert t in DELEGATE_BLOCKED_TOOLS


def test_read_tools_in_valid_ids_and_toolsets():
    for t in READ:
        assert t in VALID_TOOL_IDS
    assert set(READ) <= set(TOOLSETS["trading_research"])
    assert set(READ) <= set(TOOLSETS["research"])


def test_read_tools_not_correspondent_blocked_trade_tools_are():
    for t in READ:
        assert t not in HIGH_IMPACT_TOOLS
    for t in TRADE:
        assert t in HIGH_IMPACT_TOOLS
