# Tools Package - External Integration Framework

_Last reviewed: 2026-06-30. For the authoritative architecture see ../AGENTS.md; for env flags see ../docs/CONFIGURATION.md._

> The authoritative list of registered tools is `tools/__init__.py` (descriptors live in
> `tools/descriptors.py`; the `register_tool_class(...)` calls in `__init__.py` show what is wired,
> including the flag-gated tools). The lists in this README are illustrative and may lag.

## Overview

The `tools` package provides a comprehensive framework for integrating with external services, APIs, and automation capabilities. It implements a sophisticated tool architecture with standardized lifecycle management, action registration, dependency injection, and robust error handling. The package enables the POLYROB platform to interact with web browsers, document processing systems, social media platforms, blockchain services, MCP servers, and various other external systems.

## Architecture Philosophy

- **Tool Abstraction**: Standardized interface for all external integrations
- **Action-Based**: Decorated methods that can be dynamically discovered and registered
- **Dependency Management**: Explicit tool dependencies with graceful degradation
- **Rate Limiting**: Built-in rate limiting for API protection and compliance
- **Error Resilience**: Comprehensive error handling and recovery mechanisms
- **Resource Management**: Proper lifecycle management and cleanup

## Package Structure

```
tools/
├── __init__.py                     # Tool registry and initialization
├── README.md                       # This documentation
├── base_tool.py                    # Abstract base class for all tools
│
├── descriptors.py                  # Tool descriptors (TOOL_DESCRIPTORS) — registration metadata
├── exceptions.py                   # Tool-package exception types
├── filesystem.py                   # File system and document processing
├── filesystem_pdf.py               # PDF extraction helper (used by filesystem)
├── filesystem_docproc.py           # Document-processing helper (used by filesystem)
├── user_directory.py               # Per-user directory resolution helper
├── email_tool.py                   # Email communication
├── perplexity_tool.py              # Perplexity AI search integration
├── twitter_tool.py                 # Twitter/X platform integration
├── task_tool.py                    # Task (TODO) management tool
├── knowledge_ingest.py             # Knowledge-base ingest tool (KnowledgeTool, gated KB_ENABLED)
├── cronjob_tools.py                # Durable cron scheduling tool (gated CRON_ENABLED)
├── goal_tools.py                   # Durable goal-board tool (gated GOALS_ENABLED)
│
├── anysite/                        # AnySite structured web-data CLI tool (AnysiteTool, anysite_api)
│   ├── __init__.py
│   ├── tool.py                     # AnysiteTool (registered in __init__.py)
│   └── client.py                   # AnySite HTTP client
├── web_fetch/                      # Stateless lightweight web fetch (WebFetchTool — always registered)
│   ├── __init__.py
│   ├── tool.py                     # WebFetchTool
│   ├── fetcher.py                  # HTTP fetch
│   └── render.py                   # HTML → markdown rendering
│
├── polymarket/                     # Polymarket market data
├── hyperliquid/                    # Hyperliquid market data
├── code_exec/                      # Sandboxed code execution (gated CODE_EXEC_ENABLED)
├── coding/                         # Coding tools (gated CODING_TOOLS_ENABLED)
├── oauth/                          # OAuth manager (library)
├── x402/ (see modules/x402)        # x402 paying tool (gated X402_CLIENT_ENABLED)
│
├── browser/                        # Web browser automation
│   ├── __init__.py
│   ├── browser.py                  # Main browser controller
│   ├── browser_manager.py          # Browser instance management
│   ├── context.py                  # Browser context management
│   ├── actions.py                  # Browser action definitions
│   ├── views.py                    # Browser view models
│   └── playwright_utils.py         # Playwright utilities
│
├── alchemy/                        # Alchemy NFT API integration
│   ├── __init__.py
│   └── alchemy_tool.py             # NFT and blockchain data
│
├── collabland/                     # CollabLand integration
│   ├── __init__.py
│   └── collabland_tool.py          # Token verification
│
├── mcp/                            # Model Context Protocol
│   ├── __init__.py
│   ├── mcp_tool.py                 # Main MCP tool (MCPTool — the service implementation)
│   ├── server_manager.py           # MCP server management
│   ├── config.py                   # MCP configuration (${VAR} secret resolution)
│   ├── protocol.py                 # MCP protocol implementation
│   ├── param_coercion.py           # Tool-arg param coercion
│   ├── rate_limit.py               # MCPExecRateLimiter (per user/server)
│   ├── security.py                 # MCPEncryption (Fernet secret store)
│   ├── subscriptions.py            # ResourceSubscriptionRegistry
│   ├── user_mcp_service.py         # Per-user MCP service
│   ├── validation_tracker.py       # MCPValidationTracker (schema-injection policy)
│   ├── views.py                    # MCP view models
│   └── README.md                   # MCP-specific documentation
│
├── controller/                     # Tool orchestration (Controller composed from mixins)
│   ├── __init__.py
│   ├── service.py                  # Controller core (composes the mixins via MRO)
│   ├── execution.py                # ExecutionMixin (multi_act/act + retry/telemetry)
│   ├── tool_management.py          # ToolManagementMixin (load/add/configure/remove/get/list)
│   ├── introspection.py            # IntrospectionMixin (registry accessors + MCP prompt builders)
│   ├── action_registration.py      # ActionRegistrationMixin (default-action closures)
│   ├── hooks.py                    # HookPipeline (pre/post/transform fail-mode engine)
│   ├── mcp_registrar.py            # MCPActionRegistrar
│   ├── approval.py                 # ApprovalProvider ABC + make_approval_hook
│   ├── delegation.py               # evaluate_delegation role/depth policy
│   ├── _helpers.py                 # observe/ToolInfo/make_denylist_hook/build_load_skill_result
│   ├── execution_context.py        # Execution context management
│   ├── types.py                    # ActionResult and shared types
│   ├── views.py                    # Controller view models
│   └── registry/                   # Action registry system
│       ├── service.py              # Registry service (validation + schema cache)
│       ├── schema_generators.py    # Per-provider schema generation
│       ├── schema_sanitizer.py     # sanitize_emitted_tools (hostile-schema fixups)
│       └── views.py                # Registry views
│
└── dom/                            # DOM manipulation utilities
    ├── __init__.py
    ├── service.py                  # DOM service
    ├── views.py                    # DOM view models
    ├── buildDomTree.js             # DOM tree builder
    └── history_tree_processor/     # History processing
        ├── service.py
        └── view.py
```

