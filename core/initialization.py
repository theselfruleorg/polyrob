"""Location: core/initialization.py"""

"""Core initialization logic for bot components."""

import logging
from typing import Dict, Any, Optional, List, Type
from pathlib import Path
from enum import Enum, auto
from dataclasses import dataclass

from core.config import BotConfig
from core.exceptions import (
    ConfigurationError, 
    ServiceError,
    ManagerError,
    AgentError,
    ComponentInitializationError
)
from core.logging import get_component_logger
from core.permissions import Permissions
from core.container import DependencyContainer, ServiceScope, ServiceRegistration

# Core utilities
from modules import MODULE_INIT_ORDER
from utils.rate_limit_manager import RateLimitManager
from utils.message_utils import send_long_message
from utils.markdown_utils import format_message_with_markdown
from utils.metrics import Metrics  # Add import for Metrics

# Database and Memory
from modules.database import DatabaseManager
from modules.memory import MemoryManager
from modules.memory.cache_manager import CacheManager

# LLM Clients
from modules.llm import (
    LLMClient,
    AnthropicClient,
    OpenAIClient,
    DeepSeekClient,
    GeminiClient,
    AVAILABLE_MODELS,
    create_llm_client,
    LLMManager  # Add import for LLMManager
)

# External Tools - import from tools package to use centralized descriptors
# Individual tool classes are registered in tools/__init__.py via register_tool_class()
# TOOL_COMPONENTS is built from TOOL_DESCRIPTORS for backward compatibility

# Conversation Managers
# Legacy conversation managers retired with ChatAgent (HANDOFF-C, 2026-06-19).

# Import TASK_PACKAGE_AVAILABLE dynamically to avoid circular imports
def _get_task_package_available():
    try:
        from agents import TASK_PACKAGE_AVAILABLE
        return TASK_PACKAGE_AVAILABLE
    except (ImportError, AttributeError):
        return False

TASK_PACKAGE_AVAILABLE = _get_task_package_available()

# Import agent metadata and shared components function dynamically
def _get_agent_metadata():
    try:
        from agents import AGENT_METADATA
        return AGENT_METADATA
    except (ImportError, AttributeError):
        return {}

def _get_initialize_shared_components():
    try:
        from agents import initialize_shared_components
        return initialize_shared_components
    except (ImportError, AttributeError):
        return None

# Use dynamic imports instead of static imports
AGENT_METADATA = _get_agent_metadata()
initialize_shared_components = _get_initialize_shared_components()

# Define agent components dynamically to avoid circular imports
def _get_agent_components():
    try:
        from agents import TaskAgent
        return [
            ('task_agent', TaskAgent, 'Task agent', True, {})
        ]
    except (ImportError, AttributeError):
        return []

# Use dynamic function to get agent components
AGENT_COMPONENTS = _get_agent_components()

# NOTE: SentenceTransformer is imported lazily at its only use site (in initialize_modules,
# gated on get_embedding_config()) — never at module load. A top-level import here would
# drag torch+transformers (~2.5s) into every process that imports core.initialization
# (every uvicorn worker boot). See docs/plans/2026-06-26-runtime-architecture-finalization-FUSION.md.

# New imports
from agents.personality.character_manager import CharacterManager
from agents.personality.character import Character

from tools.base_tool import ToolStatus

__all__ = [
    'initialize_core',
    'cleanup_core',
    'initialize_tools',
    'cleanup_tools'
]

logger = get_component_logger('initialization')

class InitPhase(Enum):
    """Initialization phases in order."""
    CORE = auto()        # Config, logging, container
    INFRA = auto()       # Database, memory, cache
    LLM = auto()         # Language models
    TOOLS = auto()       # External tools
    MANAGERS = auto()    # State managers
    AGENTS = auto()      # AI agents

@dataclass
class ComponentInfo:
    """Component initialization metadata."""
    name: str
    component_class: Type
    description: str
    phase: InitPhase
    required: List[str]
    optional: List[str]
    is_core: bool = False
    is_optional: bool = False

