# Architecture

This document describes polyrob's high-level architecture. It is intended for developers extending the framework or building their own instances.

---

## Framework vs instance

**polyrob** is the framework — the package, CLI, and agent runtime.

**rob** is the default instance shipped with the framework. An instance is a named deployment (`POLYROB_INSTANCE_ID`) with its own self-identity and owner principal; give it a separate data home (`POLYROB_DATA_DIR`) too if you want its memory, skills, and scheduled work fully isolated from other instances. You can run multiple named instances on the same machine or server. See [instances.md](instances.md).

---

## Core: dependency injection container

The entry point (`main.py` or `polyrob`) builds a **DI container** (`core/`) that wires together all subsystems:

- **Config** — loads `.env` file, resolves feature flags
- **Component lifecycle** — starts and stops services in dependency order
- **Permission system** — enforces what tools and surfaces can access

Everything is resolved from the container; subsystems do not import each other directly.

---

## LLM layer (`modules/llm/`)

polyrob has a **native multi-provider LLM layer** — no third-party agent framework (LangChain etc.) is used at runtime.

Supported providers: **OpenAI**, **Anthropic**, **Google Gemini**, **DeepSeek**, **OpenRouter** (Grok, GLM, Qwen, and other models it hosts), and **NVIDIA NIM**. Each provider has a native adapter implementing a common `BaseChatModel` interface. The factory (`llm_factory.create_chat_model`) selects the adapter based on the configured provider.

Key behaviors:
- **Native tool calling** — uses each provider's structured function-call protocol; no JSON parsing hacks.
- **Automatic failover** — a rate-limit or connection error on the primary provider automatically retries on a fallback provider. Failover on billing/quota-exhaustion errors is opt-in (`BILLING_FAILOVER_ENABLED`, off by default).
- **Prompt caching** — provider-specific caching (Anthropic `cache_control`, OpenAI prefix caching) reduces token costs on long sessions.
- **Reasoning tokens** — extended thinking / reasoning effort for Claude, DeepSeek, and OpenAI reasoning models when `THINKING_CONFIG_ENABLED=true` (off by default).

---

## Task agent and step loop (`agents/task/`)

The core execution unit is the **Task agent**, which runs a step loop:

```
prepare_step
  → call LLM  (with tool schemas + memory context)
  → validate response  (schema check, tool-call repair)
  → execute tools  (via Controller)
  → record results  (memory write, telemetry)
  → finalize step  (check done / error / compaction)
```

Each step is driven by the LLM's response. The agent loops until it calls `done()`, hits a budget cap, or the session is cancelled.

**Key components:**

| Component | Responsibility |
|-----------|---------------|
| `SessionOrchestrator` | Session lifecycle, browser pool, multi-agent coordination |
| `Agent` | Step loop execution |
| `MessageManager` | Message history, token counting, context compaction |
| `Controller` | Tool dispatch: load, validate, execute, hook pipeline |
| `Registry` | Tool/action registration and schema generation |
| `ToolCallTracker` | Tool-call ID lifecycle (single source of truth) |

---

## Tools (`tools/`)

Tools are registered with the Controller and exposed to the LLM as callable functions. Not every tool is loaded by default — pick a named toolset or an explicit tool list with `polyrob run --toolset` / `--tools` (see [cli.md](cli.md)).

| Tool set | Capabilities |
|----------|-------------|
| Web fetch / search | `web_fetch` reads a single URL as clean markdown without a browser (the lightweight default reader); Perplexity-backed web search when `PERPLEXITY_API_KEY` is set |
| Browser (`tools/browser/`) | Playwright: navigate, click, type, scroll, screenshot, extract DOM — opt-in (`browser` toolset/tool) |
| Filesystem / docs | Create, read, edit files in the session workspace |
| AnySite (`tools/anysite/`) | Structured data from 200+ external sites and platforms via the `anysite` CLI (needs `ANYSITE_API_KEY`) |
| MCP (`tools/mcp/`) | Model Context Protocol — connect any MCP-compatible server (filesystem, GitHub, Slack, or your own); none are configured out of the box |
| Crypto / x402 (`core/wallet/`, `tools/x402/`) | Native agent wallet pays for external resources via x402 — opt-in (`X402_CLIENT_ENABLED`); Hyperliquid trading — opt-in (`HYPERLIQUID_TRADING_ENABLED`) |
| Code execution (`tools/code_exec/`) | Runs code in a local subprocess — a convenience, not a security sandbox; opt-in (`CODE_EXEC_ENABLED`), never loaded by default |

