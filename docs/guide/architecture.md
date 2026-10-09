# Architecture

This document describes polyrob's high-level architecture. It is intended for
developers extending the framework or building their own instances.

**polyrob** is the framework; a running deployment is one **instance** of it,
named `polyrob` by default. Naming an instance and isolating its data are two
separate decisions — see [instances.md](instances.md), and
[profiles.md](profiles.md) for the one-command way to do both.

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

Six providers are built in, each with a native adapter behind one
`BaseChatModel` interface, alongside flat-rate rows, OAuth subscription plans and
any endpoint you declare as data in `providers.yaml` with no code. Which six, and
how one is chosen: [configuration.md §4](configuration.md#4-providers-and-models).

Key behaviors:
- **Native tool calling** — uses each provider's structured function-call protocol; no JSON parsing hacks.
- **Automatic failover** — a rate-limit or connection error on the primary provider automatically retries on a fallback provider. Failover on billing/quota-exhaustion errors is also attempted before a permanent halt (`BILLING_FAILOVER_ENABLED`, on by default); it has no second provider to fall back to unless more than one is configured.
- **Prompt caching** — on by default wherever the provider supports it: Anthropic `cache_control` breakpoints with a 5-minute or 1-hour window, OpenAI and DeepSeek automatic prefix caching, OpenRouter and Gemini breakpoints. What makes it pay is that the assembled request is a stable *prefix* — see [Prompt caching and the context window](#prompt-caching-and-the-context-window) below.
- **Reasoning tokens** — extended thinking / reasoning effort for Claude, DeepSeek, and OpenAI reasoning models when `THINKING_CONFIG_ENABLED=true` (off by default).

### Prompt caching and the context window

Every provider that caches a prompt caches a **prefix**: your next request is
served from cache only for the leading bytes it shares with the last one. One
rewritten message early in the conversation re-bills everything behind it. So
POLYROB assembles each request the same way twice in a row, on purpose.

**What is held still.** The tool schemas are sorted by action name, so a cron
job, a goal and a chat session holding the same tools send identical bytes. The
requested tool set does not shrink and re-grow when a credential check flaps.
The first eight messages (system prompt, runtime identity, environment, SOUL,
project context, the initial task, the skill catalog, the tool catalog) are a
pinned *foundation*: written once, never repaired, never evicted, and a later
change to the tool catalog arrives as a short note at the end rather than a
rewrite in place.

**Tools loaded mid-session.** `load_tool` adds a tool's actions to the schema
list; on most providers that is the one prefix change POLYROB still accepts,
because the alternative is a tool the model cannot see. On a Claude model that
supports it, the new actions are declared as *deferred* and surfaced to the
model with a small addition note instead, so the cached prefix survives. An
opt-in third shape (`TOOL_SCHEMAS_FROZEN`) pins the list for the whole session
and reaches late tools through a `tool_call(name, arguments)` bridge on any
provider — at the cost of the provider's own argument validation.

**What moves, and where.** Everything time-varying is appended at the tail: the
per-step state message (step counter, clock, browser view), recalled memories,
your mid-run guidance, one-shot notes. Those cost their own bytes and nothing
else. The state message never enters durable history at all.

**What it costs when the window fills.** A ladder at 70 / 85 / 95 % of the
window: a log line, then a deterministic pass that demotes old tool results to
one line plus a pointer to where the full text was written, then — only if that
was not enough — a compaction through the model, then a non-LLM emergency prune.
Compaction is the expensive event, because a rebuild is the one moment the whole
prefix goes cold, so it waits at least eight steps between firings unless the
last one actually freed 5 % or more.

**Across a restart.** The rendered foundation and the tool order are saved with
the conversation, so a resumed session re-issues the same prefix instead of
re-rendering it from the current environment. Change the model, the provider or
the working directory and it rebuilds — once, with a warning that says so.

**Seeing it.** `/context` shows what is in the window slot by slot, and the
provider's own figures for the last request. The status bar carries `cache N%`
beside `ctx N%`. `polyrob doctor` reports the same thing over a window of real
billing rows, including tokens written *into* cache — a write costs more than an
ordinary input token, so a prefix written once and reused fifty times and one
rewritten fifty times are very different bills that a hit rate alone cannot tell
apart.

Nothing here needs configuring. Every behaviour above has a flag that restores
the previous one byte-for-byte if you need it: see
[configuration.md](configuration.md) and
[`docs/CONFIGURATION.md`](../CONFIGURATION.md).

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
| `MessageManager` | Message history; token counting anchored on the provider's own reported count; the one context gauge every surface reads; context compaction |
| `Controller` | Tool dispatch: load, validate, execute, hook pipeline |
| `Registry` | Tool/action registration and schema generation. The list it emits to the provider is sorted by action name, so the bytes do not move under the cache |
| `ToolCallTracker` | Tool-call ID lifecycle (single source of truth) |

---

## Tools (`tools/`)

Tools are registered with the Controller and exposed to the LLM as callable functions. Not every tool is loaded by default — pick a named toolset or an explicit tool list with `polyrob run --toolset` / `--tools` (see [cli.md](cli.md)).

| Tool set | Capabilities |
|----------|-------------|
| Web fetch / search | `web_fetch` reads a single URL as clean markdown without a browser (the lightweight default reader); Perplexity-backed web search (the `discovery` pack) when `PERPLEXITY_API_KEY` is set |
| Browser (`tools/browser/`) | Playwright: navigate, click, type, scroll, screenshot, extract DOM — opt-in (`browser` toolset/tool) |
| Filesystem / docs | Create, read, edit files in the session workspace |
| AnySite (the `discovery` pack, `packs/discovery/`) | Structured data from 200+ external sites and platforms via the `anysite` CLI (needs `ANYSITE_API_KEY`) |
| MCP (`tools/mcp/`) | Model Context Protocol — connect any MCP-compatible server (filesystem, GitHub, Slack, or your own); none are configured out of the box |
| Crypto / x402 (`core/wallet/`, `tools/x402/`) | A native agent wallet that pays for external resources over x402 — opt-in (`X402_CLIENT_ENABLED`) |
| DeFi and on-chain (`tools/defi/`, `tools/launchpad/`, `tools/dapp_browser/`; the venues in the `markets` pack, `packs/markets/`) | Reading a chain, swapping, bridging, deploying a token, launching one, and driving a web dapp with the agent's own wallet — every verb off by default and separately armed; see [payments.md](payments.md) |
| Durable apps (`tools/app_service/`) | Runs an app the agent built as a supervised container behind a public URL — opt-in (`AGENT_BUILDER_MODE=ship`); see [deployment-postures.md](deployment-postures.md) |
| Code execution (`tools/code_exec/`) | Runs code in a subprocess or a hardened container; opt-in (`CODE_EXEC_ENABLED`), never loaded by default. `run_code(persist=True)` keeps a per-session Python kernel; `run_code(tools=True)` lets a script call an allowlist of agent tools over a per-run RPC that re-enters the Controller, so every gate still applies (`CODE_EXEC_TOOL_CALLS`). `CODE_EXEC_NETWORK=proxy` puts the session sandbox behind one allowlist proxy |
| Shell (`tools/shell/`) | A persistent `shell` and background `process` jobs (poll, wait, log, kill, stdin through `process_write`/`process_submit`/`process_close`, PTY, notify on exit or on a pattern). In the session sandbox from `AGENT_COMPUTE_POSTURE=1`; on the host only for the owner's own terminal turn at posture 3. Executors: docker or Podman, `ssh`, and the sandbox packs (`modal`, `daytona`, `vercel_sandbox`, `singularity`). Every line passes the command guard (`core/security/command_guard.py`) |
| Email (`tools/email_tool.py`, `tools/email_mailbox.py`, `tools/email_providers/`) | The agent's own mailbox: `email_list`, `email_read`, `email_save_attachment`, `email_reply`, `email_forward`, `email_folders`, `email_mark`, `email_move`, `email_delete` (to Trash) and `email_send`, over SMTP/IMAP or AgentMail. Every send verb rides one gated rail that tiers every recipient |
| X (the `x` pack, `packs/x/`) | Posts, replies and DMs on the agent's own account; `twitter_dm` sends over X Chat and falls back to a plaintext DM for a cold first contact. See [x-twitter.md](x-twitter.md) |

---

## Memory (`modules/memory/`)

polyrob uses a **pluggable memory system** with a single external-provider seam.

Default backend (`MEMORY_BACKEND=sqlite`): keyword full-text search (FTS5) in a local `memory.db`. Cross-session recall is tenant-scoped by `user_id`.

Optional vector backend (`MEMORY_BACKEND=local_vector`): adds sentence-transformer embeddings via `sqlite-vec` loaded through `apsw`. Degrades gracefully to FTS if the `[memory-vector]` extra is absent or the extension is unavailable. See [configuration.md](configuration.md) for the apsw note.

The memory flow within a session:
1. At the start of each step, relevant memories are **prefetched** and injected as context.
2. After each step, new findings are **synced** to the store.
3. The agent can also **search** memory explicitly with the `session_search` action.

An optional episodic activity log (`EPISODIC_MEMORY_ENABLED`) records a short summary of each completed run (chat, goal, or cron job), so a new session can pick up with a brief "what happened last time" instead of starting cold. It belongs to the autonomy group: off unless `POLYROB_LOCAL` and `AUTONOMY_ENABLED` are both on, or an `AUTONOMY_POSTURE` moves its default.

---

## Surfaces

Every surface implements one **Surface contract** for receiving input and sending
output, so the same agent core powers all of them. Eight chat surfaces, plus two
programmatic ones:

| Surface | Start it with | Notes |
|---------|---------------|-------|
| CLI | `polyrob` / `polyrob run` | Interactive REPL and one-shot runner |
| Telegram | `polyrob telegram` | aiogram; needs the `telegram` extra |
| WhatsApp | `polyrob whatsapp` | WhatsApp Cloud API webhook; needs Meta credentials |
| Email | `polyrob email` | IMAP poll in, SMTP out |
| Discord | `polyrob discord` | |
| Slack | `polyrob slack` | |
| Signal | `polyrob signal` | |
| X | — | DMs are read and sent by the agent's `twitter_get_dms` / `twitter_dm` tools over X Chat. The old `polyrob x` poller reads a DM endpoint X no longer delivers to and is obsolete. See [x-twitter.md](x-twitter.md) |
| Feishu / Lark | `polyrob gateway` | Long connection (the `feishu` extra) or a signed webhook; text, files, buttons. See [feishu-dingtalk.md](feishu-dingtalk.md) |
| DingTalk | `polyrob gateway` | Stream Mode, text. See [feishu-dingtalk.md](feishu-dingtalk.md) |
| Web console | `polyrob dashboard` | Socket.IO browser interface. See [console.md](console.md) |
| REST / A2A / `/v1` | `polyrob serve` | Programmatic access. See [api.md](api.md) |

Every chat surface is off until you enable it. `polyrob gateway` runs all the
enabled ones in one process.

### Who may talk to it

Inbound access has four tiers, resolved once at the routing boundary: the **owner**
steers the agent; a **correspondent** the agent contacted first has its replies
delivered as data, never as instructions; a **group member** speaks in a room you
allowed, from a read-only room toolset; anyone else is **denied**. Rooms have their
own regime — per-room roles, policy, caps and quiet hours: see
[groups.md](groups.md). What each tier may reach is in
[security-model.md](security-model.md).

### The decision plane

One function decides what an inbound message is —
`core/surfaces/dispatcher.py::route_inbound` — and one executor carries it out for
every surface, `surfaces/telegram/harness.py::act_on_inbound` (the module name is
historical; it is not Telegram-specific). That is why the owner gets the same
in-chat verbs everywhere, and why a decision made from a phone is the same
primitive as the CLI's. The verb list and what each one does live in
[owner-controls.md](owner-controls.md) and [cli.md](cli.md).

---

## Autonomy loops

Background loops run independently of active sessions, so the agent can pursue work
between your messages:

| Loop | Description | Flag |
|------|-------------|------|
| Goals | Durable goal board — the agent pursues queued goals when idle | `GOALS_ENABLED` |
| Self-wake | Re-enters an idle session when a goal or a background result arrives | `SELF_WAKE_ENABLED` |
| Background review | An aux model reviews the work after productive turns | `BACKGROUND_REVIEW_ENABLED` |
| Curator | Archives stale authored skills, reactivates them on reuse | `CURATOR_ENABLED` |
| Cron | Scheduled runs | `CRON_ENABLED` |

**They are all off by default, everywhere.** The first four turn on as a group only
when `POLYROB_LOCAL` **and** `AUTONOMY_ENABLED` are both set — `POLYROB_LOCAL` alone
turns on the *interactive* tools (coding, git, knowledge base, project context) and
nothing self-directed. `CRON_ENABLED` is not in that group at all: set it yourself,
or set `AUTONOMY_POSTURE=full`, which moves its default. The dial, its four axes and
how to stop it are in [configuration.md](configuration.md#6-the-autonomy-dial) and
[owner-controls.md](owner-controls.md).

The agent manages its own scheduled runs with the `cronjob` tool: schedule,
list, show, edit and cancel. An edit patches the job in place rather than
re-writing it, and a job you scheduled can only be edited on your own turn —
see [cli.md](cli.md#polyrob-cron--scheduled-runs).

**Who wrote it.** Every cron job and goal carries an `authored_by` stamp. Every
owner path (`polyrob cron schedule`, `/cron add`, the console, `/groups service`)
stamps `owner`; the agent's own tools stamp `agent`, and so does an owner turn that
had already read outside text (a web page, mail, a room). Standing owner authority —
the toolset as written, an X post or room moderation without a per-run tap, a write
verb — reads only a positive `owner` stamp. `/adopt <id>` shows you an
agent-authored job or goal in full (task, schedule, tools, target, pinned skills)
and makes it yours on one confirm; a pinned skill edited after that turns it back
into the agent's until you adopt it again. Rows from before the stamp existed are
stamped once with `python -m cron.stamp_authorship` (run it with `--dry-run` first).

---

## Agent delegation

The agent can spawn **sub-agents** for parallel or delegated work via the `delegate_task` tool. Sub-agents are leaf nodes (cannot further delegate), run with a least-privilege tool set, and report results back to the parent session. Gated by `SUB_AGENTS_ENABLED` (default on) with conservative depth and concurrency caps.