# Define core components first since other services depend on them
CORE_COMPONENTS = {
    'config': ComponentInfo(
        name='config',
        component_class=BotConfig,
        description='Configuration',
        phase=InitPhase.CORE,
        required=[],
        optional=[],
        is_core=True
    ),
    'rate_limit_manager': ComponentInfo(
        name='rate_limit_manager', 
        component_class=RateLimitManager,
        description='Rate limiting',
        phase=InitPhase.CORE,
        required=['config'],
        optional=[],
        is_core=True
    ),
    'database_manager': ComponentInfo(  # Added as core component
        name='database_manager',
        component_class=DatabaseManager,
        description='Database management',
        phase=InitPhase.CORE,
        required=['config'],
        optional=[],
        is_core=True
    ),
    'cache_manager': ComponentInfo(  # Added as core component
        name='cache_manager',
        component_class=CacheManager,
        description='Cache management',
        phase=InitPhase.CORE,
        required=['database_manager'],
        optional=[],
        is_core=True
    )
}

# Define module initialization order with consistent naming
MODULE_COMPONENTS = {
    'database_manager': DatabaseManager,
    'memory_manager': MemoryManager,
    'cache_manager': CacheManager,
    'llm_client': LLMClient
}

# Define module dependencies
MODULE_DEPENDENCIES = {
    'memory_manager': {
        'required': ['database_manager'],
        'optional': []
    },
    'cache_manager': {
        'required': ['memory_manager'],
        'optional': []
    },
    'llm_client': {
        'required': [],
        'optional': ['cache_manager']
    }
}

# =============================================================================
# TOOL CONSTANTS - Import from single source of truth
# =============================================================================
# Previously duplicated here - now imported from tools/descriptors.py
from tools.descriptors import (
    TOOL_DEPENDENCIES,
    TOOL_INIT_ORDER,
    TOOL_METADATA,
    OPTIONAL_TOOLS as TOOL_OPTIONAL_TOOLS,
    get_tool_class,
)

# Re-export for backward compatibility (but prefer importing from tools directly)
OPTIONAL_TOOLS = TOOL_OPTIONAL_TOOLS

# TOOL_COMPONENTS is imported lazily to avoid circular imports
# It will be set when _get_tool_components() is called
TOOL_COMPONENTS = None

def _get_tool_components():
    """Get TOOL_COMPONENTS lazily to avoid circular imports."""
    global TOOL_COMPONENTS
    if TOOL_COMPONENTS is None:
        from tools import TOOL_COMPONENTS as _TOOL_COMPONENTS
        TOOL_COMPONENTS = _TOOL_COMPONENTS
    return TOOL_COMPONENTS

# Define which managers are optional
OPTIONAL_MANAGERS = set()  # No optional managers currently

# Manager components retired with the legacy ChatAgent (HANDOFF-C). The phase is
# kept (empty) so the init/cleanup loops stay structurally intact and no-op.
MANAGER_COMPONENTS = []

# Define manager metadata
MANAGER_METADATA = {}

async def initialize_phase(
    container: DependencyContainer,
    phase: InitPhase,
    components: Dict[str, ComponentInfo]
) -> None:
    """Initialize components for a specific phase."""
    logger.info(f"Starting {phase.name} initialization phase")
    
    # Get components for this phase
    phase_components = {
        name: info for name, info in components.items() 
        if info.phase == phase
    }
    
    # Initialize in dependency order
    initialized = set()
    while phase_components:
        started_count = len(phase_components)
        
        # Try to initialize components whose dependencies are met
        for name, info in list(phase_components.items()):
            # Check if required dependencies are initialized
            missing_deps = [
                dep for dep in info.required 
                if dep not in initialized
            ]
            
            if not missing_deps:
                try:
                    # Initialize component
                    component = info.component_class(
                        name=info.name,
                        config=container.config,
                        container=container
                    )
                    await component.initialize()
                    
                    # Register in container
                    if info.is_core:
                        container.register_core_service(name, component)
                    else:
                        container.register_service(
                            name, 
                            component,
                            is_optional=info.is_optional
                        )
                        
                    initialized.add(name)
                    del phase_components[name]
                    logger.info(f"✓ {info.description} initialized")
                    
                except Exception as e:
                    if info.is_optional:
                        logger.warning(
                            f"Optional component {info.name} failed to initialize: {e}"
                        )
                        del phase_components[name]
                    else:
                        raise ComponentInitializationError(
                            f"Failed to initialize {info.name}: {e}"
                        )
        
        # Check for circular dependencies
        if len(phase_components) == started_count:
            remaining = ", ".join(phase_components.keys())
            raise ComponentInitializationError(
                f"Circular dependencies detected in {phase.name} phase. "
                f"Remaining components: {remaining}"
            )
    
    logger.info(f"Completed {phase.name} initialization phase")

