# Modules Package - Core Functional Systems

_Last reviewed: 2026-06-30. For the authoritative architecture see ../AGENTS.md; for env flags see ../docs/CONFIGURATION.md._

## Overview

The `modules` package provides the core functional systems that power the POLYROB platform. It implements a modular architecture with these primary subsystems: **Database Management**, **Language Model Integration**, **Memory Management**, **Authentication**, **Credits & Usage Tracking**, **Payments**, **x402 Protocol**, and **ERC-8004 Trustless Agents**.

## Architecture Philosophy

- **Modular Design**: Clean separation of functional concerns
- **Dependency Management**: Explicit dependency declaration and validation
- **Provider Abstraction**: Support for multiple service providers
- **Async-First**: All operations are asynchronous for optimal performance
- **Resource Management**: Proper initialization and cleanup lifecycle
- **Extensibility**: Easy to add new providers and capabilities

## Package Structure

```
modules/
├── __init__.py                 # Package exports and metadata
├── README.md                   # This documentation
├── base_module.py              # Abstract base class for all modules
│
├── database/                   # Database management subsystem
│   ├── __init__.py
│   ├── database_manager.py     # Main database orchestrator
│   ├── connection.py           # Database connection management
│   ├── connection_pool.py      # Connection pooling
│   ├── schema.sql              # Database schema definitions
│   ├── user_profiles.py        # User profile data access
│   ├── conversation_contexts.py # Conversation data access
│   ├── auth_tables.py          # Authentication tables
│   ├── x402_tables.py          # x402 payment tables
│   ├── audit_log.py            # Audit trail for security-sensitive ops (role/credit/admin/auth)
│   ├── hyperliquid.py          # Hyperliquid credential store + trading audit/stats
│   ├── polymarket.py           # Polymarket encrypted credential store + trading audit
│   ├── user_mcp_servers.py     # Per-user MCP server configs/settings/audit
│   ├── migrate.py              # Database migrations
│   └── utils.py                # Database utilities
│
├── llm/                        # Language model integration
│   ├── __init__.py
│   ├── llm_manager.py          # LLM orchestration and fallback
│   ├── llm_client.py           # Base LLM client interface
│   ├── llm_client_registry.py  # Provider registry and factory
│   ├── model_registry.py       # Model + pricing registry (SINGLE SOURCE OF TRUTH)
│   ├── profiles.py             # Provider profiles (base_url, ordering)
│   ├── adapters.py             # Native chat-model adapters (BaseChatModel)
│   ├── llm_factory.py          # Native chat-model factory
│   ├── messages.py             # Native message types (System/Human/AI/Tool, no third-party framework)
│   ├── token_counter.py        # Token counting utilities
│   ├── cache_hints.py          # Per-provider prompt-cache strategy
│   ├── brain_scrubber.py       # Strips leaked brain-state JSON from the user-facing stream
│   ├── think_scrubber.py       # Strips leaked <think>/<reasoning> blocks at the content seam
│   ├── anthropic_client.py     # Anthropic Claude integration
│   ├── openai_client.py        # OpenAI GPT integration
│   ├── gemini_client.py        # Google Gemini integration
│   ├── deepseek_client.py      # DeepSeek integration
│   ├── openrouter_client.py    # OpenRouter (Grok/Kimi/Qwen/etc., OpenAI-compatible)
│   └── nvidia_client.py        # NVIDIA NIM (subclasses OpenRouterClient)
│   # (Llama provider removed — no llama_client.py)
│
├── memory/                     # Memory management subsystem
│   ├── __init__.py
│   ├── memory_manager.py       # Memory orchestration + conversation context
│   ├── cache_manager.py        # Caching system
│   ├── user_profile_manager.py # User profile memory
│   ├── models.py               # Data models and schemas
│   ├── provider.py             # MemoryProvider ABC + NullMemoryProvider
│   ├── registry.py             # MemoryProviderRegistry (one-external-provider seam)
│   ├── backend_factory.py      # Selects the memory backend (MEMORY_BACKEND)
│   ├── sqlite_memory_provider.py        # Local SQLite FTS5 keyword recall (default)
│   ├── local_vector_memory_provider.py  # Optional local vector recall (sqlite-vec)
│   └── task/                   # Task-specific memory
│       ├── task_context_manager.py  # Task-agent context (H-MEM) management
│       ├── null_context_manager.py  # No-op context manager for sub-agents
│       ├── compaction_manager.py
│       ├── context_retriever.py
│       ├── hierarchical_memory.py
│       ├── phase_manager.py
│       ├── semantic_retriever.py    # Embedder-based cross-phase recall
│       ├── lexical_retriever.py     # No-embedder (term-frequency) cross-phase recall
│       ├── reflection_service.py    # Aux-model phase consolidation (REFLECTION_LLM_ENABLED)
│       └── threat_scan.py           # Prompt-injection scan on memory writes (MEMORY_THREAT_SCAN)
│
├── auth/                       # Authentication subsystem
│   ├── __init__.py
│   ├── siwe_auth.py            # Sign-In with Ethereum
│   ├── identity_mapper.py      # User identity mapping
│   ├── tier_manager.py         # Subscription tier management
│   └── api_key_manager.py      # API key management
│
├── eip8004/                    # ERC-8004 Trustless Agents (on-chain identity/reputation/validation)
│   ├── __init__.py
│   ├── README.md               # ERC-8004 documentation
│   ├── contracts.py            # Smart-contract interfaces/ABIs
│   ├── models.py               # Data models
│   ├── registration.py         # Agent identity registration
│   ├── reputation.py           # On-chain reputation/feedback
│   └── validation.py           # Pluggable validation (reputation/crypto-economic/TEE)
│
├── credits/                    # Credits and usage tracking
│   ├── __init__.py
│   ├── balance_manager.py      # Credit balance management
│   ├── usage_tracker.py        # LLM usage tracking (PRIMARY)
│   ├── usage_meter.py          # Legacy usage metering (DEPRECATED)
│   ├── pricing.py              # Pricing configuration
│   └── cost_utils.py           # Cost calculation utilities
│
├── payments/                   # Payment system
│   ├── __init__.py
│   ├── wallet_generator.py     # Deposit wallet generation
│   ├── deposit_monitor.py      # Deposit monitoring
│   └── treasury_sweeper.py     # Treasury management
│
└── x402/                       # x402 Payment Protocol (via fastapi-x402)
    ├── __init__.py
    ├── README.md               # x402 documentation
    ├── middleware.py           # x402 middleware (wraps fastapi-x402)
    └── x402_integration.py     # POLYROB user integration utilities
```

