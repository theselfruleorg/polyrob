# POLYROB vs Other Agent Frameworks

A comprehensive comparison of POLYROB with major AI agent frameworks as of 2026.

---

## Quick Reference Table

| Feature | POLYROB | Hermes | OpenClaw |
|---------|---------|--------|----------|
| **License** | MIT | MIT | MIT |
| **Language** | Python | Python | TypeScript/Node |
| **Primary Focus** | Production autonomy | Self-improving agent | Personal assistant |
| **Multi-Provider LLM** | ✅ 6+ providers | ✅ via OpenRouter | ✅ Multiple |
| **Auto Failover** | ✅ Built-in | ❌ | ❌ |
| **Native Tool Calling** | ✅ Per-provider | ✅ | ✅ |
| **Persistent Memory** | ✅ SQLite + Vector | ✅ FTS5 + H-MEM | ✅ FTS5 |
| **Durable Goals** | ✅ Goal board + Cron | ✅ Cron only | ❌ |
| **Skills System** | ✅ agentskills.io `SKILL.md` + install pipeline (local/git/URL, scanned & quarantined) | ✅ Learning loop | ✅ Workspace skills |
| **Multi-Surface** | ✅ 6+ (CLI, API, Web, Telegram, WhatsApp, Email) | ✅ 6+ platforms | ✅ 20+ channels |
| **A2A Protocol** | ✅ Google spec | ❌ | ❌ |
| **MCP Support** | ✅ Full client | ✅ | ✅ |
| **Delegation** | ✅ Sub-agents, least-privilege (task-split; not model-ensemble) | ✅ RPC scripts | ✅ Multi-agent |
| **Security Model** | ✅ 3-tier access gates | ✅ Pairing only | ✅ Sandboxing |
| **Tenant Isolation** | ✅ Multi-tenant | ❌ Single-user | ❌ Single-user |
| **Self-Hosted** | ✅ Full control | ✅ | ✅ |
| **Production Ready** | ✅ Durable, multi-tenant | ⚠️ Single-user | ⚠️ Single-user |
| **Browser Automation** | ✅ Playwright | ✅ | ✅ |
| **REST API** | ✅ Built-in | ❌ | ❌ |
| **Streaming** | ✅ SSE + Surface streaming | ✅ | ✅ |

**Legend:** ✅ Supported | ⚠️ Partial/Limited | ❌ Not Supported

> **On licensing:** every framework above is permissively licensed (MIT), so license is not a
> differentiator — it's table stakes. POLYROB ships the **full engine under MIT, self-hosted**: free
> forever, yours to fork and run, with no limited "open core" held back behind a paid cloud. The
> POLYROB and Selfrule *names* are trademarks (see [TRADEMARK.md](../TRADEMARK.md)) — fork the code
> freely, just rename your distribution. What actually separates the frameworks is the capability and
> architecture rows below.

---

## Framework Deep Dives

### POLYROB

**Best for:** Running your own durable, self-hosted autonomous agent — one you control end to end. Personal-first, and it scales to multi-tenant + billing when you need it.

**Unique Strengths:**
- **Truly open** — MIT-licensed, self-hosted, fork-friendly; the full engine, free forever, not a limited open core
- **Durable autonomy** — Goal board and cron survive process restarts
- **Security model** — Three-tier access (OWNER/CORRESPONDENT/DENIED) with capability gates
- **Skill marketplace pipeline** — install skills from a local folder, GitHub repo, or a `SKILL.md` URL; every install is threat-scanned and quarantined until you explicitly approve it
- **Provider redundancy** — Automatic failover across 6+ LLM providers
- **A2A protocol** — Google's agent interoperability standard
- **Self-contained** — No external agent framework dependencies
- **Scales to teams** — single-user by default; multi-tenant isolation, metering and credits are in the core when you want them

**Trade-offs:**
- More complex configuration than single-user alternatives
- Heavier install for full feature set
- Newer ecosystem (smaller community than established frameworks)

**When to choose POLYROB:**
- You value **self-hosted control** — your keys, your data, your machine; no vendor lock-in
- You want **durable autonomy** — goals that survive restarts and deliver results unattended
- You want **provider redundancy** — automatic failover if a provider has issues
- You need **security by default** — correspondent gates and capability controls when others are in the loop
- You're **building on it** — multi-tenant, metering, and A2A interoperability are there when you grow into them

---

### Hermes Agent

**Best for:** Personal use with a focus on learning and skill development.

**Unique Strengths:**
- **Learning loop** — Agent improves from experience, creates skills
- **Nous Portal** — Single subscription for 300+ models + tools
- **Skills ecosystem** — Community skills hub with sharing
- **Rich CLI** — Full TUI with multiline editing and autocomplete
- **Multi-platform gateway** — Runs on $5 VPS or GPU clusters

**Trade-offs:**
- Single-user focused — not designed for multi-tenant deployment
- Manual provider management — no automatic failover
- Learning curve for skills system

