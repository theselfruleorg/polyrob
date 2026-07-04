"""Single source of truth for tool metadata and configuration.

This module consolidates all tool-related constants that were previously
duplicated between tools/__init__.py and core/initialization.py.

IMPORTANT: This is the ONLY place tool metadata should be defined.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Type, Any, Optional, TYPE_CHECKING
from enum import Enum

if TYPE_CHECKING:
    from tools.base_tool import BaseTool


class ToolCategory(Enum):
    """Tool categories for organization."""
    CORE = "core"              # Essential tools (filesystem, task)
    BROWSER = "browser"        # Browser automation
    COMMUNICATION = "comm"     # Email, social media
    SEARCH = "search"          # Search and research
    VERIFICATION = "verify"    # Token gating, identity
    INTEGRATION = "integration"  # MCP, external APIs


@dataclass
class ToolDescriptor:
    """Complete descriptor for a tool.

    This replaces the previous scattered definitions:
    - TOOL_COMPONENTS (name, class)
    - TOOL_DEPENDENCIES (required, optional)
    - TOOL_METADATA (description, requires_config, etc.)
    - TOOL_INIT_ORDER (via init_priority)
    """

    name: str
    description: str
    category: ToolCategory

    # Dependencies - services this tool needs
    required_services: List[str] = field(default_factory=list)
    optional_services: List[str] = field(default_factory=list)

    # Configuration requirements
    required_config: List[str] = field(default_factory=list)

    # Initialization
    init_priority: int = 50  # Lower = earlier (0-100)
    is_optional: bool = True  # Can system start without it?

    # Rate limiting
    rate_limited: bool = False
    rate_limit_settings: Dict[str, Any] = field(default_factory=dict)

    # Class reference (set during registration)
    tool_class: Optional[Type["BaseTool"]] = None

    def __post_init__(self):
        # Ensure rate_limit_manager is always required if rate_limited
        if self.rate_limited and 'rate_limit_manager' not in self.required_services:
            self.required_services = ['rate_limit_manager'] + self.required_services


# =============================================================================
# TOOL REGISTRY - Single Source of Truth
# =============================================================================

TOOL_DESCRIPTORS: Dict[str, ToolDescriptor] = {
    # ---------------------------------------------------------------------
    # CORE TOOLS (init_priority 0-19)
    # ---------------------------------------------------------------------
    'filesystem': ToolDescriptor(
        name='filesystem',
        description='File system operations and document processing',
        category=ToolCategory.CORE,
        required_services=['rate_limit_manager'],
        optional_services=['llm_client', 'cache_manager'],
        required_config=[],
        init_priority=10,
        is_optional=False,  # Required tool
    ),

    'task': ToolDescriptor(
        name='task',
        description='Task and todo list management',
        category=ToolCategory.CORE,
        required_services=['rate_limit_manager'],
        optional_services=[],
        required_config=[],
        init_priority=15,
        is_optional=True,
    ),

    # ---------------------------------------------------------------------
    # BROWSER TOOLS (init_priority 5 - needs to be early for pooling)
    # ---------------------------------------------------------------------
    'browser_manager': ToolDescriptor(
        name='browser_manager',
        description='Browser lifecycle and context pooling manager',
        category=ToolCategory.BROWSER,
        required_services=[],
        optional_services=[],
        required_config=[],
        init_priority=5,  # Very early - browser pool
        is_optional=True,
    ),

    # ---------------------------------------------------------------------
    # SEARCH TOOLS (init_priority 20-29)
    # ---------------------------------------------------------------------
    'perplexity': ToolDescriptor(
        name='perplexity',
        description='AI-powered search via Perplexity API',
        category=ToolCategory.SEARCH,
        required_services=['rate_limit_manager'],
        optional_services=['cache_manager'],
        required_config=['perplexity_api_key'],
        init_priority=20,
        is_optional=True,
    ),

    'web_fetch': ToolDescriptor(
        name='web_fetch',
        description='Stateless web page reader (URL -> markdown). No browser/Chromium.',
        category=ToolCategory.SEARCH,
        required_services=[],
        optional_services=['cache_manager'],
        required_config=[],
        init_priority=21,
        is_optional=True,
    ),

    # ---------------------------------------------------------------------
    # COMMUNICATION TOOLS (init_priority 30-39)
    # ---------------------------------------------------------------------
    'twitter': ToolDescriptor(
        name='twitter',
        description='Twitter/X account actions: post/media/poll/thread, reply/quote/delete, like/retweet, follow/mute/block, DM, mentions (gated by TWITTER_ENABLED). For Twitter/X DATA RETRIEVAL prefer the anysite tool to conserve quota.',
        category=ToolCategory.COMMUNICATION,
        required_services=['rate_limit_manager', 'database_manager'],
        optional_services=['cache_manager'],
        required_config=[],  # Uses OAuth tokens from DB
        init_priority=30,
        is_optional=True,
        rate_limited=True,
        rate_limit_settings={
            'default_wait': 900,  # 15 minutes
            'requests_per_minute': 300,
            'burst_limit': 50
        }
    ),

    'email': ToolDescriptor(
        name='email',
        description='Email sending via Gmail',
        category=ToolCategory.COMMUNICATION,
        required_services=['rate_limit_manager'],
        optional_services=[],
        required_config=['gmail_email', 'gmail_app_password'],
        init_priority=35,
        is_optional=True,
    ),

    # ---------------------------------------------------------------------
    # VERIFICATION TOOLS (init_priority 40-49)
    # ---------------------------------------------------------------------
    'collabland': ToolDescriptor(
        name='collabland',
        description='CollabLand token gating verification',
        category=ToolCategory.VERIFICATION,
        required_services=['rate_limit_manager', 'database_manager'],
        optional_services=['cache_manager'],
        required_config=['collabland_api_key'],
        init_priority=40,
        is_optional=True,
    ),

    'alchemy': ToolDescriptor(
        name='alchemy',
        description='Alchemy NFT API for token verification',
        category=ToolCategory.VERIFICATION,
        required_services=['rate_limit_manager'],
        optional_services=['cache_manager'],
        required_config=['alchemy_api_key'],
        init_priority=45,
        is_optional=True,
    ),

    # ---------------------------------------------------------------------
    # INTEGRATION TOOLS (init_priority 50-59)
    # ---------------------------------------------------------------------
    'mcp': ToolDescriptor(
        name='mcp',
        description='Model Context Protocol server integration',
        category=ToolCategory.INTEGRATION,
        required_services=['rate_limit_manager'],
        optional_services=['cache_manager'],
        required_config=[],  # MCP config is separate
        init_priority=50,
        is_optional=True,
    ),

    'anysite': ToolDescriptor(
        name='anysite',
        description="Query AnySite's 200+ sources / 1,200+ endpoints (LinkedIn, X, Reddit, YouTube, GitHub, SEC, web scraper) via the anysite CLI",
        category=ToolCategory.INTEGRATION,
        is_optional=True,
        init_priority=52,
    ),

    'polymarket_data': ToolDescriptor(
        name='polymarket_data',
        description='Polymarket prediction markets - read-only market data & research (no wallet)',
        category=ToolCategory.INTEGRATION,
        required_services=['rate_limit_manager'],
        optional_services=['cache_manager'],
        required_config=[],
        init_priority=53,  # before the trade tool
        is_optional=True,
        rate_limited=True,
        rate_limit_settings={'requests_per_minute': 60, 'burst_limit': 10, 'default_wait': 60},
    ),

    'polymarket': ToolDescriptor(
        name='polymarket',
        description='Polymarket prediction markets - trading and market data',
        category=ToolCategory.INTEGRATION,
        required_services=['rate_limit_manager'],
        optional_services=['cache_manager', 'database_manager'],
        required_config=[],  # Credentials stored in DB, not config
        init_priority=55,  # After MCP
        is_optional=True,
        rate_limited=True,
        rate_limit_settings={
            'requests_per_minute': 60,
            'burst_limit': 10,
            'default_wait': 60
        }
    ),

    'hyperliquid_data': ToolDescriptor(
        name='hyperliquid_data',
        description='Hyperliquid perps/spot - read-only market data & account state (no signing)',
        category=ToolCategory.INTEGRATION,
        required_services=['rate_limit_manager'],
        optional_services=['cache_manager'],
        required_config=[],
        init_priority=54,  # before the trade tool
        is_optional=True,
        rate_limited=True,
        rate_limit_settings={'requests_per_minute': 120, 'burst_limit': 20, 'default_wait': 30},
    ),

    'hyperliquid': ToolDescriptor(
        name='hyperliquid',
        description='Hyperliquid perpetuals and spot trading - market data and execution',
        category=ToolCategory.INTEGRATION,
        required_services=['rate_limit_manager'],
        optional_services=['cache_manager', 'database_manager'],
        required_config=[],  # Credentials stored in DB, not config
        init_priority=56,  # After polymarket
        is_optional=True,
        rate_limited=True,
        rate_limit_settings={
            'requests_per_minute': 120,
            'burst_limit': 20,
            'default_wait': 30
        }
    ),
}


# =============================================================================
# DERIVED CONSTANTS (for backward compatibility)
# =============================================================================

def get_tool_init_order() -> List[str]:
    """Get tools in initialization order (sorted by init_priority)."""
    return sorted(
        TOOL_DESCRIPTORS.keys(),
        key=lambda name: TOOL_DESCRIPTORS[name].init_priority
    )


def get_tool_dependencies(tool_name: str) -> Dict[str, List[str]]:
    """Get dependencies for a tool (backward compatible format)."""
    if tool_name not in TOOL_DESCRIPTORS:
        return {'required': [], 'optional': []}
    desc = TOOL_DESCRIPTORS[tool_name]
    return {
        'required': desc.required_services,
        'optional': desc.optional_services
    }


def get_tool_metadata(tool_name: str) -> Dict[str, Any]:
    """Get metadata for a tool (backward compatible format)."""
    if tool_name not in TOOL_DESCRIPTORS:
        return {
            'name': tool_name.title(),
            'description': 'No description available',
            'requires_config': [],
            'required': False
        }
    desc = TOOL_DESCRIPTORS[tool_name]
    return {
        'name': desc.name,
        'description': desc.description,
        'requires_config': desc.required_config,
        'required': not desc.is_optional,
        'dependencies': desc.required_services,
        'optional_dependencies': desc.optional_services,
        'rate_limited': desc.rate_limited,
        'rate_limit_settings': desc.rate_limit_settings,
        'is_core': desc.category == ToolCategory.CORE
    }


def get_optional_tools() -> set:
    """Get set of optional tool names."""
    return {
        name for name, desc in TOOL_DESCRIPTORS.items()
        if desc.is_optional
    }


def get_available_tools() -> Dict[str, str]:
    """Get dict of tool name -> description."""
    return {
        name: desc.description
        for name, desc in TOOL_DESCRIPTORS.items()
    }


def get_default_tools() -> List[str]:
    """Get list of tools that should be enabled by default in UI.

    These are the baseline tools that provide essential functionality.
    Returns both the canonical name and any aliases (e.g., 'browser' for 'browser_manager').
    """
    # Core tools always enabled
    defaults = ['filesystem', 'browser']
    return defaults


def get_agent_usable_tools() -> Dict[str, ToolDescriptor]:
    """Get tools that should appear in the agent configuration panel.

    Excludes:
    - Internal verification tools (collabland, alchemy)
    - Tools shown as MCP servers (polymarket)
    - Deprecated tools (twitter - use mcp:anysite)
    - Internal tools not meant for user selection (task)
    """
    # Categories that should appear in config panel
    usable_categories = {ToolCategory.CORE, ToolCategory.BROWSER, ToolCategory.SEARCH, ToolCategory.COMMUNICATION, ToolCategory.INTEGRATION}

    # Specific exclusions
    # NOTE: polymarket/hyperliquid are live in-process tools (see TOOL_DESCRIPTORS above),
    # NOT MCP — the old "polymarket is MCP-only" note was stale. They are selectable but
    # high-risk and stay out of the DEFAULT tool lists (opt-in only).
    # twitter is loadable (G1 full write surface, gated by TWITTER_ENABLED) — it is
    # selectable in the panel but stays out of the DEFAULT tool lists (opt-in only).
    excluded_tools = {'collabland', 'alchemy', 'task'}

    return {
        name: desc
        for name, desc in TOOL_DESCRIPTORS.items()
        if desc.category in usable_categories and name not in excluded_tools
    }


def get_tool_display_name(tool_name: str) -> str:
    """Get display name for a tool, handling aliases.

    Maps internal names to user-facing names:
    - browser_manager -> browser
    """
    aliases = {
        'browser_manager': 'browser'
    }
    return aliases.get(tool_name, tool_name)


# Backward compatible exports
TOOL_INIT_ORDER = get_tool_init_order()
OPTIONAL_TOOLS = get_optional_tools()
AVAILABLE_TOOLS = get_available_tools()


# Build TOOL_COMPONENTS and TOOL_DEPENDENCIES dynamically
# (These will be populated when tools/__init__.py imports this and registers classes)
TOOL_COMPONENTS: List[tuple] = []
TOOL_DEPENDENCIES: Dict[str, Dict[str, List[str]]] = {
    name: get_tool_dependencies(name)
    for name in TOOL_DESCRIPTORS
}
TOOL_METADATA: Dict[str, Dict[str, Any]] = {
    name: get_tool_metadata(name)
    for name in TOOL_DESCRIPTORS
}


def register_tool_class(tool_name: str, tool_class: Type["BaseTool"]) -> None:
    """Register a tool class with its descriptor.

    Called from tools/__init__.py to associate classes with descriptors.
    """
    if tool_name in TOOL_DESCRIPTORS:
        TOOL_DESCRIPTORS[tool_name].tool_class = tool_class
        # Update TOOL_COMPONENTS for backward compatibility
        if not any(name == tool_name for name, _ in TOOL_COMPONENTS):
            TOOL_COMPONENTS.append((tool_name, tool_class))


def get_tool_class(tool_name: str) -> Optional[Type["BaseTool"]]:
    """Get the tool class for a given tool name."""
    if tool_name in TOOL_DESCRIPTORS:
        return TOOL_DESCRIPTORS[tool_name].tool_class
    return None


def register_optional_tool(
    name: str,
    tool_cls: Type["BaseTool"],
    descriptor: "ToolDescriptor",
    enabled_fn,
    *,
    force: bool = False,
) -> bool:
    """Register an opt-in tool (code_exec/cronjob/goal) behind a feature gate.

    Inserts *descriptor* into ``TOOL_DESCRIPTORS`` idempotently (existing entry is
    never overwritten), then calls ``register_tool_class``.  Returns ``True`` iff
    the tool was registered.

    Args:
        name: Tool key (e.g. ``"cronjob"``).
        tool_cls: The ``BaseTool`` subclass to register.
        descriptor: ``ToolDescriptor`` instance for this tool.
        enabled_fn: Zero-argument callable; if it returns falsy *and* ``force`` is
            ``False``, registration is skipped.
        force: If ``True``, bypass ``enabled_fn`` and register unconditionally.
    """
    if not (force or enabled_fn()):
        return False
    if name not in TOOL_DESCRIPTORS:
        TOOL_DESCRIPTORS[name] = descriptor
    register_tool_class(name, tool_cls)
    return True
