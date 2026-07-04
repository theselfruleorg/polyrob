# Core Package - POLYROB Application Framework

_Last reviewed: 2026-06-30. For the authoritative architecture see ../AGENTS.md; for env flags see ../docs/CONFIGURATION.md._

## Overview

The `core` package provides the foundational framework for the POLYROB platform. It implements a dependency injection system, comprehensive configuration management, robust error handling, and a component-based architecture that ensures scalable, maintainable, and testable code.

## Architecture Philosophy

- **Dependency Injection**: Clean separation of concerns through a centralized container
- **Component-Based**: Modular components with clear lifecycle management
- **Configuration-Driven**: Centralized configuration with environment-specific settings
- **Async-First**: Built for asynchronous operations from the ground up
- **Logging-Integrated**: Comprehensive logging with component-level granularity
- **Error-Resilient**: Robust error handling and graceful degradation

## Package Structure

```
core/
├── __init__.py                  # Package exports and metadata
├── README.md                    # This documentation
├── base_component.py            # Abstract base class for all components
├── bot.py                       # Main bot orchestration and lifecycle
├── bootstrap.py                 # Application bootstrap
├── config.py                    # Configuration management system
├── runtime_config.py            # Runtime config helpers
├── constants.py                 # Core constants
├── container.py                 # Dependency injection container
├── exceptions.py                # Custom exception hierarchy
├── initialization.py            # Component initialization orchestration
├── logging.py                   # Logging configuration and utilities
├── permissions.py               # Role-based permission system
├── security_logging_filter.py   # Security-aware log filtering
├── identity.py                  # Agent identity
├── instance.py                  # Bot instance / agent-identity scaffolding (multi-instance keying)
├── pairing.py                   # Device/surface pairing
├── self_context_writer.py       # Writable self-context support
├── session_context.py           # Session context
├── interactive_gate.py          # Interactive idle-gate (REPL busy vs background tickers)
├── autonomy_runtime.py          # Shared autonomy runtime (cron/goal/curator tickers) for API + CLI
├── tickers.py                   # Ticker primitives
├── async_bridge.py              # Sync-from-async bridge (persistent background loop)
├── sqlite_util.py               # SQLite WAL + jittered-retry helpers
├── env.py                       # Environment helpers
├── path_safety.py               # Path confinement helpers
├── paths.py                     # User config-HOME seam (~/.polyrob; POLYROB_HOME override)
├── runtime_paths.py             # Project-scoped runtime data root (code/config/runtime isolation)
├── home_migration.py            # One-time ~/.rob → ~/.polyrob home migration (copy, fail-open)
├── assets.py                    # Webgate static-asset resolver (packaged web_dist/ vs repo webview/)
├── embedding.py                 # LazyEmbedder — deferred sentence-transformers proxy
├── seams.py                     # core⇄platform dependency-inversion contracts (LLMUsage, UsageRecorder, SessionAdmissionPolicy, PaymentVerifier)
├── secret_scan.py               # Workspace secret/VCS scan (SEC-1 launch-in-a-repo backstop)
├── secret_scrub.py              # Pattern-based secret redaction for persisted content
├── tool_catalog.py              # Product-facing tool catalog builder (from tools/descriptors.py)
├── surfaces/                    # Surface contracts (CLI/WebView/etc.)
└── wallet/                      # Native agent wallet (signer, policy gate, audit sink)
```

## Core Components

### 1. BaseComponent (`base_component.py`)

Abstract base class providing standardized component lifecycle management.

**Key Features**:
- Standardized initialization/cleanup patterns
- Dependency validation system
- Component registration tracking
- Async-safe lifecycle management
- Error handling and logging integration

**Usage Example**:
```python
class MyService(BaseComponent):
    async def _initialize(self) -> None:
        await self._setup_resources()
    
    async def _cleanup(self) -> None:
        await self._release_resources()
    
    @property
    def required_dependencies(self) -> List[str]:
        return ['config', 'database_manager']
```

**Lifecycle State**:

`BaseComponent` does not implement a multi-state machine. It tracks a single
`_initialized` boolean guarded by an `asyncio.Lock` (`_lock`):
- `initialize()` is idempotent — it returns early if `_initialized` is already
  `True`, otherwise it validates dependencies, runs the subclass `_initialize()`,
  and sets `_initialized = True` under the lock.
- `_initialized == False`: created but not yet initialized (or cleaned up).
- `_initialized == True`: dependencies validated and `_initialize()` completed.

There is also an `_enabled` flag for toggling a component on/off.

### 2. BotConfig (`config.py`)

Comprehensive configuration management using Pydantic for validation and type safety.