async def initialize_core(container: DependencyContainer) -> None:
    """Initialize core components."""
    try:
        logger.info("➤ Core initialization started")
        
        # Initialize rate limit manager
        rate_limit_manager = RateLimitManager(
            name='rate_limit_manager',
            config=container.config
        )
        await rate_limit_manager.initialize()
        container.register_core_service(
            'rate_limit_manager',
            rate_limit_manager,
            scope=ServiceScope.SINGLETON
        )
        logger.info("  ✓ Rate limit manager initialized")
        
        # Initialize metrics
        metrics = Metrics(
            name='metrics',
            config=container.config
        )
        await metrics.initialize()
        container.register_core_service(
            'metrics',
            metrics,
            scope=ServiceScope.SINGLETON
        )
        logger.info("  ✓ Metrics initialized")
        
        # Initialize cache
        cache_manager = CacheManager(
            name='cache_manager',
            config=container.config
        )
        await cache_manager.initialize()
        container.register_core_service(
            'cache_manager',
            cache_manager,
            scope=ServiceScope.SINGLETON
        )
        logger.info("  ✓ Cache manager initialized")
        
        # Ensure database manager is initialized before memory manager
        if not container.has_service('database_manager'):
            try:
                database_manager = DatabaseManager(
                    name='database_manager',
                    config=container.config,
                    container=container
                )
                await database_manager.initialize()
                container.register_core_service(
                    'database_manager',
                    database_manager,
                    scope=ServiceScope.SINGLETON
                )
                logger.info("  ✓ Database manager initialized")
            except Exception as e:
                logger.error(f"❌ Failed to initialize database manager: {e}")
                raise

        # Initialize memory
        memory_manager = MemoryManager(
            name='memory_manager',
            config=container.config,
            container=container
        )
        await memory_manager.initialize()
        container.register_core_service(
            'memory_manager',
            memory_manager,
            scope=ServiceScope.SINGLETON
        )
        logger.info("  ✓ Memory manager initialized")
        
        # Initialize permissions after database and memory are ready
        permissions = Permissions(
            config=container.config
        )
        # Set container reference for permissions
        permissions.container = container
        await permissions.initialize()
        container.register_core_service(
            'permissions',
            permissions,
            scope=ServiceScope.SINGLETON
        )
        logger.info("  ✓ Permissions manager initialized")
        
        # Link permissions with user profile manager
        try:
            user_profile_manager = memory_manager.user_profile_manager
            if user_profile_manager:
                permissions.set_user_profile_manager(user_profile_manager)
                logger.info("  ✓ Permissions linked with user profile manager")
        except Exception as e:
            logger.warning(f"  ⚠ Failed to link permissions with user profile manager: {e}")
        
        logger.info("✓ Core initialization complete")
        return
    except Exception as e:
        logger.error(f"❌ Core initialization failed: {e}")
        raise ComponentInitializationError(f"Failed to initialize core: {e}")