## Core Module Systems

### 1. Database Management (`database/`)

Comprehensive data persistence layer with SQLite/PostgreSQL support.

#### DatabaseManager (`database_manager.py`)

**Features**:
- Multi-Database Support: SQLite for development, PostgreSQL for production
- Automatic Migrations: Schema versioning and automatic upgrades
- Transaction Management: ACID-compliant operations
- Connection Pooling: Efficient connection management

**Core Tables**:
- `user_profiles` - User account and wallet data
- `conversation_contexts` - Chat history and context
- `user_credits` - Credit balance management
- `credit_transactions` - Transaction history
- `auth_sessions` - Authentication sessions
- `x402_payments` - x402 payment records

**Usage**:
```python
db = container.get_service('database_manager')
user_profile = await db.user_profiles.get_by_user_id(user_id)
context = await db.conversation_contexts.get_context(user_id, chat_id)
```

### 2. Language Model Integration (`llm/`)

Multi-provider LLM integration with intelligent fallback and unified interface.

#### LLMManager (`llm_manager.py`)

**Supported providers**: OpenAI, Anthropic, Google Gemini, DeepSeek, OpenRouter
(Grok/Kimi/Qwen/GLM/etc.), and NVIDIA NIM.

> The **authoritative, live model set and pricing live in
> [`modules/llm/model_registry.py`](llm/model_registry.py)** — it is the single source of
> truth and is updated as models ship. This README intentionally does **not** enumerate model
> IDs (they go stale quickly). OpenRouter and NVIDIA NIM are OpenAI-wire-compatible
> (`nvidia_client.py` subclasses `openrouter_client.py`). The Llama provider has been removed.