## Core Tool Framework

### BaseTool (`base_tool.py`)

Abstract base class providing standardized tool lifecycle, action registration, and dependency management.

**Tool Status Enum**:
```python
class ToolStatus(Enum):
    UNINITIALIZED = "uninitialized"
    INITIALIZING = "initializing"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"
```

**Core Interface**:
```python
class BaseTool(BaseComponent):
    def __init__(self, name: str, config: BotConfig, container: DependencyContainer):
        self._status = ToolStatus.UNINITIALIZED
        self._services = {}  # Injected dependencies
        self._enabled = True
        
    @property
    def required_services(self) -> Dict[str, str]:
        """Services this tool depends on"""
        return {'rate_limit_manager': 'Rate limit management'}
    
    @property
    def optional_services(self) -> Dict[str, str]:
        """Optional service dependencies"""
        return {}
    
    @staticmethod
    def action(description: str, param_model: Optional[Type[BaseModel]] = None):
        """Decorator to mark methods as discoverable actions"""
```

## Available Tools

### 1. FileSystem (`filesystem.py`)

Document processing and file operations.

**Capabilities**:
- Document Processing: PDF, DOCX, TXT, Markdown, code files
- Web Content Extraction: URL processing with metadata extraction
- File Operations: Read, write, append, list, delete
- Content Analysis: LLM-powered document analysis