**Configuration Sources** (in order of precedence):
1. Environment variables
2. `.env.{environment}` files
3. `.env` fallback file
4. Default values

**Core Configuration Sections**:
```python
# Authentication
jwt_secret: str                  # JWT secret for API authentication
admin_wallet_addresses: List[str] # Administrator wallet addresses

# LLM Provider Configuration
openai_api_key: Optional[str]    # OpenAI API key
anthropic_api_key: Optional[str] # Anthropic API key
gemini_api_key: Optional[str]    # Google Gemini API key
deepseek_api_key: Optional[str]  # DeepSeek API key
# (Llama provider was removed; there is no llama_api_key)

# Model Selection & Configuration
# NOTE: BotConfig no longer carries a `model_name` field or a `model_configs`
# dict — model selection and per-model config (context windows, max tokens,
# pricing) live in modules.llm.model_registry. BotConfig only exposes provider
# API keys + per-provider connection settings (get_*_config helpers).

# External Service Integration
twitter_api_key: Optional[str]
perplexity_api_key: Optional[str]
gmail_email: Optional[str]
```

### 3. DependencyContainer (`container.py`)

Centralized dependency injection container managing service lifecycle.

**Service Scopes**:
```python
class ServiceScope(Enum):
    SINGLETON = "singleton"    # One instance for entire app
    SCOPED = "scoped"         # One instance per scope
    TRANSIENT = "transient"   # New instance each time
```

**Core Service Categories**:
- **Core Services**: Essential services required for basic operation
- **Module Services**: Functional modules (database, LLM, memory)
- **Manager Services**: High-level orchestration managers
- **Agent Services**: AI agent implementations
- **Auth Services**: Authentication and wallet integration

**Usage**:
```python
# Register a service
container.register_service('my_service', instance, is_optional=False)

# Get a service
service = container.get_service('my_service')

# Get multiple required services
services = container.get_required_services(['db', 'llm', 'memory'])

# Check service availability
if container.has_service('optional_service'):
    service = container.get_service('optional_service')
```

### 4. Bot (`bot.py`)

Main application orchestrator managing lifecycle and component coordination.

**Startup Sequence**:
1. **Core Initialization**: Configuration, logging, container setup
2. **Module Initialization**: Database, LLM, memory systems
3. **Tool Initialization**: Browser, filesystem, MCP tools
4. **Manager Initialization**: Conversation managers
5. **Agent Initialization**: AI agent setup and configuration
6. **Auth Initialization**: Wallet auth, credits, payments

### 5. Initialization (`initialization.py`)

Orchestrates the complex initialization sequence of interdependent components.

**Key Functions**:
- `initialize_core()` - Core components (config, logging, container)
- `initialize_modules()` - Database, LLM, memory systems
- `initialize_tools()` - Tool services
- `initialize_managers()` - State managers
- `initialize_agents()` - AI agents
- `cleanup_core()` - Resource cleanup
- `cleanup_tools()` - Tool cleanup

**Initialization Order**:
1. Core components (config, logging, container)
2. Database and storage systems
3. LLM and AI service clients
4. Memory and caching systems
5. External service integrations
6. Business logic managers
7. AI agents

### 6. Logging (`logging.py`)

Centralized logging configuration with component-level granularity.

**Key Functions**:
- `setup_logging(log_level)` - Initialize logging system
- `get_component_logger(name)` - Get component-specific logger
- `initialize_task_logging()` - Setup task agent logging

**Logger Hierarchy**:
```
root
├── Bot.core               # Core application functionality
├── Bot.config             # Configuration system
├── Bot.container          # Dependency container
├── Bot.agents             # AI agents
├── Bot.modules            # Functional modules
├── Bot.services           # External services
└── task                   # Task agent logging
```

### 7. Security Logging Filter (`security_logging_filter.py`)

Security-aware log filtering to prevent sensitive data exposure.

**Features**:
- API key masking in log output
- Credential filtering
- Sensitive data redaction
- Configurable filter patterns

### 8. Permissions (`permissions.py`)

Role-based access control system with fine-grained permission management.

**Permission System**:
```python
ROLES = {
    'super_admin': {'all_permissions': True},
    'admin': {
        'use_bot': True,
        'manage_users': True,
        'manage_modes': True,
        'manage_knowledge': True,
        'manage_prompts': True
    },
    'moderator': {
        'use_bot': True,
        'manage_modes': True
    },
    'user': {
        'use_bot': True
    }
}
```

### 9. Exceptions (`exceptions.py`)

Comprehensive exception hierarchy for precise error handling.

