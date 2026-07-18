<div align="center">

<img src="https://raw.githubusercontent.com/theselfruleorg/polyrob/main/docs/assets/polyrob-logo.png" alt="POLYROB" width="440">

### A self-hosted autonomous AI agent that pursues goals, learns from experience, and runs entirely on your own machine.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![PyPI](https://img.shields.io/pypi/v/polyrob.svg)](https://pypi.org/project/polyrob/)
[![CI](https://github.com/theselfruleorg/polyrob/actions/workflows/ci.yml/badge.svg)](https://github.com/theselfruleorg/polyrob/actions)
[![Stars](https://img.shields.io/github/stars/theselfruleorg/polyrob?style=social)](https://github.com/theselfruleorg/polyrob)

**[Quick Start](#quick-start) · [Docs](#documentation) · [Examples](docs/examples.md) · [Comparison](docs/comparison.md)**

</div>

POLYROB is a self-hosted autonomous AI agent that runs on your own machine. You give it a goal in
plain language and it does the rest — planning the work into steps, browsing the web, reading and
writing files, running code and shell commands, calling external tools and APIs, and recovering
from its own errors until the goal is done. It works with every major LLM provider (OpenAI,
Anthropic, Google, DeepSeek, OpenRouter, NVIDIA NIM) and fails over between them automatically,
remembers what it learns across sessions, and reaches you wherever you are — your terminal, a web
dashboard, a REST API, or chat surfaces like Telegram and email. Switch it into personal-agent mode
and it becomes durably autonomous: it keeps a backlog of goals that survives restarts, writes and
curates its own skills from experience, wakes itself to follow up on unfinished work, and refines
an evolving model of how you work.

> **Self-hosted, not a SaaS.** POLYROB runs on your own machine or server. There is no hosted
> version — your keys, your data, your control. The core install is ~50 MB with zero cloud
> dependencies; everything beyond LLM calls stays on your box unless you opt in.

---

## 🎯 Give it a goal and walk away

POLYROB doesn't just answer — it *works*, and it keeps working when you're not watching.

- **Durable goal board** — a cross-session backlog in SQLite with atomic claims and a circuit breaker that survives process restarts, so long-running or unattended work isn't lost to a reboot.
- **Cron in natural language** — the agent schedules its own recurring runs (`"every monday 09:00"`, `"30m"`, cron syntax), and can deliver results out-of-band to Telegram, email, or X.
- **Self-wake** — re-enters idle sessions to continue or follow up, with depth and backoff guards so it never loops.
- **Background review + curator** — a cheap aux model reviews what worked every few turns and can distill a new skill; unused skills are retired automatically and revived when relevant again.
- **Parallel delegation** — `delegate_task` spawns least-privilege sub-agents for concurrent workstreams (no money/comms/code-exec, can't re-delegate), sync or detached in the background.

## 🧬 It learns and gets better with use

Run POLYROB as your personal agent (`POLYROB_LOCAL=true`) and it turns experience into durable
capability instead of forgetting it when the task ends.

- **Reflective, hierarchical memory** — findings are organized into phases, consolidated by an LLM, and forgotten by *importance* (`recency + relevance + frequency`), not just age.
- **Cross-session recall** — SQLite FTS5 keyword search out of the box (zero extra deps), or optional hybrid keyword+vector recall (`sqlite-vec`) that transparently degrades to keyword search if the extension can't load — the agent keeps working either way.
- **Episodic activity log** — a durable "what happened last time" ledger bridges each new session so it never starts cold.
- **Writes its own skills** — authored through a scanned, quarantined pipeline; every self-modification is reviewed before it takes effect, so a background turn can never silently rewrite a skill or the agent's identity.
- **Evolving identity** — a per-user self-doc the agent updates (owner-gated) as it learns how you work.

Skills use the open [agentskills.io](https://agentskills.io) `SKILL.md` format — the same format as
Claude Code, read straight from `~/.claude/skills/`. Install from a local folder, a GitHub repo, or
a `SKILL.md` URL: `polyrob skill install <spec>` threat-scans, quarantines, and waits for your
explicit approval. See **[docs/guide/skills.md](docs/guide/skills.md)**.

## 🛠️ A real toolbox

- **Web, two ways** — a lightweight `web_fetch` returns any URL as clean markdown with **no browser install needed**, or full **Playwright** automation (navigate/click/type/screenshot/DOM extract, anti-detection) when you need it.
- **MCP client** — connect any Model Context Protocol server over **STDIO, SSE, HTTP, or Streamable HTTP**, with auto tool-discovery, per-server circuit breakers, encrypted secrets, and live resource subscriptions.
- **Structured web data** — the AnySite tool pulls structured data from **200+ sites and platforms**; Perplexity-backed search; Twitter/X and Gmail tools.
- **Code & files** — file/CSV/JSON handling, built-in coding tools (`str_replace` / `apply_patch` / `run_tests` / grep), git & GitHub tools, and opt-in sandboxed code execution.
- **Vision** — reasons over screenshots and images.

## 🔌 Use it from anywhere

One agent core, many front doors:

- **Terminal** — run `polyrob` to talk to the agent: live tool transcripts, secret-scrubbed output, resumable sessions, and 20+ slash commands (`/compact`, `/usage`, `/memory`, `/model`, `/self`, `/replay`, `/autonomy`).
- **Web dashboard** — a real-time Socket.IO console: watch the agent work, browse its workspace, preview files and browser screenshots, inspect memory and the goal board.
- **REST API + SSE streaming**, plus a drop-in **OpenAI-compatible `/v1`** endpoint — point any OpenAI SDK at `localhost:9000/v1`.
- **A2A protocol** — Google's Agent-to-Agent standard (Agent Card discovery, JSON-RPC, SSE) so other agents can discover and delegate to yours.
- **Chat surfaces** — Telegram (live incremental streaming + voice-note transcription), email (IMAP/SMTP), WhatsApp, Discord, Slack, Signal, and X (Twitter) DMs.

## 🧠 Multi-provider intelligence

- **Six providers, one agent** — OpenAI, Anthropic, Google Gemini, DeepSeek, OpenRouter (Grok/Kimi/Qwen/GLM), NVIDIA NIM — behind a native LLM layer with **no third-party agent framework**.
- **Automatic failover** — a rate-limited or failing provider silently retries on a fallback.
- **Live model switching** — `/model <provider> <model>` mid-session, or per-request override on the `/v1` surface.
- **Cross-provider prompt caching** — cuts token cost on long sessions, with cached-token metrics surfaced per provider.
- **Optional extended thinking** — per-provider reasoning budgets (Anthropic thinking blocks, DeepSeek CoT, OpenAI reasoning effort), plus a scrubber that keeps leaked reasoning prose out of your history and output.

---

## Quick Start

POLYROB targets **Python 3.11+**.

```bash
# 1. Install (with all optional features)
pipx install "polyrob[all]"

# 2. Install the browser engine (for web automation)
python -m playwright install chromium

# 3. Configure (writes ~/.polyrob/.env)
polyrob init

# 4. Sanity-check your setup
polyrob doctor

# 5. Start it
polyrob
```

Run `polyrob` and you're talking to the agent. Give it a task in plain language and it plans, works,
and reports back; ask a follow-up and it keeps going. `polyrob doctor` is a real preflight — it
reports which provider keys resolve, the exact model it will pick, your active memory backend, and
workspace isolation.

**Turn on personal-agent mode** to unlock the self-evolving and goal-seeking loops (skills, curator,
goal board, self-wake, episodic memory):

```bash
# ~/.polyrob/.env  — single-user, on your own machine
POLYROB_LOCAL=true
```

<details>
<summary>Optional: web dashboard & REST API</summary>

```bash
polyrob dashboard   # single-user web UI  → http://localhost:5050
polyrob serve       # local REST API      → http://localhost:9000
polyrob gateway     # run all enabled chat surfaces (Telegram/email/…) in one process
```
</details>

For a full walkthrough see **[docs/guide/getting-started.md](docs/guide/getting-started.md)**.
Want to contribute? See **[CONTRIBUTING.md](CONTRIBUTING.md)**.

---

## A taste of what it can do

```
"Research competitors in the AI automation space and write a report
 with pricing, features, and positioning."

"Monitor this webpage daily and alert me when the price drops below $500."

"Log into my dashboard, export the monthly report, and email it to my team."

"Scrape these 50 product pages, extract pricing, and build a CSV."
```

More real-world examples → **[docs/examples.md](docs/examples.md)**

---

## The terminal, done right

Run `polyrob` and you're in the agent. From inside the session, slash commands give you full control:

| In-session command | What it does |
|---|---|
| `/compact` | LLM-compresses the context window (e.g. 8,432 → 3,210 tokens) mid-session |
| `/usage` | Authoritative token + cost accounting from the local DB |
| `/model <provider> <model>` | Hot-swap the model without losing the conversation |
| `/memory search <q>` | Search cross-session memory inline |
| `/self` | View the agent's SOUL (identity) and evolving SELF doc |
| `/replay` | Visually replay a past session |
| `/autonomy` | Inspect goals, cron, and the autonomy loops |

Named toolsets (`minimal · research · coding · browser · full · safe`) let you scope exactly what
the agent can touch. A handful of management subcommands round it out — `polyrob doctor` (preflight),
`polyrob kb add/search` (local knowledge base), and `polyrob update --apply` (self-update with
snapshot → guarded migrate → verify → **auto-rollback** on failure).

---

## Full capabilities

**Core automation**

| Capability | Detail |
|---|---|
| Web reading | `web_fetch` URL→markdown, no browser needed |
| Browser automation | Playwright with anti-detection, vision over screenshots |
| Files & data | text / JSON / CSV / markdown read + write |
| Code | built-in coding tools + git/GitHub + opt-in sandboxed execution |
| Planning | multi-step decomposition with automatic error recovery |

**Intelligence & memory**

| Capability | Detail |
|---|---|
| Cross-session recall | SQLite FTS5 (default) or hybrid vector (`sqlite-vec`), tenant-scoped |
| Reflective memory | phase-organized, LLM-consolidated, importance-based forgetting |
| Adaptive context | LLM-synthesis compaction in the 85–95% band, with anti-thrash cooldown |
| Prompt caching | per-provider, with cached-token cost metrics |
| Extended thinking | optional per-provider reasoning budgets + reasoning-prose scrubber |

**Autonomy** *(personal-agent mode)*

| Capability | Detail |
|---|---|
| Durable goal board | SQLite, atomic claims, circuit breaker, daily quota — survives restarts |
| Cron | natural-language schedules + out-of-band delivery (Telegram/email/X) |
| Self-wake | re-enters idle sessions with depth/backoff guards |
| Self-authored skills | background review → scan → quarantine → owner approval |
| Skill curator | retires unused skills, revives on reuse |
| Delegation | least-privilege sub-agents, sync or detached background |

**Interfaces & interop**

| Capability | Detail |
|---|---|
| Terminal | `polyrob` opens the agent — live tool transcripts, 20+ slash commands, resumable sessions |
| Web dashboard | real-time Socket.IO feed, file browser, live screenshots |
| REST API | session lifecycle + mid-run guidance injection + SSE streaming |
| OpenAI-compatible | drop-in `/v1/chat/completions` + `/v1/models` |
| A2A protocol | Agent Card discovery, JSON-RPC, SSE — agent-to-agent delegation |
| MCP client | STDIO / SSE / HTTP / Streamable HTTP, live resource subscriptions |
| Chat surfaces | Telegram, email, WhatsApp, Discord, Slack, Signal, X DMs — one agent core, many channels |

---

## Integrations

Multi-provider LLM (**OpenAI · Anthropic · Google Gemini · DeepSeek · OpenRouter · NVIDIA NIM**) ·
**Playwright** browser · **MCP** (STDIO/SSE/HTTP/Streamable) · **AnySite** (200+ sites) ·
**Perplexity** search · **Twitter/X** · **Gmail / IMAP-SMTP** · **Telegram** · **WhatsApp** ·
**Discord** · **Slack** · **Signal** ·
voice transcription (**faster-whisper**) · **A2A** · OpenAI-compatible `/v1` · **agentskills.io**
skills · **sqlite-vec** + **sentence-transformers** local RAG.

---

## Safe autonomy by design

An autonomous agent that touches the web, your files, and other people needs guardrails. POLYROB
treats untrusted input as data — never as commands — with the core protections **on by default**:

| Layer | Protection | Default |
|-------|------------|---------|
| **Untrusted-input wrapping** | Web pages, emails, and tool results framed as data, not instructions | **On** |
| **Least-privilege delegation** | Sub-agents get narrowed toolsets (no money/comms/code-exec), no re-delegation | **On** |
| **Schema sanitization** | Hostile tool-schema constructs fixed before they reach a provider | **On** |
| **SSRF confinement** | `web_fetch` re-validates every redirect hop; blocks loopback/metadata/private targets | **On** |
| **Self-modification review** | New skills / identity edits are quarantined and reviewed before taking effect | **On** |
| **Code-exec isolation** | Subprocess (or hardened Docker) isolation, no inherited API keys | On when code-exec enabled |
| **3-tier access** | OWNER / CORRESPONDENT / DENIED routing for chat surfaces | Opt-in (`CORRESPONDENT_ACCESS_ENABLED`) |
| **Memory threat-scan** | Rejects injected jailbreak/persona-rewrite patterns on write | Opt-in (`MEMORY_THREAT_SCAN`) |

When you expose the agent to other people, the **3-tier access model** keeps a stranger's message as
*data*: **OWNER** steers the agent · **CORRESPONDENT** (a third party the agent contacted) can only
return data, never command · **DENIED** is blocked. A correspondent-tainted session has high-impact
tools (money, comms, code-exec) gated off until a genuine owner turn.

> **Chat surfaces are off by default for a reason.** When you enable them, treat inbound DMs as
> untrusted input — use pairing/approval, and never expose public group chats without understanding
> the risks.

---

## Crypto & web3 *(optional, off by default)*

POLYROB can act as an economic agent when you want it to — all of it gated behind flags and off by
default:

- **x402 pay-per-request** — anonymous USDC micropayments (Base, Avalanche, IoTeX) via the Coinbase facilitator; the agent can both charge for its API and pay for external resources.
- **Native agent wallet** — an EOA wallet with per-transaction and rolling 24h spend caps; testnet by default.
- **ERC-8004 trustless agents** — optional on-chain agent identity + portable reputation.
- **SIWE** wallet auth for the multi-tenant posture.

> ⚠️ **Crypto features are unaudited.** The wallet, signing, and payment code (`crypto` extra) has
> **not** had any independent security audit. It ships **as-is with no warranty** and can lose
> funds. Enable it only at your own risk, and prefer testnets. See
> [SECURITY.md](SECURITY.md#crypto--wallet--payment-features).

---

## Configuration essentials

```bash
# ~/.polyrob/.env — set any providers you have keys for
OPENROUTER_API_KEY=sk-or-...   # recommended: one key reaches every model
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...
GEMINI_API_KEY=...

# Memory backend
MEMORY_BACKEND=sqlite          # keyword FTS5 (default, no extra deps)
# MEMORY_BACKEND=local_vector  # semantic vector recall (pip install "polyrob[memory-vector]")

# Personal-agent mode — turns on skills/curator/goal-board/self-wake as a group
POLYROB_LOCAL=true
```

POLYROB falls back across providers automatically on billing or rate-limit errors; switch
mid-session with `/model <provider> <model>` (or `<provider>/<model>`). Configuration guide →
**[docs/guide/configuration.md](docs/guide/configuration.md)** · full environment-flag reference
(SSOT) → **[docs/CONFIGURATION.md](docs/CONFIGURATION.md)**.

Everything crypto — the agent wallet, paying for resources (x402), getting paid
(invoicing, branded QR cards, on-chain settlement), watchtower subscriptions, ERC-8004
reputation, credits, and the trading tools — is documented end-to-end (all OFF by
default) in **[docs/guide/payments.md](docs/guide/payments.md)**.

---

## Installation options

```bash
pipx install "polyrob[all]"     # recommended — everything
pip install polyrob             # core agent + keyword memory + CLI  (~50 MB, zero cloud deps)
pip install "polyrob[browser]"  # add Playwright browser automation
pip install "polyrob[server]"   # add FastAPI REST API + WebView
```

| Extra | What it adds |
|-------|--------------|
| *(none)* | Core agent, keyword memory, CLI |
| `server` | FastAPI REST API + WebView (Socket.IO) |
| `browser` | Playwright browser automation |
| `memory-vector` | Semantic vector recall (sentence-transformers + sqlite-vec) |
| `crypto` | Web3, x402 pay-per-request, Hyperliquid |
| `telegram` | Telegram surface (aiogram) |
| `twitter` | Twitter/X integration (tweepy) |
| `voice` | Voice transcription (faster-whisper) |
| `dev` | Testing, linting, type-checking |
| `all` | Everything above |

### Self-hosting & deployment

Run it three ways, and the posture auto-derives from how you bind it: **`local`** (loopback, no
auth — the default), **`own_ops`** (public host with owner login; `--host 0.0.0.0` auto-upgrades to
this so you can't accidentally expose a no-auth dashboard), or **`multitenant`** (wallet/SIWE +
billing). One `docker compose up` builds the server + browser + vector memory and persists
memory/sessions/skills across restarts. See
**[docs/guide/self-hosting.md](docs/guide/self-hosting.md)**.

---

## For developers

```
polyrob/
├── agents/    # Task automation framework (agent loop, memory, skills, goals)
├── api/       # FastAPI HTTP + A2A + OpenAI /v1
├── cli/       # Terminal-native `polyrob` agent
├── core/      # DI, config, permissions, instance/identity, autonomy runtime
├── modules/   # LLM, Memory, Database, Auth, Credits
├── tools/     # Browser, MCP, Email, Twitter, code-exec
├── surfaces/  # Chat-surface adapters (Telegram, Email, WhatsApp, Discord, Slack, Signal, X)
├── cron/      # Durable scheduled runs + goal board
└── webview/   # Optional single-user web dashboard
```

```bash
git clone https://github.com/theselfruleorg/polyrob
cd polyrob
python -m venv venv && source venv/bin/activate
pip install -e ".[dev,all]"
python -m playwright install chromium

pytest tests/unit -q     # fast unit suite
ruff check .             # lint
```

Architecture overview → **[docs/guide/architecture.md](docs/guide/architecture.md)** · API reference
→ **[docs/guide/api.md](docs/guide/api.md)** · deep architecture & contributor guide →
**[AGENTS.md](AGENTS.md)** (every layer also has its own `README.md`)

---

## Documentation

| Document | Description |
|----------|-------------|
| [docs/guide/getting-started.md](docs/guide/getting-started.md) | Install & first run |
| [docs/guide/cli.md](docs/guide/cli.md) | Terminal agent commands |
| [docs/guide/skills.md](docs/guide/skills.md) | Skills — install, author, and manage |
| [docs/guide/api.md](docs/guide/api.md) | REST + A2A + OpenAI-compatible API |
| [docs/guide/configuration.md](docs/guide/configuration.md) | Configuration guide |
| [docs/guide/architecture.md](docs/guide/architecture.md) | Architecture overview |
| [docs/guide/self-hosting.md](docs/guide/self-hosting.md) | Self-hosting / deployment |
| [docs/guide/deployment-postures.md](docs/guide/deployment-postures.md) | Deployment postures (local / own_ops / multitenant) |
| [docs/guide/console.md](docs/guide/console.md) | Web dashboard — capabilities & payments |
| [docs/comparison.md](docs/comparison.md) | Comparison with other frameworks |
| [docs/examples.md](docs/examples.md) | Real-world usage examples |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Environment-flag reference (SSOT) |
| [AGENTS.md](AGENTS.md) | Deep architecture, invariants & contributor guide |
| [CHANGELOG.md](CHANGELOG.md) | Notable changes |

---

## Contributing & security

- **Contributions welcome** — see [CONTRIBUTING.md](CONTRIBUTING.md)
- **Report vulnerabilities** — see [SECURITY.md](SECURITY.md)
- **License** — MIT ([LICENSE](LICENSE)) · brand/name use in [TRADEMARK.md](TRADEMARK.md) · third-party attributions in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)

---

<div align="center">

**POLYROB** — by [The Selfrule Organization](https://theselfrule.org)

</div>
</content>