**Actions**:
```python
@BaseTool.action('Process a document')
async def process_document(self, params: DocProcessAction) -> str

@BaseTool.action('Process content from a URL')
async def process_url(self, params: DocProcessUrlAction) -> Dict[str, Any]

@BaseTool.action('Analyze a document using LLM')
async def analyze_document(self, params: DocAnalyzeAction) -> Dict[str, Any]

@BaseTool.action('Read a file from the workspace')
async def read_file(self, params: ReadFileAction) -> str
```

### 2. Browser (`browser/`)

Advanced web browser automation built on Playwright.

**Components**:
- **Browser**: Main browser controller with environment adaptation
- **BrowserManager**: Instance and resource management
- **BrowserContext**: Isolated browser sessions
- **Actions**: Navigation, interaction, extraction

**Capabilities**:
- Web navigation and interaction
- Content extraction
- Screenshot capture
- Form filling
- Server environment support (Xvfb)
- Anti-detection features

**Usage**:
```python
browser = Browser(headless=True)
context = await browser.new_context()
await context.goto("https://example.com")
content = await context.extract_content()
await browser.close()
```

### 3. Twitter (`twitter_tool.py`)

Twitter/X platform integration.

**Actions**:
```python
@BaseTool.action('Post a tweet')
async def post_tweet(self, text: str) -> str

@BaseTool.action('Search for tweets')
async def search_tweets(self, query: str, count: int = 10) -> List[Dict]

@BaseTool.action('Get user timeline')
async def get_user_timeline(self, username: str) -> List[Dict]
```

**Features**:
- Rate limit compliance
- Tweet and user data caching
- Database integration

### 4. Perplexity (`perplexity_tool.py`)

AI-powered search and research.

**Actions**:
```python
@BaseTool.action('Search using Perplexity AI')
async def search(self, query: str) -> Dict[str, Any]

@BaseTool.action('Ask a question to Perplexity')
async def ask(self, question: str) -> str
```

### 5. Email (`email_tool.py`)

Email communication via Gmail.

**Actions**:
```python
@BaseTool.action('Send email')
async def send_email(self, to: str, subject: str, body: str) -> str

@BaseTool.action('Read emails')
async def read_emails(self, folder: str = 'INBOX', limit: int = 10) -> List[Dict]
```

**Configuration**:
```python
gmail_email: str  # Gmail address
gmail_app_password: str  # App-specific password
```

### 6. Task (`task_tool.py`)

Task and todo list management.

**Actions**:
```python
@BaseTool.action('Add a task')
async def add_task(self, title: str, description: str = None) -> Dict

@BaseTool.action('List tasks')
async def list_tasks(self, status: str = None) -> List[Dict]

@BaseTool.action('Complete a task')
async def complete_task(self, task_id: str) -> Dict
```

### 7. Alchemy (`alchemy/`)

NFT and blockchain data via Alchemy API.

**Actions**:
```python
@BaseTool.action('Get NFTs for owner')
async def get_nfts_for_owner(self, owner_address: str) -> List[Dict]

@BaseTool.action('Get NFT metadata')
async def get_nft_metadata(self, contract_address: str, token_id: str) -> Dict
```

### 8. CollabLand (`collabland/`)

Token verification and community management.

**Actions**:
```python
@BaseTool.action('Verify token ownership')
async def verify_token_ownership(self, wallet: str, contract: str) -> bool

@BaseTool.action('Get community roles')
async def get_community_roles(self, user_id: str) -> List[str]
```

### 9. MCP (`mcp/`)

Model Context Protocol integration for connecting to MCP servers.

**Features**:
- Multi-server support (STDIO and SSE)
- Tool execution from MCP servers
- Resource management
- Dynamic action registration

**See** `tools/mcp/README.md` for detailed MCP documentation.

**Actions**:
```python
@BaseTool.action('Execute MCP tool')
async def execute_tool(self, server: str, tool: str, args: Dict) -> Any

@BaseTool.action('List MCP tools')
async def list_tools(self, server: str = None) -> List[Dict]

@BaseTool.action('Read MCP resource')
async def read_resource(self, server: str, uri: str) -> Any
```

