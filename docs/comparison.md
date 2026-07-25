# POLYROB vs Other Agent Frameworks

A comparison of POLYROB with two other prominent open-source agent projects,
**Hermes** ([NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent))
and **OpenClaw** ([openclaw/openclaw](https://github.com/openclaw/openclaw)).

> **Verified as of 2026-07-23.** The claims below were checked against each project's
> live upstream repository on that date (Hermes at its v0.19.0 release; OpenClaw on
> `main`). Both are fast-moving projects — treat the ✅/⚠️/❌ marks as a snapshot, and
> check the upstream repos for anything load-bearing to your decision. Where we could
> not verify a claim on the live repo, it is marked ❓ rather than guessed.

---

## Quick Reference Table

| Feature | POLYROB | Hermes | OpenClaw |
|---------|---------|--------|----------|
| **License** | MIT | MIT | MIT |
| **Language** | Python | Python | TypeScript / Node (native macOS/iOS in Swift) |
| **Primary focus** | Durable, self-hosted autonomy that scales to multi-tenant | Self-improving single-operator agent | Omni-channel personal assistant |
| **Multi-provider LLM** | ✅ 6 providers | ✅ 300+ models (portal / OpenRouter / own endpoint) | ✅ Claude, OpenAI, Gemini, DeepSeek |
| **Provider failover** | ✅ Automatic cross-provider | ✅ Credential pooling + rotation | ✅ Across auth profiles |
| **Native tool calling** | ✅ Per-provider | ✅ | ✅ |
| **Persistent memory** | ✅ SQLite FTS5 + optional vector | ✅ FTS5 + reflective/curated, pluggable providers | ⚠️ Session history + workspace files (no dedicated vector RAG) |
| **Durable task/goal board** | ✅ Goal board + cron | ✅ Kanban board with DAG decomposition | ❌ (cron + sessions only) |
| **Proactive self-wake** | ✅ Self-wake + change-gate | ❌ | ❌ |
| **Skills system** | ✅ `SKILL.md` + install pipeline (local/git/URL, scanned & quarantined) | ✅ Autonomous skill creation / learning loop | ✅ Workspace skills |
| **Messaging channels** | 7 (Telegram, WhatsApp, Email, Discord, Slack, Signal, X) | ~30 (adds WeChat, Feishu, LINE, Matrix, iMessage, …) | ~23 + native mobile/desktop apps |
| **REST API** | ✅ Built-in | ❌ (gateway / web, not a documented REST session API) | ❌ |
| **A2A protocol** | ✅ Google spec | ❌ (exposes ACP to editors) | ❌ |
| **MCP client** | ✅ | ✅ | ✅ |
| **MCP server (inbound)** | ✅ `POST /mcp`, 5 read-only tools | ✅ (`mcp_serve.py`; not headlined) | ❌ (community/requested) |
| **Delegation / multi-agent** | ✅ Least-privilege sub-agents | ✅ RPC + kanban swarm | ✅ Multi-agent routing |
| **Economic agency (wallet / payments)** | ✅ Wallet + x402 + invoicing | ❌ | ❌ |
| **Tenant isolation** | ✅ Multi-tenant (single-process, `user_id`-scoped) | ❌ Single-operator | ❌ Single-operator |
| **Access model** | ✅ 3-tier OWNER / CORRESPONDENT / DENIED + origin taint | ⚠️ Binary authorized/not + DM pairing | ⚠️ DM pairing + allowlists |
| **Owner-approval queue (HITL)** | ✅ Durable, remotely-approvable | ⚠️ In-memory (lost on restart) | ⚠️ Pairing = channel access, not per-tool |
| **Code-exec sandboxing** | ✅ Docker (opt-in); local / ssh backends | ✅ 6 backends (local, Docker, SSH, Singularity, Modal, Daytona) | ✅ Docker / SSH / OpenShell |
| **Self-hosted** | ✅ | ✅ | ✅ |

**Legend:** ✅ Supported · ⚠️ Partial / with caveats · ❌ Not supported · ❓ Unverified

> **On licensing:** all three frameworks are permissively MIT-licensed, so license is
> not a differentiator — it's table stakes. POLYROB ships the **full engine under MIT,
> self-hosted**: free forever, yours to fork and run, with no limited "open core" held
> back behind a paid cloud. The POLYROB and Selfrule *names* are trademarks (see
> [TRADEMARK.md](../TRADEMARK.md)) — fork the code freely, just rename your
> distribution. What actually separates the frameworks is the capability and
> architecture rows above.

---

## What actually sets POLYROB apart

Two of the three projects here are larger and more established than POLYROB (both
Hermes and OpenClaw are among the most-starred agent repos on GitHub as of
2026-07-23). POLYROB does **not** compete on channel breadth or community size. What
it has that neither upstream does, verified against their live repos:

- **Economic agency.** POLYROB has a built-in agent wallet, x402 pay-per-request (in
  and out), invoicing with branded QR cards, and rolling spend caps. **Neither Hermes
  nor OpenClaw has any payment, wallet, or monetization capability.** This is
  POLYROB's clearest single differentiator.
- **Multi-tenancy with an origin-taint access model.** POLYROB resolves every inbound
  message to one of three tiers — **OWNER** steers, **CORRESPONDENT** (a third party
  the agent itself contacted) can only return *data*, **DENIED** is blocked — and
  gates high-impact tools off while a session is correspondent-tainted. Both upstreams
  are single-operator: Hermes' authorization is a binary "is this user allowed", and
  its own security docs recommend running separate instances for isolation.
- **Proactive self-wake.** POLYROB can re-enter an idle session on its own when
  observable state changes (with depth/backoff guards and a change-gate to avoid
  paying for no-op wakes). Both upstreams are reactive — proactivity comes from cron,
  a task queue, or a completion callback; **neither has a self-wake primitive.**
- **A durable, remotely-approvable owner-approval queue.** High-impact and money
  actions route through a persisted approval queue that survives a restart and can be
  approved from your phone. Hermes' approval is pattern-based and in-memory (in-flight
  approvals are lost on restart); OpenClaw's pairing gates *channel access*, not
  per-tool actions.

Where POLYROB **lags**, honestly: **channel breadth** (7 messaging channels vs
Hermes' ~30 and OpenClaw's ~23, and OpenClaw's native mobile/desktop apps),
**code-exec backend variety** (Hermes offers serverless backends like Modal and
Daytona), and **community/ecosystem size** (POLYROB is the newer project).

---

## Framework Deep Dives

### POLYROB

**Best for:** Running your own durable, self-hosted autonomous agent that you control
end to end — personal-first, scaling to multi-tenant + billing when you need it.

**Unique strengths:**
- **Economic agency** — agent wallet, x402 payments (charge and pay), invoicing, spend caps
- **Multi-tenant + origin-taint security** — OWNER/CORRESPONDENT/DENIED tiers with capability gates
- **Durable autonomy** — goal board, cron, and self-wake all survive process restarts
- **Durable owner-approval queue** — remotely approvable, restart-surviving human-in-the-loop
- **Interoperability** — MCP client *and* server, plus Google's A2A protocol and a REST/OpenAI-compatible API
- **Self-contained** — a native LLM layer, no third-party agent framework dependency

**Trade-offs:**
- Fewer messaging channels than the omni-channel alternatives
- More configuration surface than a single-user assistant
- Newer project — smaller community and ecosystem than the two established ones

**When to choose POLYROB:**
- You want an agent that can **transact** — quote, invoice, get paid, and pay for resources
- You expect **more than one person** to interact with it, and need stranger input treated as data
- You want **durable, proactive autonomy** — goals and self-wake that survive restarts
- You want **self-hosted control** — your keys, your data, your machine, MIT-licensed

---

### Hermes Agent

**Best for:** A single operator who wants a self-improving agent with broad channel
reach and a large model/tool catalog. *(Live upstream: v0.19.0, 2026-07-20.)*

**Unique strengths:**
- **Broad channel reach** — ~30 connectors, including heavy CN/enterprise (WeChat, Feishu, LINE, Matrix, iMessage)
- **Large model catalog** — 300+ models via Nous Portal / OpenRouter / your own endpoint, with credential pooling and rotation
- **Learning loop** — autonomous skill creation; skills self-improve during use
- **Kanban swarm** — a multi-worker task board with DAG decomposition for parallel workstreams
- **Serverless code-exec** — six terminal backends including Modal and Daytona (idle-hibernating)
- **Also an MCP server** — ships an `mcp_serve.py` surface (not foregrounded in its README)

**Trade-offs:**
- **Single-operator** — authorization is binary; its own security docs suggest separate instances for isolation
- **No economic agency** — no wallet, payments, or monetization
- **No self-wake** — proactivity is cron / queue / completion-driven
- **In-memory approvals** — in-flight human-in-the-loop state is lost across a restart

**When to choose Hermes:**
- You want the **widest channel and model reach** for a single-user setup
- You value a **skill learning loop** and a kanban-style task swarm
- You want **serverless sandbox** backends (Modal, Daytona)

---

### OpenClaw

**Best for:** An omni-channel personal assistant with first-class native apps.
*(Formerly Warelay → CLAWDIS → Clawdbot → Moltbot → OpenClaw; live upstream on `main`,
2026-07-23.)*

**Unique strengths:**
- **Massive channel support** — ~23 messaging platforms (WhatsApp, Telegram, Slack, Discord, iMessage, Signal, Matrix, Feishu, LINE, WeChat, and more)
- **Native companion apps** — macOS app and iOS/Android nodes
- **Subscription-auth providers** — sign in with ChatGPT/Codex OAuth, with model failover across auth profiles
- **Sandboxing** — Docker (default), SSH, and OpenShell backends, with per-mode allow/deny tool lists
- **Voice mode** — wake words and continuous voice

**Trade-offs:**
- **Node.js / TypeScript stack** — different from the Python ecosystem
- **Single-operator** — pairing gates channel access, not per-tool actions
- **No economic agency**, no built-in provider-agnostic REST API, no dedicated vector RAG
- **MCP client only** — exposing OpenClaw's own tools as an MCP server is a community/requested feature, not native

**When to choose OpenClaw:**
- You need **omni-channel presence** across many messaging platforms
- You want **native mobile/desktop apps** and voice mode
- You prefer a **Node.js/TypeScript** stack

---

## Decision Matrix

### Choose POLYROB if you need:

| Requirement | Why POLYROB |
|-------------|--------------|
| **Economic agency** | Built-in wallet, x402 payments, invoicing, spend caps — neither alternative has any |
| **Multi-user safety** | 3-tier access + origin taint keeps a stranger's message as data, not a command |
| **Durable, proactive autonomy** | Goals, cron, and self-wake survive restarts and act unattended |
| **Restart-safe approvals** | A durable, remotely-approvable owner-approval queue for high-impact actions |
| **Interoperability** | MCP client + server, A2A, and a REST/OpenAI-compatible API |
| **Self-hosted control** | Full data ownership, your keys and machine, MIT-licensed, no vendor lock-in |

### Choose Hermes if you need:

| Requirement | Why Hermes |
|-------------|------------|
| **Widest channel/model reach** | ~30 connectors, 300+ models, credential pooling |
| **A skill learning loop** | Autonomous skill creation that improves with use |
| **Serverless sandboxes** | Modal / Daytona backends that hibernate when idle |
| **Kanban task swarm** | Multi-worker board with DAG decomposition |

### Choose OpenClaw if you need:

| Requirement | Why OpenClaw |
|-------------|--------------|
| **Omni-channel + native apps** | ~23 platforms plus macOS/iOS/Android companions |
| **Voice-first interaction** | Wake words and continuous voice |
| **Node.js preference** | TypeScript stack instead of Python |
| **Subscription-auth providers** | Sign in with ChatGPT/Codex OAuth |

---

## Migration Guides

See the migration guide directory for detailed paths from each framework:

- [from-hermes.md](guide/migration/from-hermes.md) — Migrating from Hermes Agent
- [from-openclaw.md](guide/migration/from-openclaw.md) — Migrating from OpenClaw

---

## Feature Deep Dives

### Multi-Provider LLM Support

| Framework | Providers | Failover | Hot-swap |
|-----------|-----------|----------|----------|
| **POLYROB** | OpenAI, Anthropic, Google, DeepSeek, OpenRouter, NIM | ✅ Automatic cross-provider (on billing/rate-limit errors) | ✅ Live (CLI `/model`, API per-request) |
| **Hermes** | 300+ via portal / OpenRouter / own endpoint | ✅ Credential pooling + rotation | ✅ `hermes model` switch |
| **OpenClaw** | Claude, OpenAI, Gemini, DeepSeek | ✅ Across auth profiles | ✅ Config |

Note: a cross-provider live swap resets the prompt cache (any model change breaks the
cached prefix); the running conversation is preserved in place.

### Memory Systems

| Framework | Keyword search | Vector search | Cross-session | Tenant scoped |
|-----------|---------------|--------------|---------------|---------------|
| **POLYROB** | ✅ SQLite FTS5 | ✅ sqlite-vec (optional) | ✅ | ✅ |
| **Hermes** | ✅ FTS5 | ✅ Pluggable providers | ✅ | ❌ |
| **OpenClaw** | ⚠️ Session history + workspace files | ❌ (none documented) | ✅ | ❌ |

### Security Models

| Framework | Access control | Input sanitization | Capability gates | Tenant isolation | Sandboxing |
|-----------|----------------|-------------------|------------------|------------------|------------|
| **POLYROB** | ✅ 3-tier + origin taint | ✅ Untrusted wrapping | ✅ High-impact tool blocking | ✅ `user_id`-scoped (single process) | ✅ Docker (opt-in code exec) |
| **Hermes** | ⚠️ Binary + DM pairing | ⚠️ Basic | ❌ | ❌ Single-operator | ✅ Multiple backends |
| **OpenClaw** | ⚠️ Pairing + allowlists | ⚠️ Basic | ⚠️ Per-session tool lists | ❌ Single-operator | ✅ Docker / SSH / OpenShell |

> POLYROB's tenant isolation is enforced by `user_id`-scoped software checks inside a
> single shared process — real and load-bearing, but a software boundary, not an OS
> one. For genuinely adversarial multi-tenancy, run separate instances. See
> [security-model.md](guide/security-model.md) for the full honest treatment of what
> is and isn't a hard boundary.

---

## Conclusion

POLYROB occupies a specific niche among open-source agents: it is the one built for an
agent that **transacts, serves more than one person, and acts on its own** — safely.

- **Economic** — the only one of the three with a wallet, payments, and invoicing.
- **Multi-tenant and secure** — an origin-taint access model and durable approval
  queue, where the alternatives are single-operator.
- **Proactively autonomous** — goals, cron, and self-wake that survive restarts.

It is **not** the broadest in channels (Hermes and OpenClaw both have more) or the
largest community. Choose POLYROB when you want a **durable, secure, self-hosted agent
you control** — personal-first, MIT-licensed, and ready to grow into multi-tenant,
transacting production.

For framework-specific recommendations, see the decision matrix above or the detailed
migration guides.
