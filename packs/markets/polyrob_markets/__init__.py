"""The markets pack (067 P4): prediction markets and perps — Polymarket and
Hyperliquid. Each venue has a gated trade tool (``polymarket``,
``hyperliquid``) and a read-only data tool (``polymarket_data``,
``hyperliquid_data``), a credential/audit store, API routes and skills.

The code half of the pack. Importing this module is cheap (core only); the tool
modules are named by reference and imported in the loader's phase 2. The policy
rows (capabilities, per-action policy incl. the owner approval lanes and the
correspondent blocks, catalog permissions) are in ``pack.toml`` and register in
phase 1, before any pack code runs.

The API routers mount under ``/api/packs/markets`` (the loader's prefix):
``/api/packs/markets/polymarket/*`` and ``/api/packs/markets/hyperliquid/*``.
"""
from pathlib import Path

from core.packs.spec import PackSpec, ToolContribution


def pack() -> PackSpec:
    return PackSpec(
        id="markets",
        tools=(
            ToolContribution(id="polymarket_data",
                             registrar="polyrob_markets.registration:register_polymarket_data_tool"),
            ToolContribution(id="polymarket",
                             registrar="polyrob_markets.registration:register_polymarket_tool"),
            ToolContribution(id="hyperliquid_data",
                             registrar="polyrob_markets.registration:register_hyperliquid_data_tool"),
            ToolContribution(id="hyperliquid",
                             registrar="polyrob_markets.registration:register_hyperliquid_tool"),
        ),
        api_routers=("polyrob_markets.polymarket.routes:router",
                     "polyrob_markets.hyperliquid.routes:router"),
        skills_dir=Path(__file__).parent / "skills",
    )