**Features**:
- Intelligent Fallback: Automatic failover between providers
- Health Monitoring: Real-time provider availability checking
- Native LLM Layer: POLYROB's own adapters/message types (no third-party agent framework)
- Token Counting: Accurate token usage tracking

**Usage**:
```python
llm_manager = container.get_service('llm_manager')
client = await llm_manager.get_primary_client()
response = await client.generate_response([
    {"role": "user", "content": "Hello!"}
])
```

#### Token Counter (`llm/token_counter.py`)

Utilities for counting tokens across different models and providers.

### 3. Memory Management (`memory/`)

Memory system managing conversation context, user profiles, and cross-session recall.

> **Vector-DB rework (2026):** Pinecone, ChromaDB, the `database/vector/` layer, the
> `memory/knowledge/` (RAG) layer, `text_processor.py`, and `retrieval.py` have all been retired.
> Recall is now provided by a **local SQLite** backend selected via `MEMORY_BACKEND` (default
> `sqlite`):
> - `sqlite_memory_provider.py` — SQLite FTS5 keyword recall (tenant-scoped, default).
> - `local_vector_memory_provider.py` — optional local vector recall (sqlite-vec) for hybrid
>   keyword+vector search, kept inside the same `memory.db` (no external vector service).
>
> These plug in behind the `MemoryProvider` seam (`provider.py` / `registry.py` /
> `backend_factory.py`). See ../AGENTS.md (Memory System) for the full provider story.

#### MemoryManager (`memory_manager.py`)

**Subsystems**:
- **Conversation context**: history and context preservation (handled in `memory_manager.py`;
  task-agent context lives in `memory/task/task_context_manager.py`)
- **CacheManager**: High-performance in-memory caching
- **UserProfileManager**: User preference and behavior tracking
- **MemoryProvider backends**: local SQLite FTS / optional local vector (see note above)

#### Task Memory (`memory/task/`)

Specialized memory components for task agents:
- **TaskContextManager**: Task-agent context / H-MEM management (`null_context_manager.py`
  is the no-op variant used by sub-agents)
- **HierarchicalMemory**: Multi-level memory organization
- **CompactionManager**: Memory compaction for long sessions
- **SemanticRetriever** / **LexicalRetriever**: cross-phase recall — embedder-based and
  no-embedder (term-frequency) variants respectively
- **ReflectionService**: aux-model phase consolidation (`REFLECTION_LLM_ENABLED`)
- **PhaseManager**: Task phase tracking
- **threat_scan**: prompt-injection scan on memory writes (`MEMORY_THREAT_SCAN`)

### 4. Authentication (`auth/`)

Wallet-based authentication system with SIWE (Sign-In with Ethereum) support.

#### Components

**SIWEAuthenticator** (`siwe_auth.py`):
- Sign-In with Ethereum implementation
- Message generation and verification
- Nonce management

**IdentityMapper** (`identity_mapper.py`):
- Maps wallet addresses to user identities
- Handles multiple authentication methods

**TierManager** (`tier_manager.py`):
- Subscription tier management
- Access level control
- Feature gating

**APIKeyManager** (`api_key_manager.py`):
- API key generation and validation
- Key rotation support

**Usage**:
```python
# Authenticate with wallet
siwe = SIWEAuthenticator(config)
message = await siwe.generate_message(wallet_address)
verified = await siwe.verify_signature(message, signature)

# Check user tier
tier_manager = TierManager(config)
tier = await tier_manager.get_user_tier(user_id)
```