async def initialize_modules(container: DependencyContainer) -> None:
    """Initialize all modules in correct order."""
    try:
        logger.info("➤ Module initialization started")
        
        # Initialize database first
        if not container.has_service('database_manager'):
            database = MODULE_COMPONENTS['database_manager'](
                name='database_manager',
                config=container.config,
                container=container
            )
            await database.initialize()
            container.register_service('database_manager', database)
            logger.info("  ✓ Database module initialized")
        else:
            logger.info("  ⏩ Database manager already initialized in core phase")

        # Initialize embedding model only when actually needed (KB / local_vector / local
        # mode). get_embedding_config() always returns a default dict, so gating on it built
        # the torch-backed embedder on EVERY startup even for MEMORY_BACKEND=sqlite (FTS5,
        # no embeddings). embedder_needed() is the shared SSOT with the CLI path. (P1-EMB)
        from agents.task.constants import embedder_needed
        if embedder_needed():
            try:
                # LAZY embedder: defer the torch/model load (and HF Hub network validation)
                # off the lifespan critical path; it builds on first actual vector use from
                # the local cache. (P1-EMB / lazy-embedder)
                from core.embedding import LazyEmbedder
                model = LazyEmbedder(container.config.get_embedding_config().get('model_name'))
                container.register_service('embedding_model', model)
                logger.info("  ✓ Embedding model registered (lazy)")
            except Exception as e:
                logger.warning(f"  ⚠ Failed to register embedding model: {e}")

        # Cloud vector storage (Pinecone) and the RAGKnowledgeManager have been retired.
        # Cross-session semantic recall is now served locally by the sqlite-vec memory
        # backend (LocalVectorMemoryProvider, MEMORY_BACKEND=local_vector), which reuses
        # the local embedding model registered above. The H-MEM SemanticRetriever also
        # draws its embedding model directly from the container.

        # Initialize LLM module
        try:
            llm_module = LLMManager(
                name='llm',
                config=container.config,
                container=container
            )
            await llm_module.initialize()
            container.register_service('llm', llm_module)
            logger.info("  ✓ LLM module initialized")
        except Exception as e:
            logger.warning(f"  ⚠ Failed to initialize LLM module: {e}")

        logger.info("✓ Module initialization complete")
    except Exception as e:
        logger.error(f"❌ Module initialization failed: {e}")
        raise ComponentInitializationError(f"Failed to initialize modules: {e}")

async def initialize_tools(container: DependencyContainer) -> None:
    """Initialize tools with proper error handling."""
    logger.info("➤ Tool initialization started")
    
    results = {'success': [], 'failed': []}

    # Follow TOOL_INIT_ORDER instead of TOOL_COMPONENTS to respect dependencies
    for tool_name in TOOL_INIT_ORDER:
        tool_class = None
        # Find the tool class in TOOL_COMPONENTS (lazy loaded to avoid circular imports)
        tool_components = _get_tool_components()
        for component_name, component_class in tool_components:
            if component_name == tool_name:
                tool_class = component_class
                break
                
        if not tool_class:
            logger.warning(f"  ⚠ No tool class found for {tool_name}, skipping")
            continue
            
        metadata = TOOL_METADATA.get(tool_name, {})
        requires = metadata.get('requires', [])
        
        # Check dependencies
        missing_deps = []
        for dep in requires:
            if not container.has_service(dep):
                missing_deps.append(dep)
                
        if missing_deps:
            logger.warning(f"  ⚠ Skipping tool {tool_name} due to missing dependencies: {', '.join(missing_deps)}")
            results['failed'].append(tool_name)
            continue
            
        try:
            # Skip tool config check - method doesn't exist on BotConfig
            # Tools will be initialized unless they're in OPTIONAL_TOOLS

            # Special handling for BrowserManager which follows BaseComponent pattern
            if tool_name == 'browser_manager':
                tool = tool_class(config=container.config)
            else:
                # Initialize tool with standard parameters
                tool = tool_class(
                    name=tool_name,
                    config=container.config,
                    container=container
                )

            # Initialize and register
            await tool.initialize()
            container.register_service(tool_name, tool)

            # Special case: Register browser alias for BrowserManager
            if tool_name == 'browser_manager':
                # Ensure browser_manager is initialized
                if not tool.is_initialized:
                    await tool.initialize()

                # Try multiple ways to get browser
                browser = None
                if hasattr(tool, 'browser') and tool.browser:
                    browser = tool.browser
                elif hasattr(tool, 'get_browser'):
                    browser = await tool.get_browser()

                if browser:
                    # Register only 'browser' alias - avoid multiple aliases for same resource
                    # browser_manager = the manager service, browser = the actual browser instance
                    container.register_service('browser', browser)
                    logger.info(f"  ✓ Registered 'browser' alias for browser instance")
                else:
                    logger.warning("  ⚠ BrowserManager initialized but no browser instance available")

            logger.info(f"  ✓ Tool {tool_name} initialized")
            results['success'].append(tool_name)
            
        except Exception as e:
            logger.error(f"  ❌ Failed to initialize tool {tool_name}: {e}")
            results['failed'].append(tool_name)
            if metadata.get('required', False):
                raise ComponentInitializationError(f"Required tool {tool_name} failed to initialize: {e}")
                
    # Log summary
    logger.info(f"✓ Tool initialization complete: {len(results['success'])} succeeded, {len(results['failed'])} failed")
    if results['failed']:
        logger.warning(f"  ⚠ Failed tools: {', '.join(results['failed'])}")

