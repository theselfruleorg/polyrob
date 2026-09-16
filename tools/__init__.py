"""Tools module containing all bot tool implementations.

This module provides the public interface for the tools subsystem.
Tool metadata is defined in tools/descriptors.py (single source of truth).
"""

import logging
from typing import Dict, Any, Type, List, Tuple

# IMPORTANT: Import from core BEFORE base_tool to prime the core package
# This prevents circular import issues when base_tool imports from core.config
from core.container import DependencyContainer
from core.exceptions import ConfigurationError, ToolError

# Import base classes
from .base_tool import BaseTool, ToolStatus

# Import exceptions
from .exceptions import (
    ToolSystemError,
    ToolNotFoundError,
    ActionNotFoundError,
    ActionValidationError,
    ActionExecutionError,
    DuplicateActionError,
    ToolInitializationError,
    ToolCleanupError,
    MCPError,
    MCPServerError,
    MCPToolExecutionError,
    SchemaGenerationError,
)

# Import descriptors (single source of truth for metadata)
from .descriptors import (
    ToolDescriptor,
    ToolCategory,
    TOOL_DESCRIPTORS,
    TOOL_INIT_ORDER,
    TOOL_DEPENDENCIES,
    TOOL_METADATA,
    OPTIONAL_TOOLS,
    AVAILABLE_TOOLS,
    register_tool_class,
    get_tool_class,
    get_tool_dependencies,
    get_tool_metadata,
    get_tool_init_order,
    get_optional_tools,
    get_available_tools,
)

# Import tool implementations
from .filesystem import FileSystem
from .task_tool import TaskTool
from .email_tool import EmailTool
from .perplexity_tool import PerplexityTool
from .web_fetch import WebFetchTool
# Twitter is optional (requires the `tweepy` extra); don't let a missing optional
# dependency break the whole tools import / CLI boot.
try:
    from .twitter_tool import TwitterTool
    _TWITTER_AVAILABLE = True
except ImportError:
    TwitterTool = None
    _TWITTER_AVAILABLE = False
from .collabland.collabland_tool import CollabLandTool
from .alchemy.alchemy_tool import AlchemyTool
from .mcp.mcp_tool import MCPTool
from .anysite.tool import AnysiteTool
# Importing this registers the bridge STATUS reader with core's seam
# (core/wallet/bridge_status.py) so the bridge watcher has one in every
# process that loads the tool tier — not only after a bridge has run.
from .defi.providers import relay_bridge as _relay_bridge  # noqa: F401

# Browser is optional (may not be available in all environments)
try:
    from .browser import Browser
    from .browser.browser_manager import BrowserManager
    _BROWSER_AVAILABLE = True
except ImportError:
    Browser = None
    BrowserManager = None
    _BROWSER_AVAILABLE = False

# Polymarket is optional
try:
    from .polymarket import PolymarketTool, PolymarketDataTool
    _POLYMARKET_AVAILABLE = True
except ImportError:
    PolymarketTool = None
    PolymarketDataTool = None
    _POLYMARKET_AVAILABLE = False

# Hyperliquid is optional
try:
    from .hyperliquid import HyperliquidTool, HyperliquidDataTool
    _HYPERLIQUID_AVAILABLE = True
except ImportError:
    HyperliquidTool = None
    HyperliquidDataTool = None
    _HYPERLIQUID_AVAILABLE = False

# Logger
logger = logging.getLogger(__name__)

# =============================================================================
# TOOL CLASS REGISTRATION
# Register tool classes with their descriptors
# =============================================================================

# Core tools
register_tool_class('filesystem', FileSystem)
register_tool_class('task', TaskTool)

# Communication tools
if _TWITTER_AVAILABLE and TwitterTool is not None:
    register_tool_class('twitter', TwitterTool)
register_tool_class('email', EmailTool)

# Search tools
register_tool_class('perplexity', PerplexityTool)
register_tool_class('web_fetch', WebFetchTool)

# Verification tools
register_tool_class('collabland', CollabLandTool)
register_tool_class('alchemy', AlchemyTool)

# Integration tools
register_tool_class('mcp', MCPTool)
register_tool_class('anysite', AnysiteTool)

