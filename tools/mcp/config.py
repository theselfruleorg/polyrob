"""Back-compat path: the MCP settings model and gate live in ``core.mcp_config`` (U13)."""
from core.mcp_config import (  # noqa: F401
    DEFAULT_MCP_SERVERS,
    MCPConfig,
    MCPEnvironmentVariable,
    MCPServerConfig,
    MCPServerType,
    add_mcp_server,
    create_stdio_server,
    get_default_mcp_config,
    load_local_mcp_servers,
    mcp_explicitly_off,
    mcp_gate_on,
    resolve_config_environment_variables,
    resolve_environment_variables,
)
