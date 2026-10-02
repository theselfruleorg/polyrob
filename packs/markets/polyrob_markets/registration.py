"""Phase-2 registrars of the markets pack (067 P4): the descriptors and classes
``tools/descriptors.py`` / ``tools/__init__.py`` used to hold statically. The
tool modules are imported inside the registrars, so a broken venue import drops
that venue's tools instead of refusing the whole pack. Both venue SDKs are
optional at import (the services degrade to a typed "client missing" error)."""
import logging

logger = logging.getLogger(__name__)


def _register(name, cls_path, descriptor_kwargs, force: bool) -> bool:
    import importlib
    module, _, attr = cls_path.partition(":")
    try:
        tool_cls = getattr(importlib.import_module(module), attr)
    except ImportError as exc:
        logger.debug("%s tool not registered: %s", name, exc)
        return False
    from tools.descriptors import ToolCategory, ToolDescriptor, register_optional_tool
    return register_optional_tool(
        name, tool_cls,
        ToolDescriptor(name=name, category=ToolCategory.INTEGRATION,
                       required_services=["rate_limit_manager"], required_config=[],
                       is_optional=True, rate_limited=True, **descriptor_kwargs),
        lambda: True, force=force)


def register_polymarket_data_tool(force: bool = False) -> bool:
    return _register("polymarket_data", "polyrob_markets.polymarket.service:PolymarketDataTool", dict(
        description="Polymarket prediction markets - read-only market data & research (no wallet)",
        optional_services=["cache_manager"],
        init_priority=53,  # before the trade tool
        rate_limit_settings={"requests_per_minute": 60, "burst_limit": 10, "default_wait": 60},
    ), force)


def register_polymarket_tool(force: bool = False) -> bool:
    return _register("polymarket", "polyrob_markets.polymarket.service:PolymarketTool", dict(
        description="Polymarket prediction markets - trading and market data",
        optional_services=["cache_manager", "database_manager"],
        init_priority=55,  # credentials stored in the DB, not config
        rate_limit_settings={"requests_per_minute": 60, "burst_limit": 10, "default_wait": 60},
    ), force)


def register_hyperliquid_data_tool(force: bool = False) -> bool:
    return _register("hyperliquid_data", "polyrob_markets.hyperliquid.service:HyperliquidDataTool", dict(
        description="Hyperliquid perps/spot - read-only market data & account state (no signing)",
        optional_services=["cache_manager"],
        init_priority=54,  # before the trade tool
        rate_limit_settings={"requests_per_minute": 120, "burst_limit": 20, "default_wait": 30},
    ), force)


def register_hyperliquid_tool(force: bool = False) -> bool:
    return _register("hyperliquid", "polyrob_markets.hyperliquid.service:HyperliquidTool", dict(
        description="Hyperliquid perpetuals and spot trading - market data and execution",
        optional_services=["cache_manager", "database_manager"],
        init_priority=56,  # after polymarket; credentials stored in the DB
        rate_limit_settings={"requests_per_minute": 120, "burst_limit": 20, "default_wait": 30},
    ), force)