## Tool Controller (`controller/`)

Central orchestration for tool management and action routing.

### Controller (`controller/service.py`)

`Controller` is a thin core that **composes focused mixins via MRO** rather than holding all the
logic inline (the god-file split, UP-11). `service.py` keeps `__init__`, the MCP/hook delegation
shims, and `_ensure_normalize_path_exists`; the behavior lives in:

```python
class Controller(ExecutionMixin, ToolManagementMixin, IntrospectionMixin, ActionRegistrationMixin):
    ...
```

- **`ExecutionMixin`** (`execution.py`) — the hot path: `multi_act`/`act` + retry/telemetry
- **`ToolManagementMixin`** (`tool_management.py`) — load/add/configure/remove/get/list tools
- **`IntrospectionMixin`** (`introspection.py`) — registry accessors + MCP prompt builders
- **`ActionRegistrationMixin`** (`action_registration.py`) — the default-action closures
  (`send_message`/`done`/`load_skill`/`session_search`/`memory`/delegation, etc.)

Plus the extracted collaborators:

- **`HookPipeline`** (`hooks.py`) — pre/post/transform tool-call hook engine with fail-mode policy
- **`MCPActionRegistrar`** (`mcp_registrar.py`) — registers MCP server tools as actions
- **helpers** (`_helpers.py`) — `observe`/`ToolInfo`/`make_denylist_hook`/`build_load_skill_result`
  (re-exported by `service.py` so existing imports keep working)
- **`approval.py`** — `ApprovalProvider` ABC (`AutoApprover`/`DenyByDefaultApprover`) +
  `make_approval_hook`
- **`delegation.py`** — `evaluate_delegation` pure role/depth policy
- **`types.py`** — `ActionResult` and shared types; **`execution_context.py`** — execution context

> NOTE: the mixin modules deliberately do **NOT** use `from __future__ import annotations` — it
> stringizes the action closures' first-param annotations, which the Registry introspects to route
> the validated param model.

### Action Registry (`controller/registry/`)

Dynamic action discovery and registration system.

```python
class ActionRegistry:
    def register_tool_actions(self, tools: Dict[str, BaseTool]):
        """Auto-discover and register tool actions"""
    
    def get_action(self, name: str) -> Optional[Callable]:
        """Retrieve registered action by name"""
    
    def get_action_schema(self, name: str) -> Dict:
        """Get OpenAPI-compatible schema for action"""
```

## Tool Registry

### Registered tools

The authoritative registration lives in `tools/__init__.py` (`register_tool_class(...)`). As of this
review the always-registered tools are:

```python
# tools/__init__.py — always registered
register_tool_class('filesystem', FileSystem)
register_tool_class('task', TaskTool)          # the TODO tool (NOT delegation)
register_tool_class('twitter', TwitterTool)    # guarded import (optional dependency)
register_tool_class('email', EmailTool)
register_tool_class('perplexity', PerplexityTool)
register_tool_class('web_fetch', WebFetchTool) # stateless lightweight web fetch
register_tool_class('collabland', CollabLandTool)
register_tool_class('alchemy', AlchemyTool)
register_tool_class('mcp', MCPTool)
register_tool_class('anysite', AnysiteTool)
```

Additional tools are registered conditionally (behind feature flags):
`browser_manager`, `polymarket`, `hyperliquid`, plus `code_exec` (`CODE_EXEC_ENABLED`),
`coding` (`CODING_TOOLS_ENABLED`), `cronjob` (`CRON_ENABLED`), `goal` (`GOALS_ENABLED`),
`knowledge` (`KB_ENABLED`, via `register_knowledge_tool`), and the
x402 paying tool (`X402_CLIENT_ENABLED`). See `tools/__init__.py` for the exact, current set.

### Initialization Order
```python
TOOL_INIT_ORDER = [
    'filesystem',    # FileSystem first as it's used by others
    'task',          # Task management tool second
    'perplexity',    # Search services
    'twitter',       # Social/communication services
    'email',
    'collabland',    # Token verification services
    'alchemy',       # Alchemy as fallback for CollabLand
    'mcp'            # MCP service last - advanced feature
]
```

