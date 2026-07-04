# MCP (Model Context Protocol) Service

_Last reviewed: 2026-06-30. For the authoritative architecture see ../../AGENTS.md; for env flags see ../../docs/CONFIGURATION.md._

## Overview

The MCP Service provides POLYROB with the ability to connect to and interact with multiple MCP (Model Context Protocol) servers. This enables POLYROB to access a wide variety of tools and resources from the MCP ecosystem, including file systems, APIs, databases, and more.

## Features

### 🔌 **Multi-Server Support**
- Connect to multiple MCP servers simultaneously
- Support for STDIO, SSE, HTTP, and Streamable HTTP connection types
- Automatic server discovery and capability detection
- Connection pooling and health monitoring

### 🛠️ **Tool Execution**
- Execute tools from any connected MCP server
- Dynamic action registration in POLYROB's action system
- Parameter validation and type safety
- Rate limiting and error handling

### 📂 **Resource Management**
- Read resources from MCP servers
- Resource caching with configurable TTL
- Resource subscription support (planned)
- MIME type detection

### ⚙️ **Configuration-Driven**
- JSON/YAML configuration for all servers
- Environment variable support with `${VAR}` syntax
- Hot-reload capability without service restart
- Security-first approach (disabled by default)

### 🔒 **Security & Reliability**
- Disabled by default for security
- Comprehensive error handling and logging
- Automatic reconnection and retry logic
- Circuit breaker pattern for failing servers

## Configuration

The MCP service is completely generic and server-agnostic. You can configure any MCP server dynamically.

### Basic Configuration

1. **Enable MCP Service** in your `.env` file:
   ```bash
   MCP_ENABLED=true
   ```

2. **Configure Servers** via JSON:
   ```bash
   MCP_SERVERS_CONFIG='{"example-server":{"type":"stdio","command":["npx","-y","@example/mcp-server"],"env":[{"name":"EXAMPLE_API_KEY","value":"${EXAMPLE_API_KEY}"}],"enabled":true,"timeout":30}}'
   
   # Set the actual API key
   EXAMPLE_API_KEY=fc-your-api-key-here
   ```

### Advanced Configuration

#### Option 1: JSON Configuration File

Create a file `config/mcp_servers.json`:
```json
{
  "example-server": {
    "type": "stdio",
    "command": ["npx", "-y", "@example/mcp-server"],
    "env": [
      {"name": "EXAMPLE_API_KEY", "value": "${EXAMPLE_API_KEY}"}
    ],
    "enabled": true,
    "timeout": 30,
    "retry_attempts": 3,
    "retry_delay": 5
  }
}
```

Then reference it in your `.env`:
```bash
MCP_ENABLED=true
MCP_SERVERS_CONFIG=$(cat config/mcp_servers.json)
EXAMPLE_API_KEY=fc-your-api-key-here
```

#### Option 2: Programmatic Configuration

```python
config.mcp = {
    "enabled": True,
    "servers": {
        # Add any MCP server dynamically
        "your_server": {
            "type": "stdio",  # or "sse"
            "command": ["command", "to", "start", "server"],
            "env": [
                {"name": "ENV_VAR", "value": "${ENV_VAR}"}
            ],
            "enabled": True,
            "timeout": 30
        }
    },
    "global_timeout": 60,
    "max_concurrent_connections": 10
}
```

#### Option 3: Using Helper Functions

```python
from tools.mcp.config import get_default_mcp_config, add_mcp_server, create_stdio_server

# Start with empty config
config = get_default_mcp_config()
config.enabled = True

# Add any server dynamically
example_server = create_stdio_server(
    command=["npx", "-y", "@example/mcp-server"],
    env_vars={"EXAMPLE_API_KEY": "${EXAMPLE_API_KEY}"},
    enabled=True,
    timeout=30
)
config = add_mcp_server(config, "example-server", example_server)
```

### Example: Configuring an stdio MCP server

Here is how to configure a local stdio-based MCP server (example: a web-scraper server):

1. **Set environment variables**:
   ```bash
   MCP_ENABLED=true
   EXAMPLE_API_KEY=fc-your-api-key-here
   ```

