"""Location: core/__init__.py"""

"""Core module containing base components and initialization logic."""

# Import standard logging before any local imports
import logging as _std_logging  # `logging` below is rebound to core.logging

# Import logging utilities first
from .logging import setup_logging, get_component_logger

# D6 (2026-10-10): importing core never configures the ROOT logger. A library
# import must leave an embedder's logging alone; each process entry point
# (cli.polyrob.main, api.server_boot.run_server, webview.server, the signer,
# polyrob.app.bootstrap) calls setup_logging() itself.
logger = _std_logging.getLogger(__name__)

# Lightweight imports (no heavy deps)
from .base_component import BaseComponent
from .config import BotConfig
from .container import DependencyContainer
# W7: the composition root (Bot, initialize_*/cleanup_*) lives in polyrob.app, and
# this package no longer re-exports RateLimitManager (utils) or the module
# metadata (modules) — importing core pulls no upper tier.


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

# Export core components
__all__ = [
    # Base components
    'BaseComponent',
    'BotConfig',
    'DependencyContainer',

    # Logging
    'setup_logging',
    'get_component_logger',
]

# Version info
from core.version import __version__  # noqa: F401  (project version SSOT)
__author__ = 'Your Name'
__license__ = 'MIT'