---

## Memory (`modules/memory/`)

polyrob uses a **pluggable memory system** with a single external-provider seam.

Default backend (`MEMORY_BACKEND=sqlite`): keyword full-text search (FTS5) in a local `memory.db`. Cross-session recall is tenant-scoped by `user_id`.

Optional vector backend (`MEMORY_BACKEND=local_vector`): adds sentence-transformer embeddings via `sqlite-vec` loaded through `apsw`. Degrades gracefully to FTS if the extension is unavailable. See [configuration.md](configuration.md) for the apsw note.

The memory flow within a session:
1. At the start of each step, relevant memories are **prefetched** and injected as context.
2. After each step, new findings are **synced** to the store.
3. The agent can also **search** memory explicitly with the `session_search` action.

An optional episodic activity log (`EPISODIC_MEMORY_ENABLED`) records a short summary of each completed run (chat, goal, or cron job), so a new session can pick up with a brief "what happened last time" instead of starting cold. Off by default on the server; on under `POLYROB_LOCAL`.

---

## Surfaces

All interaction surfaces implement a common **Surface contract** — a unified interface for receiving user input and sending agent output. This means the same agent core powers every surface.

| Surface | Description |
|---------|-------------|
| CLI | Interactive REPL (`polyrob`) and non-interactive runner (`polyrob run`) |
| Web console | Real-time Socket.IO browser interface (`polyrob dashboard`, aliased `polyrob webgate`) — watch sessions live, send guidance. See [console.md](console.md) |
| Telegram | Telegram bot surface via aiogram (optional `telegram` extra); run with `polyrob telegram` |
| WhatsApp | WhatsApp Cloud API webhook surface (needs Meta credentials); run with `polyrob whatsapp` |
| Email | IMAP-poll inbound + SMTP outbound surface (`surfaces/email/`, `EMAIL_SURFACE_ENABLED`); run with `polyrob email` |
| REST API | HTTP endpoints for programmatic access — see [api.md](api.md) |

Telegram, WhatsApp, and Email are off by default. When enabled, messages from anyone other than the bound owner are treated as untrusted correspondent data, not steering input — manage this with `polyrob owner` (see the "Chat-surface access model" section of [AGENTS.md](../../AGENTS.md) for the full model). Run every enabled chat surface together with `polyrob gateway`.

---

## Autonomy loops

Background loops run independently of active sessions, enabling goal-directed behavior:

| Loop | Description | Flag |
|------|-------------|------|
| Cron | Schedule recurring tasks | `CRON_ENABLED` |
| Goals | Durable goal board — agent pursues queued goals when idle | `GOALS_ENABLED` |
| Curator | Archives stale authored skills, reactivates on reuse | `CURATOR_ENABLED` |
| Background review | Aux-model reviews work after productive turns | `BACKGROUND_REVIEW_ENABLED` |
| Self-wake | Re-enters idle sessions when a goal or async result arrives | `SELF_WAKE_ENABLED` |

All loops are **default-off** on the server. Setting `POLYROB_LOCAL=true` enables the safe subset for single-user local installs.

---

## Agent delegation

The agent can spawn **sub-agents** for parallel or delegated work via the `delegate_task` tool. Sub-agents are leaf nodes (cannot further delegate), run with a least-privilege tool set, and report results back to the parent session. Gated by `SUB_AGENTS_ENABLED` (default on) with conservative depth and concurrency caps.