2. **Configure the server** (choose one method):
   
   Via JSON in env:
   ```bash
   MCP_SERVERS_CONFIG='{"example-server":{"type":"stdio","command":["npx","-y","@example/mcp-server"],"env":[{"name":"EXAMPLE_API_KEY","value":"${EXAMPLE_API_KEY}"}],"enabled":true}}'
   ```
   
   Or programmatically in your code:
   ```python
   config.mcp = {
       "enabled": True,
       "servers": {
           "example-server": {
               "type": "stdio",
               "command": ["npx", "-y", "@example/mcp-server"],
               "env": [{"name": "EXAMPLE_API_KEY", "value": "${EXAMPLE_API_KEY}"}],
               "enabled": True
           }
       }
   }
```

## Available Actions

The MCP service exposes the following actions through POLYROB's action system:

### Tool Operations
- `mcp.execute_tool` - Execute a tool on an MCP server
- `mcp.list_tools` - List available tools from all or specific servers

### Resource Operations
- `mcp.read_resource` - Read a resource from an MCP server
- `mcp.list_resources` - List available resources from all or specific servers

### Server Management
- `mcp.list_servers` - List all MCP servers and their status
- `mcp.get_server_status` - Get detailed status of a specific server
- `mcp.connect_server` - Connect to a specific server
- `mcp.disconnect_server` - Disconnect from a specific server
- `mcp.reload_server` - Reload a server connection
- `mcp.get_capabilities` - Get capabilities of a specific server
- `mcp.health_check` - Perform health check on servers

### Resource Subscriptions (implemented)
- `mcp.subscribe_resource` - Subscribe to resource updates
- `mcp.unsubscribe_resource` - Unsubscribe from resource updates

These are live: `resources/subscribe`/`unsubscribe` are wired through `MCPClient` (`protocol.py`)
and `notifications/resources/updated` is dispatched via `tools/mcp/subscriptions.py`
(`ResourceSubscriptionRegistry`). See ../../AGENTS.md (MCP resource subscriptions).

## Usage Examples

The tool class is `MCPTool` (`tools/mcp/mcp_tool.py`); its execution entry point is
`MCPTool.execute_tool(...)`.

### Execute a Tool

```python
# Through POLYROB's action system
result = await mcp_tool.execute_tool({
    "server_name": "filesystem",
    "tool_name": "read_file", 
    "arguments": {"path": "/path/to/file.txt"}
})
```

### Read a Resource

```python
content = await mcp_tool.read_resource({
    "server_name": "github",
    "resource_uri": "repo://owner/repo/README.md"
})
```

### List Available Tools

```python
tools = await mcp_tool.list_tools({
    "server_name": "filesystem"  # Optional: filter by server
})
```

## LLM Integration

MCP tools are automatically available to the LLM when the MCP tool is loaded in a task session.

### How It Works

1. **Discovery**: MCP servers are queried for available tools via `tools/list`
2. **Conversion**: Tool schemas are converted to LLM provider format (OpenAI, Anthropic, etc.)
3. **Registration**: Tools appear in the LLM's tool list as `{server}_{toolname}`
4. **Execution**: When LLM calls an MCP tool, it's routed to the correct server

### Example Flow

```python
# Create session with MCP enabled
session_config = {
    "task": "Scrape example.com and summarize",
    "tools": ["browser", "filesystem", "mcp"],
    "tools_config": {
        "mcp": {
            "servers": ["example-server"]
        }
    }
}

# Agent initialization
agent = Agent(
    task="Scrape example.com",
    controller=controller,  # Has MCP tool loaded
    ...
)

# Agent sees these tools:
# [
#     "browser_click",
#     "filesystem_read_file",
#     "example_scrape_url",    # ← MCP tool!
#     "example_crawl_site", # ← MCP tool!
#     ...
# ]

# LLM can call them directly:
# {
#     "tool_calls": [{
#         "name": "example_scrape_url",
#         "args": {"url": "https://example.com"}
#     }]
# }

# Controller routes to MCP → server_manager → example-server
```

### Configuration via API

Enable MCP tools in session creation:

```json
{
    "task": "Use example-server to scrape https://example.com",
    "tools": ["mcp"],
    "tools_config": {
        "mcp": {
            "servers": ["example-server"]
        }
    }
}
```

### Supported LLM Providers

MCP tools are automatically converted to the correct format for:

- **OpenAI** (GPT-4, GPT-4o, o1, o3)
- **Anthropic** (Claude 3.5 Sonnet, Claude 3 Opus)
- **Google** (Gemini Pro, Gemini Flash)
- **DeepSeek**

### Tool Naming Convention

