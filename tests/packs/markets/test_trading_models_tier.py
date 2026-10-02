"""067 P0.11 / P4: the trading credential models live beside the DB handler.

The handler (``polyrob_markets.<venue>.store``) stores them and must not import
the tool service; the venue ``models`` modules re-export the same objects.
"""

import pytest

pytest.importorskip("polyrob_markets")

import importlib

import pytest


@pytest.mark.parametrize("venue,names", [
    ("hyperliquid", ["TradingLimits", "AgentWallet", "HyperliquidCredentials",
                     "MAINNET_API_URL", "TESTNET_API_URL",
                     "MAINNET_WS_URL", "TESTNET_WS_URL"]),
    ("polymarket", ["TradingLimits", "ApiCredentials", "PolymarketCredentials",
                    "POLYGON_MAINNET", "POLYGON_AMOY_TESTNET",
                    "SIGNATURE_TYPE_EOA", "SIGNATURE_TYPE_MAGIC",
                    "SIGNATURE_TYPE_PROXY"]),
])
def test_tool_models_reexport_the_persistence_tier_objects(venue, names):
    low = importlib.import_module(f"polyrob_markets.{venue}.store_models")
    tool = importlib.import_module(f"polyrob_markets.{venue}.models")
    for name in names:
        assert getattr(tool, name) is getattr(low, name), name


@pytest.mark.parametrize("venue", ["hyperliquid", "polymarket"])
def test_db_handler_does_not_import_the_tool_service(venue):
    import ast
    from pathlib import Path

    src = Path(importlib.import_module(f"polyrob_markets.{venue}.store").__file__)
    for node in ast.walk(ast.parse(src.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith(("tools", f"polyrob_markets.{venue}.service")), node.module