async def initialize_auth_services(container: DependencyContainer) -> None:
    """Initialize authentication and payment services."""
    logger.info("➤ Auth services initialization started")

    # Check if auth is enabled
    if not container.config.enable_auth:
        logger.info("  ⏩ Authentication system disabled (set ENABLE_AUTH=true to enable)")
        return

    try:
        # Get required services
        db_manager = container.get_service('database_manager')
        alchemy_tool = container.get_service('alchemy')

        if not db_manager:
            raise ComponentInitializationError("Database manager required for auth")

        if not alchemy_tool:
            logger.warning("  ⚠ Alchemy tool not available - token gating will be limited")

        # Auth tables are already created during database initialization (STEP 1)
        # No need to recreate them here - just verify they exist
        auth_tables = db_manager.tables.get('auth_tables')
        if not auth_tables:
            logger.warning("  ⚠ Auth tables not found in database manager")
        else:
            logger.info("  ✓ Auth tables verified (created in database init)")

        # SIWE Authenticator (FREE - replaces Privy!)
        from modules.auth.siwe_auth import SIWEAuthenticator
        siwe_auth = SIWEAuthenticator(db=db_manager)
        container.register_service('siwe_authenticator', siwe_auth)
        logger.info("  ✓ SIWE authenticator initialized (FREE!)")

        # Identity Mapper (updated for SIWE)
        from modules.auth.identity_mapper import IdentityMapper
        identity_mapper = IdentityMapper(
            db=db_manager,
            user_profiles=db_manager.user_profiles,
            alchemy_tool=alchemy_tool
        )
        container.register_service('identity_mapper', identity_mapper)
        logger.info("  ✓ Identity mapper initialized")

        # Tier Manager
        from modules.auth.tier_manager import TierManager
        tier_manager = TierManager(
            db=db_manager,
            alchemy_tool=alchemy_tool
        )
        container.register_service('tier_manager', tier_manager)
        logger.info("  ✓ Tier manager initialized")

        # API Key Manager
        from modules.auth.api_key_manager import APIKeyManager
        api_key_manager = APIKeyManager(
            db=db_manager,
            tier_manager=tier_manager
        )
        container.register_service('api_key_manager', api_key_manager)
        logger.info("  ✓ API key manager initialized")

        # Credit Balance Manager
        if container.config.enable_credit_system:
            from modules.credits.balance_manager import CreditBalanceManager
            balance_manager = CreditBalanceManager(
                db=db_manager,
                tier_manager=tier_manager
            )
            container.register_service('balance_manager', balance_manager)
            logger.info("  ✓ Credit balance manager initialized")

        # Wallet Generator (only if master_seed configured)
        from core.payment_config import resolve_master_seed
        _resolved_seed = resolve_master_seed()
        if _resolved_seed:
            from modules.payments.wallet_generator import DepositWalletGenerator
            wallet_generator = DepositWalletGenerator(master_seed=_resolved_seed)
            container.register_service('wallet_generator', wallet_generator)
            logger.info("  ✓ Wallet generator initialized")
        else:
            wallet_generator = None
            logger.warning("  ⚠ Master seed not configured - deposit wallets disabled")

        # Deposit Monitor (if RPC configured and enabled)
        if container.config.deposit_monitor_enabled and (container.config.sepolia_rpc_url or container.config.ethereum_rpc_url):
            if balance_manager:
                try:
                    from modules.payments.deposit_monitor import DepositMonitor

                    deposit_monitor = DepositMonitor(
                        db_manager=db_manager,
                        balance_manager=balance_manager,
                        config=container.config
                    )
                    container.register_service('deposit_monitor', deposit_monitor)

                    # Start monitoring in background
                    await deposit_monitor.start()
                    logger.info(f"  ✓ Deposit monitor started (interval: {container.config.deposit_check_interval}s)")
                except Exception as e:
                    logger.error(f"  ❌ Failed to start deposit monitor: {e}")
            else:
                logger.warning("  ⚠ Deposit monitor requires balance_manager - skipping")
        elif container.config.deposit_monitor_enabled:
            logger.warning("  ⚠ Deposit monitor enabled but no RPC URLs configured - skipping")
        else:
            logger.info("  ⏩ Deposit monitor disabled (set DEPOSIT_MONITOR_ENABLED=true to enable)")

        # Treasury Sweeper (if treasury address configured and wallet generator available)
        if container.config.treasury_address and wallet_generator:
            try:
                from modules.payments.treasury_sweeper import TreasurySweeper

                treasury_sweeper = TreasurySweeper(
                    db_manager=db_manager,
                    wallet_generator=wallet_generator,
                    config=container.config
                )
                container.register_service('treasury_sweeper', treasury_sweeper)

                # Start sweeping in background
                await treasury_sweeper.start()
                logger.info(f"  ✓ Treasury sweeper started (interval: {container.config.sweep_interval}s)")
            except Exception as e:
                logger.error(f"  ❌ Failed to start treasury sweeper: {e}")
        elif container.config.treasury_address:
            logger.warning("  ⚠ Treasury sweeper requires wallet_generator - skipping")
        else:
            logger.info("  ⏩ Treasury sweeper disabled (set TREASURY_ADDRESS to enable)")

        # x402 Payment System
        # NOTE: x402 is now handled via fastapi-x402 middleware in api/app.py
        # The middleware uses Coinbase facilitator for proper on-chain verification
        if container.config.x402_enabled:
            logger.info(f"  ✓ x402 enabled (handled by fastapi-x402 middleware)")
        else:
            logger.info("  ⏩ x402 payment system disabled")

        # User MCP Service (for per-user MCP server configurations)
        try:
            from modules.database.user_mcp_servers import UserMCPServersHandler
            from tools.mcp.user_mcp_service import UserMCPService, init_user_mcp_service

            # Create database handler - use connection attribute from DatabaseManager
            user_mcp_handler = UserMCPServersHandler(db_manager.connection)

            # Ensure tables exist (creates if missing)
            await user_mcp_handler.ensure_tables()

            container.register_service('user_mcp_handler', user_mcp_handler)

            # Create service
            user_mcp_service = init_user_mcp_service(user_mcp_handler)
            container.register_service('user_mcp_service', user_mcp_service)
            logger.info("  ✓ User MCP service initialized")

        except Exception as e:
            logger.warning(f"  ⚠ User MCP service initialization failed (optional): {e}")

        # Note: Polymarket tool is initialized in initialize_tools() phase
        # with init_priority=55. It self-registers polymarket_db in container.

        logger.info("✓ Auth services initialized successfully")

    except Exception as e:
        logger.error(f"❌ Auth service initialization failed: {e}")
        raise ComponentInitializationError(f"Failed to initialize auth services: {e}")