### Dependencies
```python
TOOL_DEPENDENCIES = {
    'filesystem': {
        'required': ['rate_limit_manager'],
        'optional': ['llm_client']
    },
    'twitter': {
        'required': ['rate_limit_manager', 'database_manager'],
        'optional': [],
        'rate_limited': True
    },
    'mcp': {
        'required': ['rate_limit_manager'],
        'optional': ['cache_manager']
    },
    # ...
}
```

### Metadata
```python
TOOL_METADATA = {
    'filesystem': {
        'name': 'FileSystem',
        'description': 'Processes various document types',
        'required': True,
        'is_core': False
    },
    'twitter': {
        'name': 'Twitter Service',
        'description': 'Twitter API integration',
        'required': False,
        'rate_limited': True,
        'rate_limit_settings': {
            'default_wait': 900,
            'requests_per_minute': 300,
            'burst_limit': 50
        }
    },
    # ...
}
```

## Rate Limiting

```python
RATE_LIMITS = {
    'twitter': {
        'requests_per_minute': 300,
        'burst_limit': 50,
    },
    # Per-tool rate limits are enforced via the rate_limit_manager dependency.
}
```

## Utility Functions

```python
async def validate_tools(requested_tools: List[str]) -> Tuple[List[str], List[str]]:
    """Validate requested tools, returns (valid, invalid)"""

async def get_available_tools() -> Dict[str, str]:
    """Get dictionary of available tools and descriptions"""

async def initialize_tool(tool_name: str, tool: BaseTool, logger) -> bool:
    """Initialize a tool with proper error handling"""

async def cleanup_tools(tools: Dict[str, Any]) -> None:
    """Clean up tools in reverse dependency order"""

def get_dependencies(tool_name: str) -> List[str]:
    """Get dependencies for a tool"""
```

## Usage Examples

### Tool Initialization
```python
from tools import FileSystem, initialize_tool

# Create tool
filesystem = FileSystem(
    name='filesystem',
    config=config,
    container=container
)

# Initialize with dependency injection
success = await initialize_tool('filesystem', filesystem, logger)
```

### Using Tools via Controller
```python
# Get controller
controller = container.get_service('tool_controller')

# Execute action
result = await controller.execute_action(
    tool='filesystem',
    action='process_document',
    params={'content': 'Document text...'}
)
```

### Direct Tool Usage
```python
# Get tool from container
filesystem = container.get_service('filesystem')

# Call action directly
result = await filesystem.process_url({
    'url': 'https://example.com/document.pdf'
})
```

## Best Practices

### Tool Development
1. **Inherit from BaseTool**: Always extend the base tool class
2. **Use Action Decorator**: Mark public methods with `@action` decorator
3. **Implement Error Handling**: Comprehensive error handling and recovery
4. **Resource Management**: Proper initialization and cleanup
5. **Rate Limiting**: Respect external API limits

### Action Design
1. **Clear Descriptions**: Provide meaningful action descriptions
2. **Parameter Models**: Use Pydantic models for parameter validation
3. **Return Types**: Consistent return type patterns
4. **Error Messages**: Informative error messages for debugging

### Browser Automation
1. **Server Compatibility**: Use auto-configuration for server environments
2. **Resource Cleanup**: Always close browser contexts and instances
3. **Error Recovery**: Handle page load failures and timeouts
4. **Anti-Detection**: Use realistic interaction patterns

## Exports

```python
__all__ = [
    'BaseTool', 'ToolStatus',
    'FileSystem', 'TaskTool', 'TwitterTool',
    'PerplexityTool', 'EmailTool',
    'CollabLandTool', 'AlchemyTool', 'MCPTool',
    'TOOL_COMPONENTS', 'TOOL_DEPENDENCIES', 'TOOL_METADATA',
    'initialize_tool', 'cleanup_tools',
    'validate_tools', 'get_available_tools'
]
```
