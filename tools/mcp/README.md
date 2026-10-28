# MCP client (`tools/mcp/`)

This package connects POLYROB to external
[Model Context Protocol](https://modelcontextprotocol.io) servers. One `mcp` tool gives
the agent access to every configured server; each server tool also appears to the model
as a direct action named `{server}_{tool}`. The tool is not in the default tool list — a
session must request it.

Supported transports: `stdio`, `sse`, `http` and `streamable_http`. No server is
configured out of the box.

## Configure servers

- Turn it on and add servers: see the MCP section of
  [../../docs/guide/configuration.md](../../docs/guide/configuration.md#mcp).
  The defaults ship in `config/mcp_config.json`; your servers go in
  `~/.polyrob/mcp.json` (secrets as `${VAR}` placeholders) or are added with `/mcp add`.
  `MCP_SERVERS_CONFIG` (a JSON string) is the fallback when the defaults file is absent.
  A project-local `.polyrob/mcp.json` is never loaded.
- Add, remove and test servers from chat with `/mcp`:
  [../../docs/guide/owner-controls.md](../../docs/guide/owner-controls.md#mcp-servers-giving-the-agent-new-tools).
- Manage per-tenant servers over HTTP (`/api/mcp/*`), or run POLYROB as an MCP server
  (`POST /mcp`): [../../docs/guide/api.md](../../docs/guide/api.md#mcp-server-management-outbound).
- Every flag (`MCP_ENABLED`, `MCP_OAUTH_ENABLED`, `MCP_SELF_INSTALL_ENABLED`,
  `MCP_INSTALL_CATALOG_FILE`, `MCP_ENCRYPTION_KEY`):
  [../../docs/CONFIGURATION.md](../../docs/CONFIGURATION.md).
- What an MCP server can and cannot reach:
  [../../docs/guide/security-model.md](../../docs/guide/security-model.md).

## Main files

| File | Role |
|---|---|
| `mcp_tool.py` | `MCPTool` — the agent-facing tool and its actions |
| `server_manager.py` | Server connections, discovery, circuit breakers |
| `protocol.py` | The MCP client and the four transports |
| `config.py` | Config models and `${VAR}` resolution |
| `user_mcp_service.py` | Per-tenant servers (HTTPS only, SSRF-checked, encrypted credentials) |
| `child_env.py` | The environment a stdio server process receives (allowlist only) |
| `oauth_bridge.py` | OAuth `Authorization` headers for HTTP-family servers |
| `catalog.py`, `self_install.py` | The vetted catalog behind the agent's `mcp_install` |
| `param_coercion.py` | JSON-schema argument coercion |
| `subscriptions.py` | Resource-update subscriptions |

## Check the install

```bash
python -c "from tools.mcp import MCPTool; print('OK')"
```