MCP tools are namespaced to avoid collisions:

- MCP server: `example-server`
- MCP tool: `scrape_url`
- LLM sees: `example_scrape_url`

This ensures that tools from different MCP servers don't conflict with each other or with native POLYROB tools.

## Supported MCP Servers

The service comes pre-configured for popular MCP servers:

### File System Server
- **Type**: STDIO
- **Command**: `uvx mcp-server-filesystem`
- **Capabilities**: File operations, directory listing
- **Configuration**: `MCP_FILESYSTEM_ROOT` environment variable

### Brave Search Server
- **Type**: STDIO  
- **Command**: `uvx mcp-server-brave-search`
- **Capabilities**: Web search, news search
- **Configuration**: `BRAVE_API_KEY` environment variable

### GitHub Server
- **Type**: SSE
- **URL**: Configurable endpoint
- **Capabilities**: Repository operations, issue management
- **Configuration**: `GITHUB_TOKEN` environment variable

### Slack Server
- **Type**: STDIO
- **Command**: `uvx mcp-server-slack`
- **Capabilities**: Channel operations, message sending
- **Configuration**: `SLACK_BOT_TOKEN` environment variable

## Adding Custom Servers

To add a new MCP server:

1. **Add to configuration**:
```python
"my_custom_server": {
    "type": "stdio",  # or "sse"
    "command": ["path", "to", "server"],
    "args": ["--arg1", "value1"],
    "env": [
        {"name": "API_KEY", "value": "${MY_API_KEY}"}
    ],
    "enabled": True,
    "timeout": 30
}
```

2. **Set environment variables** (if needed):
```bash
MY_API_KEY=your_api_key
```

3. **Restart POLYROB** - The service will automatically discover and connect to the new server.

## Parameter Coercion (`param_coercion.py`)

Pure, module-level functions extracted from `MCPTool` so argument validation/coercion can be
unit-tested and reused without instantiating the full tool. The module must **not** import
`tools.mcp.mcp_tool` (no circular imports).

```python
coerce_arguments(schema, arguments, tool_name, *, logger=None) -> (converted: dict, errors: list[str])
enhance_schema_with_date_hints(schema) -> enhanced_schema: dict
```

- **`coerce_arguments`** validates and auto-converts an argument dict against a tool's JSON
  Schema (`input_schema`), returning a `(converted, errors)` tuple — the same contract as the
  original `MCPTool._validate_and_convert_parameters`. It checks required params, then per
  declared type: integers (with string and date-string parse, plus `minimum`/`maximum`
  constraints), strings (numerics stringified; dict/list rejected to avoid Python-repr
  garbage), booleans (converted **by value**, not Python truthiness — so `"false"`/`"0"` are
  correctly `False`), arrays, and objects. Unknown types and extra (undeclared) params pass
  through. It is defensively guarded so a malformed/hostile schema or non-dict arguments
  return errors rather than raising.
- **`enhance_schema_with_date_hints`** returns a deep copy of the schema with date-hint
  descriptions and examples added to integer parameters whose names look date-like
  (`date`/`time`/`timestamp`/`from`/`to`/`start`/`end`/`created`/`updated`/...), advertising
  that `YYYY-MM-DD` strings auto-convert to Unix timestamps. `coerce_arguments` calls this
  before validating. The original schema is never mutated.

## Per-User MCP Servers (`user_mcp_service.py`)

A higher-level service for managing **per-user** MCP server configurations (distinct from the
config-driven global servers in `mcp_config.json`). It layers validation, security checks,
encryption, rate limiting, and connection testing over the
`modules.database.user_mcp_servers` storage handler.

Result/value types:
- **`AddServerResult`** — `success`, `server`, `error`, `ready`.
- **`TestConnectionResult`** — `success`, `latency_ms`, `error`, `tools_discovered`, `tools`
  (discovered tool names).
- **`RateLimiter`** — simple in-memory sliding-window limiter (`check(user_id)` /
  `remaining(user_id)`), per `user_id`.

`UserMCPService` (singleton via `init_user_mcp_service` / `get_user_mcp_service` /
`require_user_mcp_service`) provides:
- `add_server(...)` — validates server type (**stdio is blocked for users**; only `sse`/`http`),
  validates the URL (HTTPS only, no internal IPs, via the security URL validator), enforces a
  server-name format and the user's `max_servers` limit, rejects duplicates, and can optionally
  verify the MCP protocol handshake before saving (`verify_connection=True`).
