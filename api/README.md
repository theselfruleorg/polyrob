# API Package - HTTP API Layer

_Last reviewed: 2026-06-30. For the authoritative architecture see ../AGENTS.md; for env flags see ../docs/CONFIGURATION.md._

## Overview

The `api` package provides the HTTP API layer for the POLYROB platform, built on FastAPI. It exposes RESTful endpoints for task automation, chat interactions, authentication, payments, and administrative functions. The API supports JWT authentication, wallet-based auth (SIWE), and integrates with the platform's credit system.

## Architecture Philosophy

- **RESTful Design**: Standard REST patterns for resource management
- **Authentication**: Multiple auth methods (JWT, API keys, wallet)
- **Rate Limiting**: Built-in rate limiting and abuse protection
- **Modular Routers**: Organized by domain/feature
- **Async-First**: Full async/await support for performance
- **Error Handling**: Comprehensive error handling and reporting

## Package Structure

```
api/
├── __init__.py                  # Package initialization
├── README.md                    # This documentation
│
├── app.py                       # FastAPI application factory + router mounting (authoritative router list)
├── models.py                    # Pydantic request/response models
├── interfaces.py                # Type interfaces
│
├── middleware.py                # Authentication and rate limiting middleware
├── jwt_middleware.py            # JWT authentication middleware
├── auth_constants.py            # Authentication constants
│
├── auth_endpoints.py            # Wallet (SIWE) auth + API-key management
├── task_http_api.py             # Task/AutoV2 API endpoints
├── payment_endpoints.py         # Payment, balance, deposits, transactions
├── pricing_endpoints.py         # Pricing information endpoints
├── admin_endpoints.py           # Administrative endpoints
├── x402_endpoints.py            # x402 payment protocol endpoints
├── eip8004_endpoints.py         # ERC-8004 trustless-agents endpoints
├── mcp_routes.py                # MCP server management endpoints
│
├── kb/                          # Knowledge-base router (/api/kb, gated KB_API_ENABLED)
├── openai_compat/               # OpenAI-compatible /v1 surface (gated OPENAI_COMPAT_API_ENABLED)
├── a2a/                         # Agent-to-Agent protocol (agent_card, endpoints, streaming)
│
├── conversation_manager.py      # API conversation management
├── payment_verification.py      # Payment verification utilities
│
└── TASK_API_DOCS.md            # Task API documentation
```

## Application Factory (`app.py`)

### Creating the Application

```python
from api.app import create_app

app = create_app()
```

### Lifespan Management

The application uses FastAPI's lifespan context for proper startup/shutdown:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    config = BotConfig()
    bot = CoreBot(config=config)
    await bot.initialize()
    
    yield  # Server runs
    
    # Shutdown
    await bot.cleanup()
