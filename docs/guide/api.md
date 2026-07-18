# API Reference

polyrob exposes a REST API and implements the Google Agent-to-Agent (A2A) protocol. Both are served by the same FastAPI app.

## Start the server

```bash
polyrob serve
```

Binds to `127.0.0.1:9000` by default (`--host`/`--port`, or `UVICORN_HOST`/`UVICORN_PORT`). Requires
at least one LLM provider key configured — see [configuration.md](configuration.md); `polyrob serve`
exits with a clear message if none is found. `--workers` sets the uvicorn worker count (default 1).

Check it's up:

```bash
curl http://localhost:9000/health
```

Interactive API docs (Swagger UI) are at `http://localhost:9000/docs`.

> The Docker image sets `UVICORN_PORT=8000` and maps `8000:8000` — adjust the port in the examples
> below if you're running via Docker.

---

## Authentication

Three authentication methods are supported:

### 1. API key (recommended)

Create an API key and pass it in the `X-API-KEY` header:

```bash
curl -X POST http://localhost:9000/api/auth/api-keys \
  -H "Authorization: Bearer <jwt>" \
  -H "Content-Type: application/json" \
  -d '{"name": "My Integration"}'

# Response:
# {
#   "api_key": "rob_xxx...",   # full key — shown ONCE, store it now
#   "prefix": "rob_xxx",
#   "name": "My Integration",
#   "expires_at": null,
#   "created_at": "2026-01-01T00:00:00Z",
#   "warning": "Store this key securely — it will not be shown again."
# }
```

Use the key on subsequent requests:

```
X-API-KEY: rob_xxx...
```

### 2. Bearer JWT

SIWE (Sign-In with Ethereum) wallet authentication:

```bash
# Step 1: get a nonce
curl -X POST http://localhost:9000/api/auth/nonce \
  -d '{"wallet_address": "0x..."}'

# Step 2: sign the returned message with your wallet, then verify
curl -X POST http://localhost:9000/api/auth/verify \
  -d '{"wallet_address": "0x...", "message": "...", "signature": "0x...", "nonce": "..."}'
# Response (flat; also sets an auth_token cookie):
# { "token": "<jwt>", "user_id": "...", "wallet_address": "0x...", "role": "user", "tier": "free", "is_admin": false, "expires_at": "..." }
```

Pass the JWT as `Authorization: Bearer <jwt>`.

### 3. x402 crypto pay-per-request

No account needed — pay per request with USDC on Base or Ethereum. The server returns a `402 Payment Required` response with payment details; include the signed payment in the `X-PAYMENT` header on retry. x402 receiving is off by default (`X402_ENABLED`, see [../CONFIGURATION.md](../CONFIGURATION.md)). See [modules/x402/README.md](../../modules/x402/README.md) for client library details.

---

## Session endpoints

All `/api/task/*` endpoints require one of the three auth methods above. Creating a session
additionally requires payment (credits from a logged-in account, or x402) unless the caller is an
admin — an unauthenticated `POST /api/task/sessions` gets `402 Payment Required`.

### Create a session

```
POST /api/task/sessions
```

```json
{
  "task": "Go to github.com/trending and list the top 5 repos"
}
```

`model`, `provider`, `tools`, and `max_steps` are optional and default from your config (run
`polyrob model list` to see available models). The response echoes what was actually used:

```json
{
  "ok": true,
  "session_id": "abc123",
  "status": "running",
  "task": "Go to github.com/trending and list the top 5 repos",
  "model": "gpt-5",
  "tools": ["browser", "filesystem"],
  "webview_url": "http://localhost:5050/session/abc123",
  "message": "Session created"
}
```

### Get session status

```
GET /api/task/sessions/{session_id}
```

Returns a user-facing status (`active`/`idle`/`stopped`), whether the caller can cancel or message
it, the resolved model/tools, timestamps, and the `webview_url` for live monitoring.

### Send a message to a running session

```
POST /api/task/sessions/{session_id}/messages
```