# Optional tools (only register if available)
if _BROWSER_AVAILABLE and BrowserManager is not None:
    register_tool_class('browser_manager', BrowserManager)

# Polymarket: Register tool for prediction market access
if _POLYMARKET_AVAILABLE and PolymarketTool is not None:
    register_tool_class('polymarket', PolymarketTool)
    if PolymarketDataTool is not None:
        register_tool_class('polymarket_data', PolymarketDataTool)

# Hyperliquid: Register tool for perpetuals and spot trading
if _HYPERLIQUID_AVAILABLE and HyperliquidTool is not None:
    register_tool_class('hyperliquid', HyperliquidTool)
    if HyperliquidDataTool is not None:
        register_tool_class('hyperliquid_data', HyperliquidDataTool)

# Code execution (Item 3): registers the 'code_execution' descriptor + class only
# when CODE_EXEC_ENABLED=true. OFF by default; never in the default tool_ids.
try:
    from .code_exec import register_code_exec_tool
    register_code_exec_tool()
except Exception as _e:  # never block tool import on the optional code-exec seam
    logging.getLogger(__name__).debug(f"code_exec registration skipped: {_e}")

# Coding tools (H10-B): registers the 'coding' descriptor + class only when
# CODING_TOOLS_ENABLED=true. OFF by default; never in the default tool_ids. Provides
# str_replace/grep/run_tests on top of the code_exec backend (single-user coding agent).
try:
    from .coding import register_coding_tool
    register_coding_tool()
except Exception as _e:  # never block tool import on the optional coding seam
    logging.getLogger(__name__).debug(f"coding registration skipped: {_e}")

# Shell + process tools (computer-use parity WS-2/WS-3): registers the 'shell' +
# 'process' descriptors + classes only when reachable (AGENT_COMPUTE_POSTURE>=1).
# OFF by default; never in the default tool_ids; delegation-blocked. The per-call
# compute_posture_allows gate still applies on every action.
try:
    from .shell import register_shell_tools
    register_shell_tools()
except Exception as _e:  # never block tool import on the optional shell seam
    logging.getLogger(__name__).debug(f"shell registration skipped: {_e}")

# self_env self-maintenance tool (computer-use parity WS-5): registers the 'self_env'
# descriptor + class only at AGENT_COMPUTE_POSTURE>=2. OFF by default; never in the
# default tool_ids; delegation-blocked. Every verb is compute_posture_allows(ctx,2)-
# AND approval-gated (the Controller's posture-2 wiring).
try:
    from .self_env import register_self_env_tool
    register_self_env_tool()
except Exception as _e:  # never block tool import on the optional self_env seam
    logging.getLogger(__name__).debug(f"self_env registration skipped: {_e}")

# Git tool (P0-D): registers the 'git' descriptor + class only when GIT_TOOLS_ENABLED
# is on (or under POLYROB_LOCAL via _SAFE_LOCAL_FLAGS). Never in the default tool_ids.
try:
    from .git import register_git_tool
    register_git_tool()
except Exception as _e:  # never block tool import on the optional git seam
    logging.getLogger(__name__).debug(f"git registration skipped: {_e}")

# x_browser tool (2026-08-18 X rail): registers the 'x_browser' descriptor + class
# only when X_BROWSER_ENABLED=true. OFF by default; never in the default tool_ids;
# delegate_blocked. Browser-based X posting + self-registration.
try:
    from .x_browser import register_x_browser_tool
    register_x_browser_tool()
except Exception as _e:  # never block tool import on the optional x_browser seam
    logging.getLogger(__name__).debug(f"x_browser registration skipped: {_e}")

# GitHub tool (P0-E): registers the 'github' descriptor + class only when
# GITHUB_TOOL_ENABLED is on. OFF by default (even locally); never in the default tool_ids.
try:
    from .github import register_github_tool
    register_github_tool()
except Exception as _e:  # never block tool import on the optional github seam
    logging.getLogger(__name__).debug(f"github registration skipped: {_e}")

# Cron jobs (UP-02): registers the 'cronjob' descriptor + class only when
# CRON_ENABLED=true. OFF by default; never in the default tool_ids. The cron
# subsystem (ticker) is already lifespan-wired in api/app.py — this exposes the
# agent-facing schedule/list/cancel surface so the documented behavior is real.
try:
    from .cronjob_tools import register_cronjob_tool
    register_cronjob_tool()