- `get_user_servers` / `get_server` / `get_server_config` (decrypts credentials and builds an
  `MCPServerConfig` for `MCPServerManager`), `update_server`, `delete_server`.
- `test_connection(...)` — performs a real MCP handshake (or a fast HTTP-only check) with
  SSRF validation enabled, recording results and audit logs.
- `get_user_settings` / `update_user_settings`, `get_available_servers_for_session` (merges
  global + user servers), and `health_check_user_servers`.

Security/reliability notes: credentials are stored encrypted (`MCPEncryption`), all
user-supplied URLs are SSRF-validated, requests are rate-limited per user, and error messages
are sanitized (`_sanitize_error`) to strip internal paths/module names before they reach the
caller.

## Security Considerations

### Default Security Posture
- **Disabled by default** - Must be explicitly enabled
- **No default servers enabled** - Each server must be explicitly enabled
- **Environment variable validation** - Prevents accidental credential exposure
- **Command validation** - STDIO commands are validated before execution

### Best Practices
1. **Enable only required servers** - Don't enable unused servers
2. **Use specific paths** - For filesystem servers, use restrictive root paths
3. **Rotate credentials** - Regularly rotate API keys and tokens
4. **Monitor logs** - Enable MCP communication logging for debugging only
5. **Network isolation** - Run MCP servers in isolated environments when possible

## Troubleshooting

### Common Issues

#### Server Won't Connect
```bash
# Check if the MCP server command exists
uvx mcp-server-filesystem --help

# Verify environment variables
echo $BRAVE_API_KEY

# Check POLYROB logs
tail -f logs/rob.log | grep MCP
```

#### Tool Execution Fails
```bash
# List available tools to verify names
curl -X POST http://localhost:8080/action \
  -H "Content-Type: application/json" \
  -d '{"action": "mcp.list_tools", "params": {}}'

# Check server capabilities
curl -X POST http://localhost:8080/action \
  -H "Content-Type: application/json" \
  -d '{"action": "mcp.get_capabilities", "params": {"server_name": "filesystem"}}'
```

#### Health Check Issues
```bash
# Perform health check
curl -X POST http://localhost:8080/action \
  -H "Content-Type: application/json" \
  -d '{"action": "mcp.health_check", "params": {}}'
```

### Debug Mode

Enable debug logging in your configuration:

```python
mcp = {
    "log_mcp_communications": True,  # Enable detailed MCP protocol logging
    # ... other config
}
```

## Development

### Project Structure
```
tools/mcp/
├── __init__.py              # Module exports
├── config.py                # Configuration models and validation (resolves ${VAR} secrets)
├── server_manager.py        # Server connection management
├── protocol.py              # MCP client protocol (incl. resource subscribe/unsubscribe)
├── subscriptions.py         # ResourceSubscriptionRegistry (resource-update dispatch)
├── rate_limit.py            # Per-(user, server) exec rate limiter
├── security.py              # Fernet secret store (MCPEncryption) + URL/SSRF validator
├── validation_tracker.py    # MCP schema-injection policy tracker
├── param_coercion.py        # Pure JSON-schema argument coercion (no MCPTool import)
├── user_mcp_service.py      # Per-user MCP server config service (add/test/manage)
├── views.py                 # Pydantic models for actions
├── mcp_tool.py              # MCPTool — main tool/service implementation
└── README.md               # This file
```

### Adding New Features

1. **New action types** - Add to `views.py` and implement in `mcp_tool.py`
2. **New server types** - Extend `MCPServerType` enum and connection logic
3. **New capabilities** - Update capability discovery in `server_manager.py`

### Testing

```bash
# Test MCP tool imports
python -c "from tools.mcp import MCPTool; print('OK')"

# Test configuration validation
python -c "from tools.mcp.config import get_default_mcp_config; print(get_default_mcp_config())"

# Test with POLYROB
python main.py --test-services
```

## Contributing

When contributing to the MCP service:

1. **Follow POLYROB patterns** - Use existing service patterns and conventions
2. **Add comprehensive tests** - Test both success and failure scenarios
3. **Update documentation** - Keep this README current
4. **Security first** - Consider security implications of new features
5. **Backward compatibility** - Don't break existing configurations

## License

This MCP service implementation is part of the POLYROB project and follows the same license terms.