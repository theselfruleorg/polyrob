"""F5/N6: the Polymarket prompt section must only advertise REAL action names.

It drifted: it advertised get_price/get_positions/portfolio_summary/get_balance/
place_market_order — none of which are registered Polymarket actions (the real
names are get_current_price/get_all_positions/get_portfolio_summary, and PM has
no market order). Advertising non-existent tools invites hallucinated calls.
"""
import re
import pytest

from agents.task.agent.prompts import SystemPrompt

# Registered Polymarket action_map keys (tools/polymarket/service.py::execute_action).
_REAL_PM_ACTIONS = {
    "search_markets", "get_trending_markets", "filter_markets_by_category",
    "get_featured_markets", "get_closing_soon_markets", "get_sports_markets",
    "get_crypto_markets", "get_market_details", "get_current_price",
    "get_orderbook", "get_spread", "get_market_volume", "get_all_positions",
    "get_portfolio_summary", "get_trade_history", "place_limit_order",
    "get_open_orders", "get_order_history", "cancel_order", "cancel_all_orders",
}


def _section():
    sp = SystemPrompt(action_description="", mcp_servers={"polymarket": ["search_markets"]})
    return sp._get_polymarket_section()


@pytest.mark.parametrize("bad", [
    "get_price", "get_positions", "portfolio_summary", "get_balance", "place_market_order",
])
def test_no_phantom_polymarket_actions(bad):
    section = _section()
    assert not re.search(r"\b" + re.escape(bad) + r"\b", section), (
        f"prompt advertises non-existent Polymarket action '{bad}'"
    )


@pytest.mark.parametrize("real", [
    "get_current_price", "get_all_positions", "get_portfolio_summary",
    "place_limit_order", "search_markets",
])
def test_advertises_real_polymarket_actions(real):
    assert real in _section()


def test_every_dashed_tool_token_is_a_real_action():
    """Every '- <name> -' / '- <a> / <b> -' bullet must reference real actions."""
    section = _section()
    for line in section.splitlines():
        m = re.match(r"^- ([a-z_/ ]+?) -", line)
        if not m:
            continue
        for token in m.group(1).split("/"):
            token = token.strip()
            assert token in _REAL_PM_ACTIONS, f"unknown advertised action: {token}"


# --- reachability (census, 2026-09-12) --------------------------------------
# The tests above proved the section's CONTENT while the section never rendered:
# they hand-build `mcp_servers={"polymarket": ...}`, which production never
# produces. Prod's `config/mcp_config.json` has `"servers": {}` AND a
# `_polymarket_note` saying polymarket is deliberately NOT an MCP server. So the
# gate tested a condition that is false by design. These pin the gate itself.

def test_the_section_renders_for_a_session_holding_the_polymarket_TOOL():
    """The real production shape: polymarket is a loaded tool_id, no MCP server."""
    sp = SystemPrompt(action_description="", tool_ids=["polymarket"], mcp_servers={})
    assert "Polymarket" in sp._get_polymarket_section()


def test_the_section_stays_dark_without_the_rail():
    sp = SystemPrompt(action_description="", tool_ids=["filesystem"], mcp_servers={})
    assert sp._get_polymarket_section() == ""


def test_the_call_site_gate_is_not_narrower_than_the_section_gate():
    """The block stayed dark because the CALL SITE re-implemented a narrower
    condition (`'polymarket' in self.mcp_servers`) than the function it guarded.
    A caller must not be able to suppress a section the section itself emits."""
    sp = SystemPrompt(action_description="", tool_ids=["polymarket"], mcp_servers={})
    assert sp._get_polymarket_section(), "precondition: the section emits"
    assert "<polymarket>" in sp.get_system_message().content
