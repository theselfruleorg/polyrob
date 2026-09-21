# API Reference

polyrob exposes a REST API and implements the Google Agent-to-Agent (A2A) protocol. Both are served by the same FastAPI app.

## Start the server

```bash
polyrob serve
```

Binds to `127.0.0.1:9000` by default (`--host`/`--port`, or `UVICORN_HOST`/`UVICORN_PORT`). Requires
at least one LLM provider key configured — see [configuration.md](configuration.md); `polyrob serve`
exits with a clear message if none is found. `--workers` sets the uvicorn worker count (default 1).

> **`--workers` above 1 needs `API_AUTONOMY_RUNTIME=false`.** Each worker
> starts its OWN cron / goal / curator / settlement runtime on the same data
> dir, and two settlement watchers can double-apply a payment — so `polyrob
> serve --workers 2` refuses while the loops are on. Run the loops in exactly
> one process and start the extra workers with `API_AUTONOMY_RUNTIME=false`.

Check it's up:

```bash
curl http://localhost:9000/health
```

`/health` answers `200` with `{"status": "healthy", ...}` or `503` with
`{"status": "degraded", "reason": "..."}`. Its metrics are `bot_initialized`,
`active_sessions` and `session_capacity`; `active_sessions` is `null` — never
`0` — when the agent cannot be read.

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
#   "created_at": "2026-09-15T12:00:00Z",
#   "warning": "Store this key securely — it will not be shown again."
# }
```

Use the key on subsequent requests:

```
X-API-KEY: rob_xxx...
```

Manage them with `GET /api/auth/api-keys` (list, prefixes only) and
`DELETE /api/auth/api-keys/{key_prefix}` (revoke). `GET /api/auth/me` returns the
caller the server resolved from whichever credential you sent — the fastest way to
confirm auth is working.

> **Minting a key needs the account system.** `POST /api/auth/api-keys` answers
> **503** — with the remedy — on an instance where the account system is off:
> set `ENABLE_AUTH=true`, configure a database, and restart. Wallet sign-in
> (`/api/auth/nonce` + `/api/auth/verify`), which produces the JWT the mint
> call needs, answers 503 the same way for the same reason.
>
> **Using a key never does.** Since 2026-09-21 the `rob_xxx` validator is
> registered unconditionally, so an existing key authenticates whether or not
> `API_SECRET` / `ADMIN_TOKEN` is set. Before that it was only mounted
> alongside `API_SECRET`, so on a default install a freshly minted key
> authenticated nothing.

### Operator service token (not an API key)

`API_AUTH_TOKEN` is the operator's own machine-to-machine credential. Send it
as `X-Service-Token`:

```
X-Service-Token: <API_AUTH_TOKEN>
```

It carries the role `service`: it is not billed per request, and it is **not an
admin** — `/api/admin/*` refuses it. Sending it as `X-API-KEY` still works for
one release and logs a deprecation warning.

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

No account needed — pay per request with USDC on Base or Ethereum. The server returns a `402 Payment Required` response with payment details; include the signed payment in the `X-PAYMENT` header on retry. x402 receiving is off by default (`X402_ENABLED`, see [../CONFIGURATION.md](../CONFIGURATION.md)). The paywalled routes are exactly `POST /a2a/rpc`, `POST /a2a/message/stream`, `POST /a2a/tasks` and `POST /v1/chat/completions` — free reads and continuations are never charged. See [payments.md](payments.md) for the money model.

---

## What is mounted

The app assembles itself from routers. Some are always present; some appear only
when their flag is on, in which case the whole path space is absent rather than
403 — a missing route is the honest answer to "this deployment does not do that".

| Prefix | What it is | Present when |
|---|---|---|
| `/health` | Liveness and component status (`503` when degraded) | always |
| `/api/task/*` | Sessions: create, status, message, cancel, files | always |
| `/api/auth/*` | SIWE sign-in, JWTs, API keys | always |
| `/api/chat/message` | One-shot chat over the task agent (`/api/message` is the legacy alias) | always |
| `/api/payments/*` | Deposit address, credit balance, transaction history | always (wallet sign-in only — an API-token identity is rejected) |
| `/api/x402/*` | Public pricing, payment status, and the per-invoice challenge/pay pair | always (info-only until `X402_ENABLED`) |
| `/api/pricing/*` | Public model pricing and a cost calculator | always |
| `/api/admin/*` | Tenant administration: users, credits, roles, blocks, billing failures | always (admin role required) |
| `/api/mcp/*` | Outbound MCP server management | always |
| `/api/skills/*` | Read, edit, delete and fork a skill | always |
| `/api/polymarket/*`, `/api/hyperliquid/*` | Venue configuration, read data, and gated execution | always (the venues' own flags gate live trading) |
| `/a2a/*`, `/.well-known/agent.json` | The A2A protocol surface | always |
| `/eip8004/*` | ERC-8004 identity, reputation and validation | always (discovery-only until `EIP8004_ENABLED`) |
| `/webhooks/{surface_id}` | Inbound webhook verify and delivery | always (404 with an empty surface registry) |
| `/api/kb/*` | Knowledge-base ingest and search | `KB_API_ENABLED` |
| `/v1/*` | OpenAI-compatible chat and model list | `OPENAI_COMPAT_API_ENABLED` |
| `/mcp` | POLYROB acting as an MCP server | `MCP_SERVE_ENABLED` |

`GET /docs` is the generated OpenAPI browser and is the authority on request and
response shapes for everything below.

> The console (`polyrob dashboard`) is a **separate** app on port 5050 with its own
> routes, including a public `GET /api/status`. See [console.md](console.md).

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
  "model": "<the model your config resolved>",
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

Injects a user guidance message into a running session. An optional `kind`
must be one of `answer`, `correction`, `feedback`, `guidance` (the default),
`question`, `steer`, `user_message`; anything else is **422**. The internal
forged-turn kinds (`self_wake`, `delegation_result`) are refused — they mark a
turn as machine-originated, which is what stops it auto-activating a skill or
reaching a high-impact verb.

### Cancel a session

```
POST /api/task/sessions/{session_id}/cancel
```

### Check message queue status

```
GET /api/task/sessions/{session_id}/queue-status
```

Returns the number of queued messages and the agent's current status.

### Files a session produced

```
GET  /api/task/sessions/{session_id}/documents            # list the workspace with metadata
GET  /api/task/sessions/{session_id}/workspace/{path}     # download ONE file
POST /api/task/sessions/{session_id}/workspace/upload     # multipart: file=@…
```

The upload is bounded by the same extension and MIME allow-lists the console
uses, and all three routes refuse a caller who does not own the session.

The download route is the URI A2A artifacts point at. It is confined to the
session workspace — an absolute path, a `..` segment or a symlink is refused
(400/403) — and always serves `Content-Disposition: attachment` with
`nosniff`, so an agent-authored file can never render as a page on the API's
own origin.

### Sessions of a user

```
GET  /api/task/users/{user_id}/sessions
POST /api/task/users/{user_id}/active_session
```

List a user's sessions, and switch which one is active. A caller may only read
their own.

### What this deployment can do

```
GET /api/task/capabilities   # models and tools this server actually offers
GET /api/task/metrics        # live resource usage — ADMIN ONLY
```

`capabilities` is the honest answer to "which model may I ask for" — it reflects
the keys and flags this process resolved, not a static list.

`metrics` requires an admin role: it reports `users.sessions_per_user` and
`users.top_users`, a roster of every tenant on the box with its session count.

### Streaming session events (SSE)

Real-time streaming is available via the **A2A layer** (`POST /a2a/message/stream`) or the Console's Socket.IO interface — not as a `/api/task` route. See the [A2A protocol](#a2a-protocol-agent-to-agent) section below.

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

`GET /a2a/agent-card` serves the same card at an API path. `GET /a2a/extended-card`
serves it to an authenticated caller and may add tier-specific metadata.

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

`GET /a2a/tasks/{task_id}/stream` attaches to a task that already exists, and
`POST /a2a/tasks/resubscribe` reattaches after a dropped connection — its
parameters go in the BODY (`{"id": "<taskId>", "historyLength": N}`), per the
spec's `TaskResubscriptionRequest`. There is no `tasks/resubscribe` JSON-RPC
method: it delivers an SSE stream, which a JSON-RPC POST cannot return, so
`/a2a/rpc` answers **501** naming this route.

SSE frames follow the A2A shapes: a status frame is
`{"taskId", "contextId", "kind": "status-update", "status", "final"}` and an
artifact frame is `{"taskId", "contextId", "kind": "artifact-update",
"artifact"}`. The SSE `event:` name is the frame's own `kind`.

### The REST task surface

The JSON-RPC methods have plain REST twins, for a client that would rather not
speak JSON-RPC:

```
POST   /a2a/tasks                        # create (payment-verified)
GET    /a2a/tasks                        # list, paginated
GET    /a2a/tasks/{task_id}              # status
POST   /a2a/tasks/{task_id}/send         # send a message to a running task
POST   /a2a/tasks/{task_id}/cancel       # cancel
```

### Push notifications

Instead of holding a stream open, register a callback and let the server call
you:

```
POST   /a2a/tasks/{task_id}/push-config
GET    /a2a/tasks/{task_id}/push-config
DELETE /a2a/tasks/{task_id}/push-config
```

---

## MCP server management (outbound)

Manage the MCP servers **this agent connects out to**, per tenant. Ten routes:

```
POST   /api/mcp/servers                     # add a server
GET    /api/mcp/servers                     # list yours
GET    /api/mcp/servers/{server_name}       # one server
PATCH  /api/mcp/servers/{server_name}       # update it
DELETE /api/mcp/servers/{server_name}       # remove it
POST   /api/mcp/servers/{server_name}/test  # test the connection
GET    /api/mcp/available                   # servers and the tools they expose
GET    /api/mcp/settings                    # your MCP settings
PATCH  /api/mcp/settings
GET    /api/mcp/audit                       # what was called, and by whom
```

Server secrets are stored encrypted; the read routes never return them.

---

## Knowledge base (`KB_API_ENABLED`, default off)

```
POST /api/kb/ingest          # {path, session_id, collection, recursive, globs}
POST /api/kb/ingest/upload   # multipart upload straight into a collection
POST /api/kb/search          # {query, collection, limit}
```

The tenant comes from the credential, never from the body, and `path` is
resolved inside the named session's workspace — a path that escapes it is
rejected before anything is read.

---

## Skills

```
GET    /api/skills/{skill_id}        # content and metadata
PUT    /api/skills/{skill_id}        # edit a user skill (system skills are immutable)
DELETE /api/skills/{skill_id}
POST   /api/skills/{skill_id}/fork   # copy a system skill so you can change it
```

See [skills.md](skills.md) for the format.

---

## Money and pricing

```
GET  /api/pricing/models                       # public model pricing
GET  /api/pricing/calculator                   # cost for a given usage (404 if the model is not priced)
GET  /api/x402/pricing                         # public: what this server charges per request
GET  /api/x402/requests/{request_id}           # public: the 402 challenge for an agent invoice
POST /api/x402/requests/{request_id}/pay       # public: settle that invoice via the facilitator
GET  /api/x402/verify-status/{nonce}           # payment status by nonce
GET  /api/x402/payment-history/{wallet}        # payments from one wallet
GET  /api/payments/deposit-address             # your credit-deposit address
GET  /api/payments/balance
GET  /api/payments/transactions
GET  /api/payments/deposits
GET  /api/payments/pricing
```

The `/api/x402/requests/*` pair is public by design — a payer must be able to
read a challenge and settle without an account — and is rate-limited by a client
key that cannot be spoofed with `X-Forwarded-For`. The full money model is in
[payments.md](payments.md).

---

## Trading venues

```
/api/polymarket/{configure,status,trading-limits,disable,enable,credentials,tools,execute,audit,stats}
/api/hyperliquid/{configure,status,trading-limits,demo-mode,enabled,credentials,tools,execute,audit,stats}
/api/hyperliquid/{markets/perpetual,markets/spot,price/{coin},orderbook/{coin},funding/{coin}}
/api/hyperliquid/{account,balances/spot,orders/open}
```

These routes configure credentials and read market data. **Mounting them does
not arm live trading** — an order is validated and returned as a dry run unless
the master and per-venue trade flags are on and the order is within its cap. See
[payments.md](payments.md).

---

## ERC-8004 (Trustless Agents)

Always mounted; discovery-only until `EIP8004_ENABLED`. The READ routes stay
open (and label themselves `simulated`), but the two that WRITE —
`POST /eip8004/reputation/feedback` and `POST /eip8004/validation/request` —
require an authenticated caller, and the two that SIGN or adjudicate
(`/reputation/authorize`, `/validation/respond`) require an admin role.

`trustMode` in `/eip8004/registration.json` is `onchain` only when there is
something a reader can look up: a confirmed on-chain record, or an operator
claim carrying BOTH an agent id and a registry address. Otherwise it is
`local`.

```
GET  /eip8004/registration.json                   # this agent's registration file
GET  /eip8004/config                              # what is configured, and what is not
POST /eip8004/reputation/{authorize,feedback,query}
GET  /eip8004/reputation/{agent_id}
POST /eip8004/validation/{request,respond}
GET  /eip8004/validation/{status/{hash},summary/{agent_id},pending,validators}
```

ERC-8004 is a trust and discovery layer, not a payment rail — it composes with
x402 rather than replacing it. See [payments.md](payments.md).

---

## Inbound webhooks

```
GET  /webhooks/{surface_id}   # the provider's verification handshake
POST /webhooks/{surface_id}   # deliver an event to that surface
```

Always mounted and harmless: with no webhook surface registered, both return
`404`.

---

## Administration

`/api/admin/*` (admin role required) covers user lookup and search, credit
add/deduct/read, role and tier changes, block and unblock, per-user audit and
sessions, instance statistics, and the billing-failure queue
(`GET /api/admin/billing-failures`, `…/summary`, `POST …/{id}/resolve`). Browse
the exact shapes at `/docs`.

---

## MCP server (inbound)

polyrob can also act as an MCP *server* itself, so an MCP client (Claude Desktop, Cursor) can connect to it as a tool provider. This is distinct from [MCP server management](#mcp-server-management-outbound) above, which is the *outbound* side — the agent connecting OUT to external MCP servers. It is **off by default** — enable with `MCP_SERVE_ENABLED=true` (see [../CONFIGURATION.md](../CONFIGURATION.md)).

```
POST /mcp   # JSON-RPC 2.0: initialize, tools/list, tools/call
```

Auth is `X-API-KEY` or `Authorization: Bearer <jwt>`. ⚠️ **x402 does not apply
here** — the per-request payment rail is gated on an explicit route list
(`/a2a/rpc`, `/a2a/message/stream`, `POST /a2a/tasks`, `/v1/chat/completions`)
and `/mcp` is not on it, so an anonymous caller gets 401, never a 402
challenge. v1 is read-only and exposes five tenant-scoped tools:

- `rob_usage_summary` — the caller's LLM usage (cost, credits, call count)
- `rob_goals_list` — the caller's autonomy goals
- `rob_goal_show` — one goal by id
- `rob_conversations` — the caller's recent correspondent conversations
- `rob_pending_approvals` — the caller's pending tool-approval requests

```bash
curl -X POST http://localhost:9000/mcp \
  -H "X-API-KEY: rob_xxx..." \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc": "2.0", "method": "tools/list", "id": 1}'
```

To connect Claude Desktop or Cursor, point their HTTP MCP client config at `http://localhost:9000/mcp` with an `X-API-KEY` header carrying your polyrob API key:

```json
{
  "mcpServers": {
    "polyrob": {
      "url": "http://localhost:9000/mcp",
      "headers": { "X-API-KEY": "rob_xxx..." }
    }
  }
}
```

(The exact field names in the client config vary by MCP client — the endpoint is a JSON-RPC-over-POST `/mcp`.)

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
that provider, else your default provider is used — an unrecognized string is served by the default
provider verbatim, never a 400). The slug form addresses ANY provider explicitly, including ones you
declared in `~/.polyrob/providers.yaml`: `"model": "ollama/qwen3-coder:30b"` or
`"model": "zai-coding/glm-5"`. `GET /v1/models` lists each available provider's default plus every
declared model. Point any OpenAI SDK at `http://localhost:9000/v1` with your polyrob API key.

> **Two different "API keys."** The `X-API-KEY: rob_xxx…` header is your **rob API key** — an
> inbound credential identifying the caller to POLYROB (minted at `/api/auth/api-keys`). It is NOT
> an LLM provider key (`OPENAI_API_KEY`, …), which is the outbound credential POLYROB spends to
> reach a model — see [configuration.md](configuration.md).

**⚠️ Single-turn.** Only the **last user message** in `messages` is executed.
polyrob keeps its own per-session history, keyed on `user` (else your
authenticated id), so replaying a client-side transcript would duplicate it.
`system` messages are **not** applied — the agent's system prompt comes from
its own identity, skills and tools. Send one new user message per call and let
the server hold the thread.

**Parameters.** `temperature` is threaded to the turn. `max_tokens`,
`max_completion_tokens`, `top_p`, `presence_penalty` and `frequency_penalty`
are accepted and **ignored** (output length is governed by the agent's own step
budget, and the sampling knobs are not forwarded). These are **refused with a
400** naming the field and the reason, rather than silently dropped:
`tools`, `tool_choice`, `functions`, `function_call`, `response_format`,
`stop`, `logprobs`, `top_logprobs`, `seed`, `logit_bias`, and `n` greater
than 1.

**`usage` is an estimate.** The response carries `usage.estimated: true`. The
counts are re-computed from the request text and the reply; they exclude the
system prompt, skills, memory recall, tool schemas and every intermediate agent
step, so treat them as a **floor**, not a bill. Authoritative per-call cost
lives in the instance's own usage records (`polyrob doctor`, the console's
Money destination).

**Errors use the OpenAI envelope.** Every `/v1` error — auth, validation,
internal, and a refusal from an auth gate that never reaches the route — is
returned as `{"error": {"message", "type", "code", "param"}}`, so an OpenAI SDK
reports the real reason instead of "unknown error".

The ONE exception is **402**: on a billed route it carries the x402 payment
*challenge*, which a paying client parses to build its payment, so that body is
passed through untouched.

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
