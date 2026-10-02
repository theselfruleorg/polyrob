"""Review C10 (2026-09-29): the markets pack's OWN-ACCOUNT reads (balance,
positions, orders, fills, trade history) stayed open while a correspondent had
tainted the session, though defi_data's holdings reads are blocked
(defi_data_portfolio). They are the reconnaissance before a drain."""
import tomllib
from pathlib import Path

_TOML = (Path(__file__).resolve().parents[4] / "packs" / "markets" / "polyrob_markets"
         / "pack.toml")

_OWN = {
    "polymarket": ["get_all_positions", "get_balance", "get_open_orders",
                   "get_order_history", "get_portfolio_summary", "get_trade_history"],
    "polymarket_data": ["get_all_positions", "get_portfolio_summary", "get_trade_history"],
    "hyperliquid": ["agent_status", "get_account_state", "get_fills", "get_open_orders",
                    "get_spot_balances"],
    "hyperliquid_data": ["agent_status", "get_account_state", "get_fills",
                         "get_open_orders", "get_spot_balances"],
}


def _verbs(tool):
    data = tomllib.loads(_TOML.read_text(encoding="utf-8"))
    return data["tools"][tool]["verbs"]


def test_own_account_reads_are_correspondent_blocked():
    for tool, names in _OWN.items():
        verbs = _verbs(tool)
        for n in names:
            assert verbs[f"{tool}_{n}"].get("correspondent_blocked") is True, (tool, n)


def test_market_data_reads_stay_open():
    for tool, name in (("polymarket_data", "get_orderbook"),
                       ("hyperliquid_data", "get_all_mids")):
        assert not _verbs(tool)[f"{tool}_{name}"].get("correspondent_blocked")