```

### Middleware Stack

Middleware runs in reverse order (LIFO - last added runs first):

1. **CORS Middleware**: Cross-origin resource sharing
2. **Rate Limiting Middleware**: Request throttling
3. **Authentication Middleware**: API key validation
4. **JWT Middleware**: Token-based authentication
5. **x402 Middleware**: Pay-per-request payment verification (runs first due to LIFO)

## Mounted Routers

`api/app.py` is the authoritative source for what is mounted (grep `include_router`). As of this
review the application mounts:

| Router | Prefix | Notes |
| --- | --- | --- |
| Task / AutoV2 (`task_http_api.py`) | `/api/task` | session lifecycle, messages, workspace, documents, metrics |
| Auth (`auth_endpoints.py`) | `/api/auth` | SIWE wallet auth (`/nonce`, `/verify`, `/me`) + API-key management (`/api-keys`) |
| Payments (`payment_endpoints.py`) | `/api/payments` | balance, deposit-address, transactions, deposits, pricing |
| x402 (`x402_endpoints.py`) | `/api/x402` | pay-per-request info + payment lifecycle |
| OpenAI-compat (`openai_compat/router.py`) | `/v1` | `POST /v1/chat/completions`, `GET /v1/models` — gated by `OPENAI_COMPAT_API_ENABLED` (default OFF) |
| KB (`kb/endpoints.py`) | `/api/kb` | knowledge-base ingest/search — gated by `KB_API_ENABLED` (default OFF) |
| Admin (`admin_endpoints.py`) | `/api` | admin/user management |
| MCP (`mcp_routes.py`) | `/api/mcp` | MCP server management |
| Polymarket | `/api/polymarket` | market data |
| Hyperliquid | `/api/hyperliquid` | market data |
| Skills (`skill_endpoints.py`) | `/api/skills` | skill management |
| Pricing (`pricing_endpoints.py`) | `/api/pricing` | model pricing |
| A2A discovery / endpoints / streaming (`api/a2a/*`) | various | Agent-to-Agent protocol (`/.well-known/agent.json`, `/a2a/agent-card`, `/a2a/extended-card`, `/a2a/*`) |
| EIP-8004 (`eip8004_endpoints.py`) | `/eip8004` | trustless-agents (registration, reputation, validation, `/eip8004/config`) |

Several routers are conditionally mounted based on config/feature flags — `api/app.py` is the
single source of truth; the endpoint samples below are illustrative, not exhaustive.

## API Endpoints

### Health Check

```http
GET /health
```

Returns system health status and metrics:
```json
{
  "status": "healthy",
  "service": "rob-platform",
  "metrics": {
    "active_updates": 5,
    "semaphore_available": 45,
    "bot_initialized": true
  }
}
```

### Authentication (`/api/auth/*`)

#### Request Nonce / SIWE Message
```http
POST /api/auth/nonce
Content-Type: application/json

{
  "wallet_address": "0x1234...",
  "chain_id": 1
}
```

Response (`NonceResponse`):
```json
{
  "message": "your-polyrob-host.example wants you to sign in...",
  "nonce": "...",
  "issued_at": "2026-06-30T12:00:00Z",
  "expiration": "2026-06-30T12:05:00Z"
}
```

#### Verify Signature
```http
POST /api/auth/verify
Content-Type: application/json

{
  "wallet_address": "0x1234...",
  "message": "...",
  "signature": "0x...",
  "nonce": "...",
  "chain": "ethereum"
}
```

Response is **flat** (also sets an `auth_token` HTTP-only cookie):
```json
{
  "token": "eyJ...",
  "user_id": "user_123",
  "wallet_address": "0x1234...",
  "role": "user",
  "tier": "free",
  "is_admin": false,
  "expires_at": "2026-07-07T12:00:00Z"
}
```

#### Get User Profile
```http
GET /api/auth/me
Authorization: Bearer <token>
```

#### API Key Management
Self-service API keys for programmatic access (requires a wallet-authenticated JWT).
The full key is returned only once on creation.
```http
POST   /api/auth/api-keys          # body: {"name": "...", "expires_days": 90}
GET    /api/auth/api-keys          # lists key prefixes (never full keys)
DELETE /api/auth/api-keys/{key_prefix}
```

### Task API (`/api/task/*`)

See `TASK_API_DOCS.md` for detailed documentation.

#### Create Session
```http
POST /api/task/sessions
Content-Type: application/json

{
  "task": "Your task description here",
  "user_id": "user_identifier",
  "model": "gpt-5",
  "provider": "openai",
  "max_steps": 50,
  "tools": ["browser", "filesystem"]
}
```

Response:
```json
{
  "ok": true,
  "session_id": "uuid",
  "task": "Your task...",
  "state": "running",
  "webview_url": "https://your-domain.example/session/uuid"
}
```

#### Cancel Session
```http
POST /api/task/sessions/{session_id}/cancel
```

(There are no pause/resume routes.)

#### Send User Message
```http
POST /api/task/sessions/{session_id}/messages
Content-Type: application/json

{
  "text": "Your guidance message",
  "kind": "guidance"
}
```

#### Get Session Info
```http
GET /api/task/sessions/{session_id}
```

#### Get Session Queue Status
```http
GET /api/task/sessions/{session_id}/queue-status
```

#### List User Sessions
```http
GET /api/task/users/{user_id}/sessions
```

#### Set User's Active Session
```http
POST /api/task/users/{user_id}/active_session
```

#### Server Metrics / Capabilities
```http
GET /api/task/metrics
GET /api/task/capabilities
```

#### Upload to Session Workspace / List Documents
```http
POST /api/task/sessions/{session_id}/workspace/upload
GET  /api/task/sessions/{session_id}/documents
```

See `TASK_API_DOCS.md` for the full, authoritative path list (`task_http_api.py` mounts under the
`/task` prefix, so the app-level paths are `/api/task/...`).

### OpenAI-Compatible API (`/v1/*`)

Gated by `OPENAI_COMPAT_API_ENABLED`. Provides a non-streaming OpenAI-compatible surface over the
Task agent's `chat_once`:

```http
POST /v1/chat/completions
GET  /v1/models
```

See `openai_compat/router.py`.

### Knowledge Base API (`/api/kb/*`)

Gated by `KB_API_ENABLED` (default OFF). `user_id` is always derived from the authenticated
request (never the body); ingest paths are confined to the tenant's session workspace.

```http
POST /api/kb/ingest          # body: {path, session_id, collection, recursive, globs}
POST /api/kb/ingest/upload   # multipart file upload
POST /api/kb/search          # body: {query, collection, limit}
```

See `kb/endpoints.py`.

### Payment API (`/api/payments/*`)

#### Get Credit Balance
```http
GET /api/payments/balance
Authorization: Bearer <token>
```

Response (`CreditBalanceResponse`):
```json
{
  "user_id": "user_123",
  "balance": 1000,
  "lifetime_earned": 1500,
  "lifetime_spent": 500,
  "tier": "free"
}
```

#### Get Deposit Address
```http
GET /api/payments/deposit-address
Authorization: Bearer <token>
```

Response (`DepositAddressResponse`):
```json
{
  "user_id": "user_123",
  "deposit_address": "0xabc...",
  "chains": ["ethereum", "sepolia"],
  "qr_code_url": "https://chart.googleapis.com/...",
  "instructions": "Send USDC, USDT, or ETH..."
}
```

#### Get Transaction / Deposit History
```http
GET /api/payments/transactions?limit=100&offset=0&paginated=false
GET /api/payments/deposits?limit=100&offset=0
Authorization: Bearer <token>
```

#### Get Credit Pricing (public)
```http
GET /api/payments/pricing
```

### Pricing API (`/api/pricing/*`)

#### Get Token Pricing
```http
GET /api/pricing/models
```

Response:
```json
{
  "models": {
    "gpt-5": {
      "input_cost_per_1k": 0.005,
      "output_cost_per_1k": 0.02
    },
    "claude-sonnet-4-5": {
      "input_cost_per_1k": 0.003,
      "output_cost_per_1k": 0.015
    }
  }
}
```

### Admin API (`/api/admin/*`)

Requires admin authentication.

#### Generate API Key
```http
POST /api/admin/generate-key
X-Admin-Token: <admin_token>
```

#### User Management
```http
GET /api/admin/users
GET /api/admin/users/{user_id}
PUT /api/admin/users/{user_id}/tier
```

#### System Stats
```http
GET /api/admin/stats
```

### x402 Payment Protocol

The x402 protocol enables pay-per-request API access using USDC stablecoins. Uses the [fastapi-x402](https://github.com/jordo1138/fastapi-x402) library with Coinbase facilitator for on-chain verification.

See [modules/x402/README.md](../modules/x402/README.md) for detailed documentation.

#### Payment Flow

1. **Request without auth** → Server returns 402 with payment details
2. **Client signs payment** with wallet → Sends `X-PAYMENT` header (base64-encoded)
3. **Server verifies via Coinbase** → On-chain USDC settlement
4. **Server returns response** with requested data

#### x402 Endpoints (`/api/x402/*`)

```http
GET  /api/x402/pricing                          # public pricing/flow info
GET  /api/x402/verify-status/{nonce}             # poll a payment's status by nonce
GET  /api/x402/payment-history/{wallet_address}  # payment history for a wallet
```

`GET /api/x402/pricing` response:
```json
{
  "payment_method": "x402",
  "description": "Pay-per-request with cryptocurrency. No account required.",
  "pricing": {
    "per_request_usd": 0.01,
    "minimum_purchase_usd": 0,
    "supported_assets": ["usdc", "usdt", "eth"],
    "supported_chains": ["base", "ethereum"]
  },
  "payment_address": "0x...",
  "facilitator": "Direct payment"
}
```

### A2A Protocol (`/a2a/*`, `/.well-known/*`)

Google's Agent-to-Agent protocol. See `api/a2a/*` and `../AGENTS.md`.

```http
GET  /.well-known/agent.json      # Agent Card discovery (RFC 8615)
GET  /a2a/agent-card              # Agent Card (alias)
GET  /a2a/extended-card          # extended Agent Card
POST /a2a/rpc                    # JSON-RPC task operations
POST /a2a/tasks                  # REST task create (+ /a2a/tasks/{id}, /send, /cancel, push-config)
POST /a2a/message/stream         # SSE streaming
GET  /a2a/tasks/{task_id}/stream # SSE streaming for a task
POST /a2a/tasks/resubscribe      # resubscribe to a task stream
```

### EIP-8004 Trustless Agents (`/eip8004/*`)

```http
GET  /eip8004/config              # config/discovery
GET  /eip8004/registration.json   # registration descriptor
POST /eip8004/reputation/*        # reputation feedback/query (gated EIP8004_ENABLED)
POST /eip8004/validation/*        # validation request/response (gated EIP8004_ENABLED)
GET  /eip8004/validation/*        # validation status/summary/pending/validators
```

## Authentication

### JWT Authentication

Most endpoints require JWT authentication:

```http
Authorization: Bearer <jwt_token>
```

JWT tokens contain:
- `sub`: User ID
- `wallet`: Wallet address (if wallet auth)
- `tier`: Subscription tier
- `role`: User role
- `exp`: Expiration timestamp

### API Key Authentication

Alternative authentication via API key:

```http
X-API-KEY: <api_key>
```

### Wallet Authentication (SIWE)

Sign-In with Ethereum flow:
1. Request nonce/message from `/api/auth/nonce` (body: `wallet_address`, `chain_id`)
2. Sign message with wallet
3. Submit signature to `/api/auth/verify`
4. Receive JWT token (flat response + `auth_token` cookie)

### x402 Payment Authentication

Pay-per-request authentication using x402 protocol:
```http
X-PAYMENT: <base64-encoded payment payload>
```

The payload contains EIP-712 signed authorization for USDC transfer. See [modules/x402/README.md](../modules/x402/README.md) for details.

## Request/Response Models (`models.py`)

### MessageRequest
```python
class MessageRequest(BaseModel):
    text: str
    user_id: Optional[str] = None
    chat_id: Optional[str] = None
    message_id: Optional[str] = None
    platform: Optional[str] = "api"
    chat_type: Optional[str] = "private"
    metadata: Optional[Dict[str, Any]] = {}
    attachments: Optional[List[Dict[str, Any]]] = []
    reply_to: Optional[str] = None
    session_id: Optional[str] = None
```

### MessageResponse
```python
class MessageResponse(BaseModel):
    success: bool = True
    text: Optional[str] = None
    format: Optional[str] = "markdown"
    message: Optional[str] = None
    message_id: Optional[str] = None
    conversation_id: Optional[str] = None
    data: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = {}
    suggestions: Optional[List[str]] = []
    attachments: Optional[List[Dict[str, Any]]] = []
    session_id: Optional[str] = None
    agent_id: Optional[str] = None
```

### SessionCreateRequest
```python
class SessionCreateRequest(BaseModel):
    user_id: str                                  # required
    task: str                                     # required
    model: Optional[str] = "gpt-5"
    provider: Optional[str] = "openai"
    tools: Optional[List[str]] = []               # default: empty list
    max_steps: Optional[int] = 50
    temperature: Optional[float] = 0.0
    use_vision: Optional[bool] = True
    session_config: Optional[Dict[str, Any]] = None
```

## Middleware

### Rate Limiting (`middleware.py`)

```python
class RateLimitMiddleware:
    def __init__(
        self,
        requests_per_minute: int = 60,
        requests_per_hour: int = 1000,
        burst_size: int = 10
    )
```

### JWT Middleware (`jwt_middleware.py`)

```python
class JWTAuthMiddleware:
    def __init__(self, jwt_secret: str):
        self.jwt_secret = jwt_secret
```

Adds to request state:
- `user_id`: Authenticated user ID
- `wallet_address`: Wallet address (if applicable)
- `tier`: User's subscription tier
- `role`: User's role
- `is_admin`: Admin flag
- `authenticated`: Authentication status

## Configuration

### Environment Variables

```bash
# API Configuration
API_HOST=0.0.0.0
API_PORT=9000
CORS_ALLOW_ORIGINS=http://localhost:3000,https://your-domain.example

# Authentication
JWT_SECRET_KEY=your-secret-key
API_AUTH_TOKEN=your-api-token
ADMIN_TOKEN=admin-secret

# Rate Limiting
API_RATE_LIMIT_RPM=60
API_RATE_LIMIT_RPH=1000
API_RATE_LIMIT_BURST=10

# Concurrency
MAX_CONCURRENT_UPDATES=50
```

## Error Handling

### Standard Error Response
```json
{
  "error": "Error message",
  "error_id": "error_123456789",
  "message": "User-friendly message",
  "details": {}
}
```

### HTTP Status Codes
- `200`: Success
- `201`: Created
- `400`: Bad Request
- `401`: Unauthorized
- `402`: Payment Required (x402)
- `403`: Forbidden
- `404`: Not Found
- `422`: Validation Error
- `429`: Rate Limited
- `500`: Internal Server Error
- `503`: Service Unavailable

## Running the API

### Development
```bash
python -m api.app
# or
uvicorn api.app:get_app --host 0.0.0.0 --port 9000 --reload --factory
```

### Production
```bash
uvicorn api.app:get_app --host 0.0.0.0 --port 9000 --workers 4 --factory
```

## Integration with Core Platform

The API integrates with all platform components:

```python
# Access via app_state
bot = app_state["bot"]
container = app_state["container"]

# Get services
task_agent = container.get_service("task_agent")
database = container.get_service("database_manager")
```

## Best Practices

### API Development
1. **Use Pydantic Models**: Always define request/response models
2. **Authentication**: Check auth on all protected endpoints
3. **Rate Limiting**: Respect rate limits for external calls
4. **Error Handling**: Return appropriate status codes and messages
5. **Logging**: Log all requests and errors

### Security
1. **Validate Input**: Always validate and sanitize input
2. **Check Permissions**: Verify user permissions for actions
3. **Secure Secrets**: Never expose secrets in responses
4. **Rate Limit**: Protect against abuse
5. **Audit Trail**: Log security-relevant events

## OpenAPI Documentation

FastAPI auto-generates OpenAPI documentation:
- Swagger UI: `http://localhost:9000/docs`
- ReDoc: `http://localhost:9000/redoc`
- OpenAPI JSON: `http://localhost:9000/openapi.json`