async def initialize_managers(container: DependencyContainer) -> None:
    """Initialize managers with proper error handling."""
    logger.info("➤ Manager initialization started")
    
    results = {'success': [], 'failed': []}

    # Initialize managers in the specified order
    for manager_name, manager_class in MANAGER_COMPONENTS:  # Fixed: Unpack tuple properly
        metadata = MANAGER_METADATA.get(manager_name, {})
        requires = metadata.get('requires', [])
        
        # Check dependencies
        missing_deps = []
        for dep in requires:
            if not container.has_service(dep):
                missing_deps.append(dep)
                
        if missing_deps:
            logger.warning(f"  ⚠ Skipping manager {manager_name} due to missing dependencies: {', '.join(missing_deps)}")
            results['failed'].append(manager_name)
            continue
            
        try:
            # Skip manager config check - method doesn't exist on BotConfig
            
            # Initialize manager
            manager = manager_class(
                name=manager_name,
                config=container.config,
                container=container
            )
            
            # Initialize and register
            await manager.initialize()
            container.register_manager(manager_name, manager)
            
            logger.info(f"  ✓ Manager {manager_name} initialized")
            results['success'].append(manager_name)
            
        except Exception as e:
            logger.error(f"  ❌ Failed to initialize manager {manager_name}: {e}")
            results['failed'].append(manager_name)
            if metadata.get('required', False):
                raise ComponentInitializationError(f"Required manager {manager_name} failed to initialize: {e}")
                
    # Log summary
    logger.info(f"✓ Manager initialization complete: {len(results['success'])} succeeded, {len(results['failed'])} failed")
    if results['failed']:
        logger.warning(f"  ⚠ Failed managers: {', '.join(results['failed'])}")