**Exception Hierarchy** (abridged):
```python
BotError                              # Base exception for all bot errors
├── ComponentError                    # Base for component-related errors
│   ├── ComponentInitializationError  # Component startup failures
│   ├── ComponentCleanupError         # Component cleanup failures
│   ├── ConfigurationError            # Configuration validation failed
│   ├── DependencyError               # Dependency validation/resolution
│   ├── ServiceError                  # Service operation failures
│   │   └── MCPError ...              # MCP connection/protocol/tool errors
│   ├── ToolError                     # Tool-specific errors
│   ├── HandlerError                  # Handler-related errors
│   ├── AgentError                    # AI agent errors
│   │   └── SessionError ...          # Session / HITL errors (MessageQueueFull, NotFound, State)
│   ├── LLMError                      # LLM errors (Config, Connection, RateLimit, Permanent, ...)
│   ├── DatabaseError                 # Database errors
│   ├── PermissionError               # Access control violations
│   └── ValidationError               # Validation errors
├── ContainerError                    # Dependency-container errors (direct child of BotError)
├── APIError                          # External API errors (Authentication, RateLimit, NotFound)
├── AuthError                         # Auth/identity errors (status_code hint; UserNotFound, Tier)
├── InsufficientCreditsError          # Credit balance too low (status_code 402)
└── ... (Conversation, Storage, Prompt, Embedding, Cache, Memory, Manager, Module, ...)
```
Note: most domain errors descend from `ComponentError`, not directly from
`BotError`. `ContainerError`, `APIError` and `AuthError` are direct `BotError`
children. See `exceptions.py` for the full list.

## Core Components Registry

The package exports a `CORE_COMPONENTS` registry:
```python
CORE_COMPONENTS = {
    'config': {
        'class': BotConfig,
        'description': 'Configuration',
        'required': True,
        'dependencies': {'required': [], 'optional': []}
    },
    'rate_limit_manager': {
        'class': RateLimitManager,
        'description': 'Rate limiting',
        'required': True,
        'dependencies': {'required': ['config'], 'optional': []}
    },
    'permissions': {
        'class': Permissions,
        'description': 'Permissions management',
        'required': True,
        'dependencies': {'required': ['config'], 'optional': []}
    }
}
```

## Best Practices

### Component Development
1. **Inherit from BaseComponent**: Always extend `BaseComponent` for lifecycle management
2. **Declare Dependencies**: Use `required_dependencies` property for explicit dependency declaration
3. **Handle Initialization**: Implement `_initialize()` and `_cleanup()` methods properly
4. **Use Container**: Access dependencies through the container, not direct imports
5. **Error Handling**: Use appropriate exception types for different error conditions

### Configuration Management
1. **Environment Variables**: Use environment variables for sensitive configuration
2. **Type Annotations**: Always use proper type hints for configuration fields
3. **Validation**: Implement validators for complex configuration values
4. **Defaults**: Provide sensible defaults for optional configuration
5. **Documentation**: Document configuration options clearly

### Logging Guidelines
1. **Component Loggers**: Use `get_component_logger()` for component-specific logging
2. **Log Levels**: Use appropriate log levels (DEBUG, INFO, WARNING, ERROR)
3. **Structured Messages**: Include relevant context in log messages
4. **Error Logging**: Always log exceptions with stack traces using `exc_info=True`
5. **Performance**: Avoid expensive operations in debug logs

## Performance Considerations

- **Lazy Loading**: Services initialize only when needed
- **Async Operations**: All I/O operations are asynchronous
- **Resource Management**: Proper cleanup prevents resource leaks
- **Singleton Pattern**: Efficient resource sharing through singletons
- **Caching**: Configuration and service instances are cached

## Security Features

- **API Key Validation**: Secure handling and validation of API keys
- **Permission System**: Role-based access control
- **Input Validation**: Comprehensive input validation and sanitization
- **Error Information**: Careful error message handling to prevent information leakage
- **Log Filtering**: Security-aware log filtering to prevent credential exposure

## Exports

```python
__all__ = [
    # Base components
    'BaseComponent', 'Bot', 'BotConfig', 'DependencyContainer',
    
    # Core managers 
    'Permissions', 'RateLimitManager',
    
    # Initialization
    'initialize_core', 'initialize_modules', 'initialize_tools',
    'initialize_managers', 'initialize_agents', 'cleanup_core', 'cleanup_tools',
    
    # Logging
    'setup_logging', 'get_component_logger', 'initialize_task_logging',
    
    # Component metadata
    'CORE_COMPONENTS', 'MODULE_METADATA', 'MODULE_INIT_ORDER'
]
```