### 5. Credits System (`credits/`)

Comprehensive credit-based usage tracking and billing.

#### CreditBalanceManager (`balance_manager.py`)

**Features**:
- Real-time balance tracking
- Credit addition and deduction
- Transaction history
- Balance alerts

#### LLMUsageTracker (`usage_tracker.py`) - PRIMARY

**Features**:
- Token-level usage tracking
- Cost calculation per request
- Provider-specific pricing
- Usage analytics

```python
tracker = LLMUsageTracker(config)
await tracker.record_usage(UsageRecord(
    user_id=user_id,
    model="gpt-5",
    input_tokens=150,
    output_tokens=200,
    cost=0.005
))
```

#### Pricing (`pricing.py`)

```python
pricing = PricingConfig()
cost = pricing.calculate_cost(
    model="claude-sonnet-4-5",
    input_tokens=1000,
    output_tokens=500
)
```

#### Cost Utilities (`cost_utils.py`)

```python
from modules.credits import calculate_cost_from_tokens, get_cost_breakdown

cost = calculate_cost_from_tokens("gpt-5", 1000, 500)
breakdown = get_cost_breakdown(user_id, start_date, end_date)
```

### 6. Payments (`payments/`)

Cryptocurrency payment processing system.

#### DepositWalletGenerator (`wallet_generator.py`)

- Generates unique deposit addresses per user
- Supports multiple chains (ETH, Base, etc.)
- HD wallet derivation

#### DepositMonitor (`deposit_monitor.py`)

- Monitors blockchain for incoming deposits
- Automatic credit allocation
- Transaction confirmation tracking

#### TreasurySweeper (`treasury_sweeper.py`)

- Sweeps deposits to treasury wallet
- Batch processing for efficiency
- Gas optimization

**Usage**:
```python
wallet_gen = DepositWalletGenerator(config)
deposit_address = await wallet_gen.get_or_create_address(user_id)

monitor = DepositMonitor(config)
await monitor.start_monitoring()
```

### 7. x402 Payment Protocol (`x402/`)