async def initialize_agents(container: DependencyContainer) -> None:
    """Initialize agents with proper error handling."""
    logger.info("➤ Agent initialization started")
    
    results = {'success': [], 'failed': []}

    # Initialize AutoV2 logging integration first
    try:
        from agents.task.logging_config import ensure_core_logging_integration, configure_library_loggers
        if ensure_core_logging_integration():
            logger.info("  ✓ AutoV2 logging integrated with core logging system")
            # Configure library loggers to reduce noise
            configure_library_loggers()
        else:
            logger.warning("  ⚠ AutoV2 logging integration not available")
    except ImportError:
        logger.debug("  ⏩ AutoV2 logging module not available")
    except Exception as e:
        logger.warning(f"  ⚠ Failed to initialize AutoV2 logging integration: {e}")

    # Initialize shared components (character_manager, system_prompt_manager) first
    try:
        from agents import initialize_shared_components
        await initialize_shared_components(container)
        logger.info("  ✓ Shared components (character_manager, system_prompt_manager) initialized")
    except Exception as e:
        logger.error(f"  ❌ Failed to initialize shared components: {e}")
        # Don't raise - these are important but we can continue without them

    # Initialize task infrastructure before agents if task_agent is present
    if any(name == 'task_agent' for name, _, _, _, _ in AGENT_COMPONENTS):
        try:
            # Register SessionManager
            from agents.task.agent.session import get_session_manager
            session_manager = get_session_manager()
            container.register_service('session_manager', session_manager)
            logger.info("  ✓ SessionManager singleton registered in container")

            # Register Controller factory
            from tools.controller.service import Controller
            container.register_service('controller_class', Controller)
            logger.info("  ✓ Controller factory registered in container")
        except ImportError as e:
            logger.warning(f"  ⚠ Failed to import task infrastructure: {e}")
        except Exception as e:
            logger.warning(f"  ⚠ Failed to register task infrastructure: {e}")

    # Initialize agents in the specified order
    for agent_component in AGENT_COMPONENTS:
        # Unpack the agent tuple (name, class, description, optional, metadata)
        agent_name, agent_class = agent_component[0], agent_component[1]
        metadata = AGENT_METADATA.get(agent_name, {})
        # Fix: Use correct key 'required_services' instead of 'requires'
        requires = metadata.get('required_services', [])
        
        # Check dependencies
        missing_deps = []
        for dep in requires:
            if not container.has_service(dep):
                missing_deps.append(dep)
                
        if missing_deps:
            logger.warning(f"  ⚠ Skipping agent {agent_name} due to missing dependencies: {', '.join(missing_deps)}")
            results['failed'].append(agent_name)
            continue
            
        try:
            # Skip agent config check - method doesn't exist on BotConfig
            
            # Initialize agent
            agent = agent_class(
                name=agent_name,
                config=container.config,
                container=container
            )
            
            # Initialize and register
            await agent.initialize()
            container.register_agent(agent_name, agent)
            
            logger.info(f"  ✓ Agent {agent_name} initialized")
            results['success'].append(agent_name)
            
        except Exception as e:
            logger.error(f"  ❌ Failed to initialize agent {agent_name}: {e}")
            results['failed'].append(agent_name)
            if metadata.get('required', False):
                raise ComponentInitializationError(f"Required agent {agent_name} failed to initialize: {e}")
                
    # Log summary
    logger.info(f"✓ Agent initialization complete: {len(results['success'])} succeeded, {len(results['failed'])} failed")
    if results['failed']:
        logger.warning(f"  ⚠ Failed agents: {', '.join(results['failed'])}")