except Exception as _e:  # never block tool import on the optional cron seam
    logging.getLogger(__name__).debug(f"cronjob registration skipped: {_e}")

# Durable goals (W4): registers the 'goal' descriptor + class only when
# GOALS_ENABLED=true. OFF by default; never in the default tool_ids. The dispatcher
# ticker is lifespan-wired in api/app.py — this exposes the create/list/show/cancel
# surface so an agent that opts into tool_ids=['goal'] can manage durable goals.
try:
    from .goal_tools import register_goal_tool
    register_goal_tool()
except Exception as _e:  # never block tool import on the optional goals seam
    logging.getLogger(__name__).debug(f"goal registration skipped: {_e}")

# Knowledge base (Task 6): registers the 'knowledge' descriptor + class only when
# KB_ENABLED=true (or under POLYROB_LOCAL). OFF by default; never in the default tool_ids.
# Provides kb_ingest/kb_search/kb_list/kb_remove over the tenant-scoped KB.
try:
    from .knowledge_ingest import register_knowledge_tool
    register_knowledge_tool()
except Exception as _e:  # never block tool import on the optional knowledge seam
    logging.getLogger(__name__).debug(f"knowledge registration skipped: {_e}")

# Agent x402 paying (native crypto): registers the 'x402_pay' descriptor + class
# only when X402_CLIENT_ENABLED=true. OFF by default; never in the default tool_ids.
try:
    from .x402 import register_x402_tool
    register_x402_tool()
except Exception as _e:  # never block tool import on the optional x402 seam
    logging.getLogger(__name__).debug(f"x402 registration skipped: {_e}")

# Agent x402 invoicing + accounting (money loop): registers 'x402_invoice'
# only when X402_INVOICE_ENABLED=true. OFF by default; never in the default tool_ids.
try:
    from .x402 import register_x402_invoice_tool
    register_x402_invoice_tool()
except Exception as _e:  # never block tool import on the optional invoicing seam
    logging.getLogger(__name__).debug(f"x402 invoice registration skipped: {_e}")

# Agent DeFi read tier (proposal 023 T0+T1): registers 'defi_data' only when
# DEFI_DATA_ENABLED=true. Read-only — no signer, no broadcast. OFF by default
# and deliberately NOT in the POLYROB_LOCAL safe group (prod runs POLYROB_LOCAL=1
# beside a live mainnet wallet).
try:
    from .defi import register_defi_data_tool
    register_defi_data_tool()
except Exception as _e:  # never block tool import on the optional defi seam
    logging.getLogger(__name__).debug(f"defi_data registration skipped: {_e}")

# Agent DeFi money verbs (proposal 023 T3): registers 'defi_trade' only when
# DEFI_TRADE_ENABLED=true. This one CAN move funds — every call routes through
# core/wallet/tx_guard.py. OFF by default; never in the default tool_ids.
try:
    from .defi import register_defi_trade_tool
    register_defi_trade_tool()
except Exception as _e:  # never block tool import on the optional defi seam
    logging.getLogger(__name__).debug(f"defi_trade registration skipped: {_e}")

# Launchpad tool (042): registers the 'launchpad' descriptor + class only when
# LAUNCHPAD_ENABLED=true. It signs spends -- every write routes through
# core/wallet/tx_guard.py. OFF by default; never in the default tool_ids.
try:
    from .launchpad import register_launchpad_tool
    register_launchpad_tool()
except Exception as _e:  # never block tool import on the optional launchpad seam
    logging.getLogger(__name__).debug(f"launchpad registration skipped: {_e}")

# Dapp browser wallet (042): registers the 'dapp_browser' descriptor + class
# only when DAPP_BROWSER_ENABLED=true. `dapp_connect` authorizes spending from a
# web page; every transaction still routes through core/wallet/tx_guard.py.
# OFF by default; never in the default tool_ids.
try:
    from .dapp_browser import register_dapp_browser_tool
    register_dapp_browser_tool()
except Exception as _e:  # never block tool import on the optional dapp seam
    logging.getLogger(__name__).debug(f"dapp_browser registration skipped: {_e}")