**When to choose Hermes:**
- You want a **personal assistant that learns** from your interactions
- You prefer **curated models and tools** via one subscription
- You value **community skills** and sharing
- You're running **single-user** on modest hardware

---

### OpenClaw

**Best for:** Personal assistants across many messaging platforms with companion apps.

**Unique Strengths:**
- **Massive channel support** — 20+ platforms (WhatsApp, Telegram, Slack, Discord, iMessage, etc.)
- **Companion apps** — Windows Hub, macOS menu bar, iOS/Android nodes
- **Live Canvas** — Agent-driven visual workspace
- **Voice mode** — Wake words and continuous voice on mobile
- **Sandboxing** — Docker/SSH/OpenShell backends for isolation

**Trade-offs:**
- Node.js/TypeScript stack — different from Python ecosystem
- Single-user focused
- No built-in provider failover
- Steeper learning curve for configuration

**When to choose OpenClaw:**
- You need **omni-channel presence** across many platforms
- You want **companion apps** for mobile/desktop
- You prefer **Node.js** over Python
- You need **sandboxing** for safety

---

## Decision Matrix

### Choose POLYROB if you need:

| Requirement | Why POLYROB |
|-------------|--------------|
| **Self-hosted control** | Full data ownership, your keys and machine, no vendor lock-in |
| **Truly open** | MIT-licensed, fork-friendly — the full engine, not a limited open core |
| **Durable autonomy** | Goals and cron survive restarts, deliver results asynchronously |
| **Provider redundancy** | Automatic failover keeps you running during provider outages |
| **Multi-provider flexibility** | Switch between OpenAI, Anthropic, Google, DeepSeek, OpenRouter, NIM dynamically |
| **Room to grow** | Multi-user security, multi-tenant + billing, and A2A interoperability when you need them |

### Choose Hermes if you need:

| Requirement | Why Hermes |
|-------------|------------|
| **Personal learning** | Agent improves from experience, creates skills |
| **Curated experience** | Nous Portal provides 300+ models and tools via one subscription |
| **Community skills** | Share and discover skills via Skills Hub |
| **Modest hardware** | Runs on $5 VPS |

### Choose OpenClaw if you need:

| Requirement | Why OpenClaw |
|-------------|--------------|
| **Omni-channel presence** | 20+ messaging platforms supported |
| **Companion apps** | Native mobile/desktop experiences |
| **Node.js preference** | TypeScript stack instead of Python |
| **Visual workspace** | Live Canvas for agent-driven UI |

---

## Migration Guides

See the migration guide directory for detailed paths from each framework:

- [from-hermes.md](guide/migration/from-hermes.md) — Migrating from Hermes Agent
- [from-openclaw.md](guide/migration/from-openclaw.md) — Migrating from OpenClaw

---

## Feature Deep Dives

### Multi-Provider LLM Support

| Framework | Providers Supported | Auto Failover | Hot-Swap |
|-----------|-------------------|---------------|----------|
| **POLYROB** | OpenAI, Anthropic, Google, DeepSeek, OpenRouter, NIM | ✅ Built-in | ✅ Live (CLI `/model`, API per-request) |
| **Hermes** | 200+ via OpenRouter | ❌ | ✅ Config restart |
| **OpenClaw** | OpenAI, Anthropic, others | ❌ | ✅ Config |

Note: a cross-provider live swap resets the prompt cache (any model change breaks the cached prefix); the running conversation is preserved in place.

### Memory Systems

| Framework | Keyword Search | Vector Search | Cross-Session | Tenant Scoped |
|-----------|---------------|--------------|---------------|---------------|
| **POLYROB** | ✅ SQLite FTS5 | ✅ sqlite-vec | ✅ | ✅ |
| **Hermes** | ✅ FTS5 | ✅ Embeddings | ✅ | ❌ |
| **OpenClaw** | ✅ FTS5 | ✅ Optional | ✅ | ❌ |

### Security Models

| Framework | Access Control | Input Sanitization | Capability Gates | Sandboxing |
|-----------|----------------|-------------------|------------------|------------|
| **POLYROB** | ✅ 3-tier (OWNER/CORRESPONDENT/DENIED) | ✅ Untrusted wrapping | ✅ High-impact tool blocking | ✅ Docker sandbox (opt-in code execution) |
| **Hermes** | ✅ Pairing codes | ⚠️ Basic | ❌ | ⚠️ Optional |
| **OpenClaw** | ✅ Allowlists | ⚠️ Basic | ✅ Per-session | ✅ Docker/SSH |

---

## Conclusion

POLYROB occupies a unique position in the agent framework landscape:

- **More durable and secure** than personal-focused alternatives (Hermes, OpenClaw) — goals survive restarts, and untrusted input is treated as data, not instructions
- **Truly open and crypto-native** — MIT-licensed and self-hosted, with a built-in agent wallet, x402 payments, and A2A interoperability

Choose POLYROB when you want a **durable, secure, self-hosted autonomous agent you control** — personal-first, MIT-licensed, and ready to scale to multi-tenant production when you are.

For framework-specific recommendations, see the decision matrix above or the detailed migration guides.