```json
{
  "text": "Focus on enterprise use cases only"
}
```

Injects a user guidance message into a running session.

### Cancel a session

```
POST /api/task/sessions/{session_id}/cancel
```

### Check message queue status

```
GET /api/task/sessions/{session_id}/queue-status
```

Returns the number of queued messages and the agent's current status.

### Streaming session events (SSE)

Real-time streaming is available via the **A2A layer** (`POST /a2a/message/stream`) or the WebView Socket.IO interface — not as a `/api/task` route. See the [A2A protocol](#a2a-protocol-agent-to-agent) section below.

---

## A2A protocol (Agent-to-Agent)

polyrob implements Google's A2A protocol for AI agent interoperability. Other AI agents can discover and delegate tasks to polyrob without human involvement.

### Discovery

```
GET /.well-known/agent.json
```

Returns the Agent Card — polyrob's capabilities, supported methods, and authentication options. This is the standard A2A discovery endpoint (no auth required).

```bash
curl http://localhost:9000/.well-known/agent.json
```

### Send a task (JSON-RPC)

```
POST /a2a/rpc
```

```json
{
  "jsonrpc": "2.0",
  "method": "message/send",
  "params": {
    "message": {
      "role": "user",
      "parts": [{"text": "Take a screenshot of example.com"}]
    }
  },
  "id": "1"
}
```

Requires auth via `X-API-KEY`, `Authorization: Bearer <jwt>`, or an x402 payment.

### Streaming task updates (SSE)

```
POST /a2a/message/stream
```

Same request body as `/a2a/rpc`; returns an SSE stream of task progress events in A2A format.

While a task is blocked on owner approval the stream emits a status update with
the native A2A `input-required` state (and returns to `working` when the
approval resolves). `tasks/get` responses carry a `metadata.current_activity`
snapshot (`{phase, detail, seconds_in_state, step, call_id}`, `null` when
unknown) describing what the agent is doing right now.

---

## MCP server management

```
POST /api/mcp/servers                     # Add a custom MCP server
POST /api/mcp/servers/{server_name}/test  # Test connection
GET  /api/mcp/available                   # List available MCP servers and their tools
```

---

## OpenAI-compatible API

polyrob exposes a drop-in OpenAI-style surface so existing OpenAI SDK clients can talk to it. It is **off by default** — enable with `OPENAI_COMPAT_API_ENABLED=true` (see [../CONFIGURATION.md](../CONFIGURATION.md)).

```
POST /v1/chat/completions   # chat over the task agent (non-streaming reply; `stream: true` wraps it in SSE chunks)
GET  /v1/models             # list available models
```

```bash
curl -X POST http://localhost:9000/v1/chat/completions \
  -H "X-API-KEY: rob_xxx..." \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-4o", "messages": [{"role": "user", "content": "Summarize https://example.com"}]}'
```

An OpenAI model string (e.g. `gpt-4o`) is mapped to a polyrob `(provider, model)` pair internally
(a `provider/model` slug wins outright, else known prefixes like `gpt-`/`claude-`/`gemini-` route to
that provider, else your default provider is used). Point any OpenAI SDK at
`http://localhost:9000/v1` with your polyrob API key.

**Streaming honesty:** `stream: true` is *buffered* SSE — the agent turn runs to
completion and the full reply arrives as one content chunk (true token streaming
is a planned upgrade). During long turns the stream emits SSE comment frames
(`: keep-alive`) every ~15s so clients and proxies don't idle-timeout; OpenAI
SDK parsers ignore comment frames per the SSE spec.

---

## Error responses

| Status | Meaning |
|--------|---------|
| `400` | Bad request — invalid parameters |
| `401` | Missing or invalid auth |
| `402` | Payment required (x402 flow, or session creation without credits) |
| `403` | Forbidden — caller does not own this resource, or lacks permission |
| `404` | Session not found |
| `409` | Session is owned by another worker (multi-worker deployments) |
| `429` | Rate limit exceeded |
| `500` | Internal error |