Implementation of the [x402 HTTP payment protocol](https://x402.org) for pay-per-request API access using USDC stablecoins. Uses the **[fastapi-x402](https://github.com/jordo1138/fastapi-x402)** library with Coinbase facilitator for proper on-chain verification.

See [modules/x402/README.md](x402/README.md) for detailed documentation.

#### X402PaymentMiddleware (`middleware.py`)

- Intercepts requests with `X-PAYMENT` header
- Verifies payments via Coinbase Developer Platform (CDP) facilitator
- Creates POLYROB user profiles for x402 payers
- Settles payments on-chain (actual USDC transfer)

#### Integration Layer (`x402_integration.py`)

- `generate_user_id_from_wallet()` - Creates user IDs from wallet addresses
- `ensure_user_profile_for_payer()` - Creates user records for new payers
- `record_x402_payment()` - Records payments in database

**Configuration**:
```bash
# Environment variables
X402_ENABLED=true
X402_PAYMENT_RECIPIENT=0xYourTreasuryAddress
X402_DEFAULT_CHAIN=base  # or base-sepolia for testnet

# CDP credentials (required for mainnet)
CDP_API_KEY_ID=your_key_id
CDP_API_KEY_SECRET=your_key_secret
```

**Payment Flow**:
1. Client sends request without auth → Server returns 402 with payment details
2. Client signs payment with wallet → Sends `X-PAYMENT` header
3. Server verifies via CDP facilitator → On-chain settlement
4. Server creates user profile → Returns response

### 8. ERC-8004 Trustless Agents (`eip8004/`)

Implementation of the [ERC-8004](https://eips.ethereum.org/EIPS/eip-8004) standard for
trustless agent discovery and trust — letting agents discover, choose, and interact with
other agents across organizational boundaries without pre-existing trust. It layers on top
of existing agent protocols (A2A, MCP).

- **Decentralized identity** — NFT-based (ERC-721) agent identity on any EVM chain (`registration.py`)
- **Portable reputation** — on-chain feedback scores that follow agents across platforms (`reputation.py`)
- **Pluggable trust** — reputation, crypto-economic, or TEE validation (`validation.py`)
- **Smart-contract interfaces / ABIs** — `contracts.py`; data models in `models.py`

See [modules/eip8004/README.md](eip8004/README.md) for the full specification, the three
registries, configuration, and trust-flow examples.

## Module Initialization System

### Dependency Resolution

Modules initialize in dependency order:
```python
MODULE_INIT_ORDER = [
    ('database_manager', 1),      # Foundation layer
    ('memory_manager', 2),        # Depends on database
    ('cache_manager', 3),         # Depends on memory
    ('llm_client', 4),           # Independent of others
]
```

### BaseModule Pattern

```python
class MyModule(BaseModule):
    @property
    def required_modules(self) -> Dict[str, str]:
        return {'database_manager': 'Database access required'}
    
    @property  
    def optional_modules(self) -> Dict[str, str]:
        return {'llm_client': 'LLM integration for AI features'}
    
    async def _initialize(self) -> None:
        await self._setup_resources()
    
    async def _cleanup(self) -> None:
        await self._release_resources()
```

## Configuration

### Database Configuration
```python
db_path: str = "data/bot.db"
data_dir: str = "data"
# (Pinecone is retired — there are no pinecone_* config fields.)
```

### LLM Configuration
```python
openai_api_key: Optional[str]
anthropic_api_key: Optional[str] 
gemini_api_key: Optional[str]
model_name: str  # Default model — see modules/llm/model_registry.py for the live set
```

### Memory Configuration
```python
cache_size: int = 1000
cache_ttl: int = 3600
embedding_model_name: str = "sentence-transformers/all-MiniLM-L6-v2"
```

### Credits Configuration
```python
credits_enabled: bool = True
free_tier_credits: int = 100
credit_cost_multiplier: float = 1.0
```

### x402 Configuration
```bash
X402_ENABLED=true                          # Enable/disable x402
X402_PAYMENT_RECIPIENT=0x...               # Treasury wallet address
X402_DEFAULT_CHAIN=base                    # Network (base, base-sepolia, etc.)
CDP_API_KEY_ID=...                         # Coinbase CDP key (mainnet only)
CDP_API_KEY_SECRET=...                     # Coinbase CDP secret (mainnet only)
```

## Exports

```python
__all__ = [
    'BaseModule',
    'DatabaseManager', 'MemoryManager', 'CacheManager',
    'LLMClient', 'AnthropicClient', 'OpenAIClient',
    'DeepSeekClient', 'GeminiClient',
    'create_llm_client', 'AVAILABLE_MODELS',
    'MODULE_METADATA', 'MODULE_INIT_ORDER'
]
```

## Best Practices

### Module Development
1. **Extend BaseModule**: Always inherit from `BaseModule` for lifecycle management
2. **Declare Dependencies**: Use `required_modules` and `optional_modules` properties
3. **Handle Initialization**: Implement proper `_initialize()` and `_cleanup()` methods
4. **Error Handling**: Use module-specific exceptions and proper error recovery
5. **Resource Management**: Ensure proper cleanup of resources and connections

### Database Usage
1. **Use Transactions**: Wrap related operations in transactions
2. **Optimize Queries**: Use indexes and avoid N+1 query patterns
3. **Handle Migrations**: Plan for schema changes and data migrations

### LLM Integration
1. **Implement Fallbacks**: Always have backup providers configured
2. **Monitor Costs**: Track API usage and implement cost controls
3. **Cache Responses**: Cache frequent or expensive responses

### Credit System
1. **Track All Usage**: Record every LLM call with accurate token counts
2. **Handle Insufficient Credits**: Graceful handling when credits run out
3. **Audit Trail**: Maintain transaction history for all credit operations