async def cleanup_core(container: DependencyContainer) -> None:
    """Clean up all components in reverse initialization order."""
    try:
        logger.info("Starting cleanup...")
        
        # Clean up in reverse order
        for component_type in ['agents', 'managers', 'tools', 'modules', 'core']:
            await _cleanup_component_type(container, component_type)
            
        logger.info("Cleanup complete")
        
    except Exception as e:
        logger.error(f"Error during cleanup: {e}")
        raise

async def _cleanup_component_type(container: DependencyContainer, component_type: str) -> None:
    """Clean up components of a specific type."""
    try:
        # Get components to clean up
        components = []

        if component_type == 'agents':
            components = [
                agent for agent in container.agents.values()
                if agent is not None
            ]
            
        elif component_type == 'managers':
            components = [
                manager for manager in container.managers.values()
                if manager is not None
            ]
            
        elif component_type == 'tools':
            # Cleanup polymarket tool (registered as 'polymarket' by initialize_tools)
            polymarket_tool = container.get_service('polymarket')
            if polymarket_tool and hasattr(polymarket_tool, 'cleanup'):
                try:
                    await polymarket_tool.cleanup()
                    logger.info("✓ polymarket cleaned up")
                except Exception as e:
                    logger.error(f"Error cleaning up polymarket: {e}")

            components = [
                tool for tool in container.services.values()
                if tool is not None and tool is not polymarket_tool
            ]
            
        elif component_type == 'modules':
            modules = [
                container.get_service('database_manager'),
                container.get_service('memory_manager'),
                container.get_service('cache_manager'),
                container.get_service('llm_client')
            ]
            components = [m for m in modules if m is not None]
            
        elif component_type == 'core':
            core = [
                container.get_service('permissions'),
                container.get_service('rate_limit_manager')
            ]
            components = [c for c in core if c is not None]

        # Clean up components
        for component in components:
            try:
                if hasattr(component, 'cleanup'):
                    await component.cleanup()
                    logger.info(f"✓ {getattr(component, 'name', component)} cleaned up")
            except Exception as e:
                logger.error(f"Error cleaning up {getattr(component, 'name', component)}: {e}")

    except Exception as e:
        logger.error(f"Error during {component_type} cleanup: {e}")

async def cleanup_agents(container: DependencyContainer) -> None:
    """Clean up agents."""
    await _cleanup_component_type(container, 'agents')

async def cleanup_managers(container: DependencyContainer) -> None:
    """Clean up managers."""
    await _cleanup_component_type(container, 'managers')

async def cleanup_modules(container: DependencyContainer) -> None:
    """Clean up modules."""
    await _cleanup_component_type(container, 'modules')

async def cleanup_tools(container: DependencyContainer) -> None:
    """Clean up tools in reverse initialization order."""
    logger.info("Cleaning up tools...")
    try:
        # Get all registered tools (use canonical names, not aliases)
        tools_to_cleanup = [
            'filesystem',
            'twitter_tool',
            'perplexity_tool',
            'email_tool',
            'browser_manager',  # Use canonical name, not browser_tool alias
            'rate_limit_manager',
            'cache_manager',
            'database_manager'
        ]
        
        # Clean up in reverse order
        for tool_name in reversed(tools_to_cleanup):
            tool = container.get_service(tool_name)
            if tool:
                try:
                    await tool.cleanup()
                    logger.info(f"✓ {tool_name} cleaned up")
                except Exception as e:
                    logger.error(f"Error cleaning up {tool_name}: {e}")
        
        logger.info("Tools cleanup complete")
        
    except Exception as e:
        logger.error(f"Error during tools cleanup: {e}")
        raise ServiceError(f"Tool cleanup failed: {e}") 