# Build TOOL_COMPONENTS for backward compatibility
TOOL_COMPONENTS: List[Tuple[str, Type[BaseTool]]] = [
    (name, desc.tool_class)
    for name, desc in TOOL_DESCRIPTORS.items()
    if desc.tool_class is not None
]


# =============================================================================
# INITIALIZATION HELPERS
# =============================================================================

async def initialize_tool(
    tool_name: str,
    tool: BaseTool,
    logger: logging.Logger
) -> bool:
    """Initialize a tool with proper error handling and dependency injection."""
    try:
        descriptor = TOOL_DESCRIPTORS.get(tool_name)
        if not descriptor:
            logger.warning(f"No descriptor found for tool '{tool_name}'")
            # Fall back to basic initialization
            await tool.initialize()
            return tool.status == ToolStatus.HEALTHY

        # Check if tool is enabled
        if not tool.enabled:
            logger.warning(f"{tool_name} tool is disabled, skipping initialization")
            return False

        # For rate-limited tools, configure rate limiter first
        if descriptor.rate_limited:
            rate_limiter = tool.container.get_service('rate_limit_manager')
            if not rate_limiter:
                logger.error(f"{tool_name} requires rate limiter but it's not available")
                return False

            # Configure rate limits from descriptor
            if descriptor.rate_limit_settings:
                await rate_limiter.configure_limits(
                    tool_name,
                    descriptor.rate_limit_settings.get('requests_per_minute', 300),
                    descriptor.rate_limit_settings.get('burst_limit', 50),
                    descriptor.rate_limit_settings.get('default_wait', 900)
                )

        # Initialize tool
        try:
            await tool.initialize()
            if tool.status == ToolStatus.HEALTHY:
                logger.info(f"✓ {tool_name} initialized successfully")
                return True
            else:
                logger.error(
                    f"Tool {tool_name} failed to initialize: {tool.error_message}"
                )
                return False

        except Exception as e:
            logger.error(f"Failed to initialize {tool_name}: {e}")
            return False

    except Exception as e:
        logger.error(f"Error during {tool_name} initialization: {e}")
        return False


async def cleanup_tools(tools: Dict[str, Any]) -> None:
    """Clean up tools in reverse initialization order."""
    cleanup_logger = logging.getLogger("tools")

    # Get initialization order and reverse it
    cleanup_order = list(reversed(get_tool_init_order()))

    # Add any tools not in the order (shouldn't happen but be safe)
    for tool_name in tools.keys():
        if tool_name not in cleanup_order:
            cleanup_order.append(tool_name)

    # Clean up in order
    for tool_name in cleanup_order:
        if tool_name in tools:
            try:
                await tools[tool_name].cleanup()
                cleanup_logger.info(f"Cleaned up tool: {tool_name}")
            except Exception as e:
                cleanup_logger.error(f"Error cleaning up tool {tool_name}: {str(e)}")


# =============================================================================
# PUBLIC API
# =============================================================================

__all__ = [
    # Base classes
    'BaseTool',
    'ToolStatus',

    # Exceptions
    'ToolSystemError',
    'ToolNotFoundError',
    'ActionNotFoundError',
    'ActionValidationError',
    'ActionExecutionError',
    'DuplicateActionError',
    'ToolInitializationError',
    'ToolCleanupError',
    'MCPError',
    'MCPServerError',
    'MCPToolExecutionError',
    'SchemaGenerationError',

    # Descriptors
    'ToolDescriptor',
    'ToolCategory',
    'TOOL_DESCRIPTORS',
    'TOOL_COMPONENTS',
    'TOOL_DEPENDENCIES',
    'TOOL_METADATA',
    'TOOL_INIT_ORDER',
    'OPTIONAL_TOOLS',
    'AVAILABLE_TOOLS',

    # Tool classes
    'FileSystem',
    'TaskTool',
    'TwitterTool',
    'PerplexityTool',
    'EmailTool',
    'CollabLandTool',
    'AlchemyTool',
    'MCPTool',
    'AnysiteTool',
    'PolymarketTool',
    'HyperliquidTool',

    # Functions
    'initialize_tool',
    'cleanup_tools',
    'get_tool_dependencies',
    'get_tool_metadata',
    'get_tool_init_order',
    'register_tool_class',
    'get_tool_class',
]
