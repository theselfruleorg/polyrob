# x402 Payment Protocol Module

> ⚠️ **Unaudited — use at your own risk.** This module has had **no independent security audit**.
> It handles real value on mainnet and is provided as-is with no warranty (see the project
> [LICENSE](../../LICENSE)); bugs may lead to loss of funds. It is **OFF by default**. Prefer
> testnets while evaluating. See [SECURITY.md](../../SECURITY.md#crypto--wallet--payment-features).

## Overview

The x402 module implements the [x402 payment protocol](https://x402.org) for pay-per-request API access. It enables AI agents and users to pay for API calls using USDC stablecoins on supported blockchains (Base, Avalanche, etc.).

This implementation uses the **[fastapi-x402](https://github.com/jordo1138/fastapi-x402)** library which handles payment verification and settlement via the official Coinbase facilitator.

## How It Works

```
┌─────────────┐     1. Request (no auth)      ┌─────────────┐
│   Client    │ ─────────────────────────────>│  POLYROB API    │
│  (AI Agent) │                               │             │
│             │ <───────────────────────────  │             │
│             │     2. 402 Payment Required   │             │
│             │        + payment details      │             │
│             │                               │             │
│             │     3. Request + X-PAYMENT    │             │
│             │ ─────────────────────────────>│             │
│             │                               │  ┌───────┐  │
│             │     4. Verify via Coinbase ──────>│ CDP │  │
│             │                               │  └───────┘  │
│             │ <───────────────────────────  │             │
└─────────────┘     5. Response + data        └─────────────┘
```

### Payment Flow

1. **Client sends request** without authentication
2. **Server returns 402** with payment requirements (amount, recipient, network)
3. **Client signs payment** with their wallet and sends `X-PAYMENT` header
4. **Server verifies payment** via Coinbase Developer Platform (CDP) facilitator
5. **CDP settles on-chain** (actual USDC transfer happens)
6. **Server returns response** with requested data

## Architecture

```
modules/x402/
├── __init__.py           # Package exports
├── README.md             # This file
├── middleware.py         # FastAPI middleware (wraps fastapi-x402)
└── x402_integration.py   # POLYROB-specific user integration
```

### Components

**X402PaymentMiddleware** (`middleware.py`)
- Intercepts requests with `X-PAYMENT` header
- Uses `fastapi-x402` for verification/settlement
- Creates POLYROB user profiles for x402 payers
- Sets request state for downstream handlers

**Integration Layer** (`x402_integration.py`)
- `generate_user_id_from_wallet()` - Creates consistent user IDs from wallet addresses
- `ensure_user_profile_for_payer()` - Creates user_profiles record for new x402 payers
- `record_x402_payment()` - Records payments in database
- `get_x402_config()` - Reads configuration from environment

## Configuration

### Environment Variables

```bash
# Enable/disable x402
X402_ENABLED=true

# Treasury wallet address (receives payments)
X402_PAYMENT_RECIPIENT=0xYourTreasuryAddress

# Blockchain network
X402_DEFAULT_CHAIN=base  # Options: base, base-sepolia, avalanche, iotex

# Coinbase Developer Platform credentials (REQUIRED for mainnet)
CDP_API_KEY_ID=your_cdp_key_id
CDP_API_KEY_SECRET=your_cdp_key_secret
```

### Network Options

| Network | Chain ID | Testnet | CDP Required |
|---------|----------|---------|--------------|
| `base` | 8453 | No | Yes |
| `base-sepolia` | 84532 | Yes | No |
| `avalanche` | 43114 | No | Yes |
| `avalanche-fuji` | 43113 | Yes | No |
| `iotex` | 4689 | No | Yes |

### Getting CDP Credentials

For **mainnet** payments (real USDC), you need Coinbase Developer Platform credentials:

1. Go to https://portal.cdp.coinbase.com/
2. Create an account/login
3. Create a new project
4. Generate API keys (Key ID + Secret)
5. Add to your `.env.production`:
   ```
   CDP_API_KEY_ID=your_key_id
   CDP_API_KEY_SECRET=your_key_secret
   ```

For **testnet** (base-sepolia), no CDP credentials are needed - the free `x402.org` facilitator is used automatically.

## User Management

When a user pays via x402, the system:

1. **Generates user ID** from wallet address: `usr_{sha256(wallet)[:12]}`
2. **Creates user_profiles record** with:
   - `user_id`: Generated ID
   - `wallet_address`: Payer's wallet
   - `tier`: `x402`
   - `role`: `user`
3. **Creates user_credits record** with 0 balance (x402 users pay per request)
4. **Records payment** in `x402_payment_requests` table

### User Tiers

| Tier | Description | Credit Deduction |
|------|-------------|------------------|
| `free` | Default tier | Yes |
| `premium` | Paid subscription | Yes |
| `x402` | Pay-per-request | No (paid via x402) |
| `admin` | Admin users | No (bypassed) |

## API Integration

### Protected Endpoints

x402 middleware automatically protects endpoints. The flow:

```python
# In api/app.py
app.add_middleware(X402PaymentMiddleware, enabled=True)
```

### Public Endpoints

These endpoints are excluded from x402 payment:
- `/` - Root
- `/health` - Health check
- `/docs`, `/openapi.json` - API docs
- `/api/x402/*` - x402 info endpoints
- `/.well-known/agent.json` - A2A agent card
- `/a2a/agent-card` - A2A agent card

### Request State

After successful x402 payment, middleware sets:

```python
request.state.payment_method = "x402"
request.state.payer_address = "0x..."  # Wallet address
request.state.user_id = "usr_abc123..."  # Generated user ID
request.state.tier = "x402"
request.state.authenticated = True
```

## Database Schema

### x402_payment_requests

```sql
CREATE TABLE x402_payment_requests (
    id TEXT PRIMARY KEY,
    amount TEXT,
    amount_usd REAL,
    asset TEXT DEFAULT 'usdc',
    chain TEXT DEFAULT 'base',
    recipient TEXT,
    nonce TEXT,
    deadline INTEGER,
    status TEXT DEFAULT 'pending',
    payer_address TEXT,
    transaction_hash TEXT,
    payment_id TEXT,
    metadata TEXT,
    created_at TIMESTAMP,
    completed_at TIMESTAMP,
    updated_at TIMESTAMP
);
```

## Testing

### Testnet Testing

1. Set `X402_DEFAULT_CHAIN=base-sepolia` in your `.env`
2. Get testnet USDC from a faucet
3. No CDP credentials needed

### Local Testing

```bash
# Test health (should work without payment)
curl https://your-domain.example/health

# Test protected endpoint (should return 402)
curl https://your-domain.example/a2a/rpc \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"message/send","id":1,"params":{}}'
```

## Troubleshooting

### "CDP credentials required"

For mainnet networks, you must provide CDP credentials:
```bash
CDP_API_KEY_ID=your_key_id
CDP_API_KEY_SECRET=your_key_secret
```

### "Payment verification failed"

- Check the `X-PAYMENT` header format (base64-encoded JSON)
- Ensure sufficient USDC balance in payer wallet
- Verify the payment amount matches requirements
- Check network matches (e.g., Base mainnet vs testnet)

### "x402 not enabled"

Ensure `X402_ENABLED=true` in your environment.

## Dependencies

```
fastapi-x402>=0.1.8
```

This package includes:
- `cdp-sdk` - Coinbase Developer Platform SDK
- `web3` - Ethereum interaction
- `httpx` - Async HTTP client

## Resources

- [x402 Protocol Spec](https://github.com/coinbase/x402/blob/main/specs/x402-specification.md)
- [x402.org](https://x402.org)
- [fastapi-x402 GitHub](https://github.com/jordo1138/fastapi-x402)
- [Coinbase CDP Portal](https://portal.cdp.coinbase.com/)
- [Coinbase x402 Docs](https://docs.cdp.coinbase.com/x402/welcome)
