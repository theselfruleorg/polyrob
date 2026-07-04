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

# Git tool (P0-D): registers the 'git' descriptor + class only when GIT_TOOLS_ENABLED
# is on (or under POLYROB_LOCAL via _SAFE_LOCAL_FLAGS). Never in the default tool_ids.
try:
    from .git import register_git_tool
    register_git_tool()
except Exception as _e:  # never block tool import on the optional git seam
    logging.getLogger(__name__).debug(f"git registration skipped: {_e}")

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


async def validate_tools(requested_tools: List[str]) -> Tuple[List[str], List[str]]:
    """Validate requested tools.

    Args:
        requested_tools: List of tool names

    Returns:
        Tuple of (valid tools, invalid tools)
    """
    valid = []
    invalid = []

    for tool in requested_tools:
        tool = tool.strip().lower()
        if tool in TOOL_DESCRIPTORS:
            valid.append(tool)
        else:
            invalid.append(tool)

    return valid, invalid


async def get_tool_info(tool_name: str) -> dict:
    """Get metadata for a specific tool.

    Args:
        tool_name: Name of the tool

    Returns:
        Dictionary containing tool metadata
    """
    return get_tool_metadata(tool_name)


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
    'validate_tools',
    'get_tool_info',
    'get_tool_dependencies',
    'get_tool_metadata',
    'get_tool_init_order',
    'register_tool_class',
    'get_tool_class',
]
