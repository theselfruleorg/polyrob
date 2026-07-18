"""Location: core/__init__.py"""

"""Core module containing base components and initialization logic."""

# Import standard logging before any local imports
import logging

# Import logging utilities first
from .logging import setup_logging, get_component_logger

# Initialize root logger with our custom formatting
setup_logging(log_level='INFO')
logger = get_component_logger(__name__)

# Lightweight imports (no heavy deps)
from .base_component import BaseComponent
from .config import BotConfig
from .container import DependencyContainer
from .permissions import Permissions
from utils.rate_limit_manager import RateLimitManager


def __getattr__(name):
    """Lazy-load heavy modules (bot, initialization) on first access."""
    if name == 'Bot':
        from .bot import Bot
        globals()['Bot'] = Bot
        return Bot
    if name in (
        'initialize_core', 'initialize_modules', 'initialize_tools',
        'initialize_managers', 'initialize_agents', 'cleanup_core', 'cleanup_tools',
    ):
        from .initialization import (
            initialize_core, initialize_modules, initialize_tools,
            initialize_managers, initialize_agents, cleanup_core, cleanup_tools,
        )
        globals().update({
            'initialize_core': initialize_core,
            'initialize_modules': initialize_modules,
            'initialize_tools': initialize_tools,
            'initialize_managers': initialize_managers,
            'initialize_agents': initialize_agents,
            'cleanup_core': cleanup_core,
            'cleanup_tools': cleanup_tools,
        })
        return globals()[name]
    if name in ('MODULE_METADATA', 'MODULE_INIT_ORDER'):
        from modules import MODULE_METADATA, MODULE_INIT_ORDER
        globals()['MODULE_METADATA'] = MODULE_METADATA
        globals()['MODULE_INIT_ORDER'] = MODULE_INIT_ORDER
        return globals()[name]
    raise AttributeError(f"module 'core' has no attribute {name!r}")

# Core bot functionality - explicit imports from exceptions
from .exceptions import (
    BotError,
    ComponentError,
    ContainerError,
    ComponentInitializationError,
    ComponentCleanupError,
    ConfigurationError,
    DependencyError,
    ServiceError,
    ToolError,
    HandlerError,
    AgentError,
    LLMError,
    LLMConfigError,
    LLMConnectionError,
    LLMResponseError,
    LLMRateLimitError,
    LLMAuthenticationError,
    LLMContextLengthError,
    LLMInvalidRequestError,
    LLMPermanentError,
    LLMProviderExhaustedError,
    DatabaseError,
    PermissionError,
    ValidationError,
    APIError,
    AuthenticationError,
    RateLimitError,
    ResourceNotFoundError,
    ConversationError,
    StorageError,
    PromptError,
    EmbeddingError,
    CacheError,
    BadRequestError,
    MemoryStorageError,
    KnowledgeBaseError,
    ManagerError,
    PermissionsError,
    MemoryError,
    ModuleError,
    ConfigError,
    ModelError,
    MCPError,
    MCPConnectionError,
    MCPProtocolError,
    MCPToolExecutionError,
    SessionError,
    MessageQueueFullError,
    SessionNotFoundError,
    SessionStateError,
)

# Core components in one place with clear dependencies
CORE_COMPONENTS = {
    'config': {
        'class': BotConfig,
        'description': 'Configuration',
        'required': True,
        'dependencies': {
            'required': [],
            'optional': []
        }
    },
    'rate_limit_manager': {
        'class': RateLimitManager,
        'description': 'Rate limiting',
        'required': True,
        'dependencies': {
            'required': ['config'],
            'optional': []
        }
    },
    'permissions': {
        'class': Permissions,
        'description': 'Permissions management',
        'required': True,
        'dependencies': {
            'required': ['config'],  # Only needs config
            'optional': []  # No optional dependencies
        }
    }
}

# Export core components
__all__ = [
    # Base components
    'BaseComponent',
    'Bot',
    'BotConfig',
    'DependencyContainer',
    
    # Core managers 
    'Permissions',
    'RateLimitManager',
    
    # Initialization
    'initialize_core',
    'initialize_modules',
    'initialize_tools',
    'initialize_managers',
    'initialize_agents',
    'cleanup_core',
    'cleanup_tools',
    
    # Logging
    'setup_logging',
    'get_component_logger',

    # Component metadata
    'CORE_COMPONENTS',
    'MODULE_METADATA',
    'MODULE_INIT_ORDER'
]

# Version info
from core.version import __version__  # noqa: F401  (project version SSOT)
__author__ = 'Your Name'
__license__ = 'MIT'
