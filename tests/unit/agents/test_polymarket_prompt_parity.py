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
