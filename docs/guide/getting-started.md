# Getting Started with POLYROB

POLYROB is an open-source autonomous AI agent framework. The default instance is `rob`. This guide gets you from zero to running your first task in under five minutes.

---

## Table of Contents

- [New Here?](#new-here)
- [Quick Start](#quick-start)
- [Installation Options](#installation-options)
- [First Run Setup](#first-run-setup)
- [Running Your First Task](#running-your-first-task)
- [Interactive Chat](#interactive-chat)
- [Configuration](#configuration)
- [Where Data Lives](#where-data-lives)
- [Updating](#updating)
- [Troubleshooting](#troubleshooting)
- [Next Steps](#next-steps)

---

## New Here?

Welcome! POLYROB is a **self-hosted autonomous AI agent** that you run on your own machine. Think of it as:

- **A research assistant** — Scrape websites, extract data, compile reports
- **An automation tool** — Fill forms, navigate pages, run workflows
- **A code companion** — Analyze code, write tests, generate docs
- **A scheduler** — Run recurring tasks, monitor changes, deliver results

**What makes POLYROB different:**
- ✅ **Multi-provider** — OpenAI, Anthropic, Google, DeepSeek, OpenRouter, NVIDIA NIM
- ✅ **Automatic failover** — Switches providers if one has issues
- ✅ **Self-hosted** — Full control, no vendor lock-in
- ✅ **Persistent memory** — Remembers context across sessions
- ✅ **Durable autonomy** — Goals survive restarts

---

## Quick Start

Get running in 4 commands:

```bash
# 1. Install
pipx install "polyrob[all]"

# 2. Configure
polyrob init

# 3. Verify (catches a missing/malformed API key before your first run)
polyrob doctor

# 4. Run
polyrob run "Summarize https://example.com"
```

That's it! POLYROB fetches the page, reads the content, and summarizes it — using the
lightweight `web_fetch` tool, so no browser install is needed for this first task.
(Full browser automation is opt-in; see [Step 1 below](#step-1-install-browser-engine-optional).)

### Owner-paired install

`polyrob init` also pairs the instance to an **owner** — the identity autonomy and
self-evolution answer to. Interactively it asks for an instance id and owner user id
(both default `rob` for a single-user setup). To script it:

```bash
polyrob init --non-interactive --owner rob --instance-id rob --openai-key sk-...
```

`--owner` alone backfills the instance id (and vice-versa). The pairing is written to
`~/.polyrob/.env` as `POLYROB_OWNER_USER_ID` / `POLYROB_INSTANCE_ID`; `polyrob doctor`
reports it (`owner:` / `instance id:` lines).

---

## Installation Options

### Option A — pipx (Recommended)

**Best for:** End users who want a clean, isolated installation.

```bash
pipx install "polyrob[all]"
```

This installs POLYROB with all optional features:
- Browser automation (Playwright)
- Vector memory (sentence-transformers)
- Crypto/x402 (optional)
- Telegram surface
- REST API + WebView

### Option B — pip with Selected Extras

**Best for:** Users who want minimal installs or specific features.

```bash
# Core only (smallest - ~50MB)
pip install polyrob

# With browser automation
pip install "polyrob[browser]"

# With REST API + WebView
pip install "polyrob[server]"

# With vector memory
pip install "polyrob[memory-vector]"

# Mix and match
pip install "polyrob[browser,server]"
```

### Option C — Development Install

**Best for:** Contributors and developers.

```bash
git clone https://github.com/theselfruleorg/polyrob
cd polyrob
pip install -e ".[dev,all]"
```

### Extras Reference

| Extra | What It Adds | Size |
|-------|--------------|------|
| *(none)* | Core agent, keyword memory, CLI | ~50MB |
| `server` | FastAPI REST API + WebView | +20MB |
| `browser` | Playwright browser automation | +100MB |
| `memory-vector` | Semantic vector recall | +500MB |
| `crypto` | Web3, x402 pay-per-request | +10MB |
| `telegram` | Telegram surface | +5MB |
| `twitter` | Twitter/X integration | +5MB |
| `voice` | Voice transcription (faster-whisper) | +50MB |
| `dev` | Testing, linting, type-checking | +30MB |
| `all` | Everything above | ~700MB |

---

## First Run Setup

### Step 1: Install Browser Engine (Optional)

```bash
python -m playwright install chromium
# On a fresh Linux server, also pull the system libraries (needs sudo):
# python -m playwright install --with-deps chromium
```

**Why?** POLYROB's default web tool (`web_fetch`) is a lightweight, no-browser HTTP
fetch — it's what handles the Quick Start example above and most "read this page"
tasks. The heavier Playwright `browser` tool (clicking, filling forms, multi-step
navigation) needs the Chromium binary. This is a one-time download (~100MB).

**Skip this if:** You're not using interactive browser automation yet — you can
always run this later, whenever a task needs it.

### Step 2: Configure Your LLM Provider

POLYROB needs at least one LLM provider. The easiest way is `polyrob init`:

```bash
polyrob init
```

This creates `~/.polyrob/.env` and walks you through configuration.

**Minimum required — pick any one** (OpenRouter is recommended: one key reaches
every model, and `polyrob init` prompts for it first):

```bash
# ~/.polyrob/.env
OPENROUTER_API_KEY=sk-or-...   # recommended — one key, all models
# or
ANTHROPIC_API_KEY=sk-ant-...
# or
OPENAI_API_KEY=sk-...
# or
GEMINI_API_KEY=...
```

> DeepSeek has no standalone bootstrap path — its direct client is disabled
> (tool-calling is unreliable there). Use DeepSeek via
> `OPENROUTER_API_KEY` with a `deepseek/deepseek-chat` model instead.

**Skipped `polyrob init`?** You don't have to run it first. The first time you run
`polyrob run` or `polyrob chat` interactively with no usable provider key configured,
POLYROB runs the same OpenRouter-first key wizard inline — no separate step required.
Once a key is saved, it asks:

```
Key saved. Finish full setup now (model, persona, autonomy — ~1 min)? [y/N]
```

Accepting bridges straight into the full `polyrob init` wizard (model, toolset,
persona, owner pairing, autonomy guardrails, optional wallet) without re-prompting for
the key you just entered; declining leaves a one-line reminder that `polyrob init` is
available anytime. This inline wizard only fires on a real interactive terminal — it
never prompts (and never blocks) in CI, scripts, or a piped/non-interactive run.

### Step 3: Verify Setup

```bash
polyrob doctor
```

This checks:
- ✅ Which provider API keys are present and usable
- ✅ The provider/model `polyrob run` will actually resolve to
- ✅ The active memory backend + optional vector-search dependencies
- ✅ Workspace isolation and the `POLYROB_LOCAL` autonomy-flag footgun
- ✅ Skill library compliance

**Fix any issues before proceeding.**

---

## Running Your First Task

### Non-Interactive (One-Shot)

**Best for:** Scripts, automation, quick tasks.

```bash
polyrob run "summarize https://example.com"
```

POLYROB creates a session, runs the task, and prints the result.

### More Examples

```bash
# Research
polyrob run "Search for 'AI automation trends 2026' and summarize the top 5 results"

# Code analysis
polyrob run "Analyze the code in ./src/ for security issues"

# File operations
polyrob run "Create a markdown todo list from the items in items.txt"

# Multi-step
polyrob run "Go to producthunt.com, find top 3 AI tools, and create a comparison table"
```

### Interactive Mode

**Best for:** Exploratory work, iteration, collaboration.

```bash
polyrob chat
```

This opens a REPL (Read-Eval-Print Loop) where you can:

```
You: Research the latest Python 3.12 features
Agent: [Performs research, provides summary]
You: Focus on the performance improvements
Agent: [Filters and elaborates on performance]
You: Create a markdown summary
Agent: [Writes to file]
You: exit
```

See [Interactive Chat](#interactive-chat) below for the everyday slash commands, or [cli.md](cli.md#slash-commands-repl) for the full reference.

---

## Interactive Chat

The `polyrob chat` command opens a REPL with the following features:

### Features

| Feature | Description |
|---------|-------------|
| **Multiline editing** | Use `Shift+Enter` for new lines |
| **Session history** | Up/Down arrows for previous commands |
| **Tool transparency** | See what tools are being called |
| **Progress indicators** | Real-time status updates |
| **Auto-save** | Sessions saved automatically |

This is a curated subset for everyday use — see the [full slash-command reference](cli.md#slash-commands-repl) in the CLI docs for everything else.

```
/help                     — Show available commands
/exit                     — Leave the REPL (aliases: /quit, /q)
/clear                    — Clear history, keep the system prompt (start fresh)
/model <provider> <model> — Swap the model for this session and persist it as the default
/compact                  — Compact history via the LLM (alias: /compress)
/usage                    — Authoritative usage breakdown, tokens + cost (alias: /cost)
/memory [search <query>]  — Show the memory provider, or recall from cross-session memory
/skills [list|info <id>|install <spec>] — List, inspect, or install agent skills
/self                     — Show the instance identity (SOUL + SELF docs, read-only)
/kb [search <query>]      — List or search the local knowledge base
/goals                    — Show goals board summary
/autonomy                 — Show autonomy loops + scheduled cron jobs / open goals
/tools                    — List the agent's registered tools/actions
/sessions                 — List all known sessions
/replay <session>         — Replay a session's feed (visual history, not a re-attach; /resume is an alias)
```

### Example Session

The startup banner is deliberately quiet — two lines, never competing with your first
message:

```bash
$ polyrob chat
● polyrob v0.7.0 · claude-sonnet-4.5 (anthropic)
  session a1b2c3d4 · tools filesystem, task · /help · /session

You: I need to research quantum computing companies for an investment report.

Agent: I'll help you research quantum computing companies. Let me search for recent information...

→ Tool: search(query="quantum computing companies 2026 funding")
→ Found: 15 results

Agent: I found several key quantum computing companies. Let me organize this information...

[Research continues...]

You: Focus on the ones with Series C or later funding.

Agent: Filtering for Series C+ companies...

[Filtered results...]

You: Now create a markdown report with a comparison table.

Agent: Creating report...

→ Tool: write_file(path="quantum_companies_report.md", content="...")

✓ Wrote quantum_companies_report.md

You: /compress
Agent: Context compressed. Token count: 8,432 → 3,210

You: /usage
Agent: Session usage: 12,432 tokens ($0.15 estimated)

You: exit
Session saved to ./.polyrob/sessions/
```

---

## Configuration

### Where Configuration Lives

| Location | Purpose |
|----------|---------|
| `~/.polyrob/.env` | Global user configuration |
| `./.polyrob/.env` | Project-local overrides |
| `~/.polyrob/cli.json` | CLI preferences (default provider/model set via `model set-default`; `polyrob init` migrates old entries into `.env`) |
| `~/.rob/.env` | Legacy pre-rename home — read-only fallback, lowest precedence of the three `.env` layers above. `polyrob` migrates it into `~/.polyrob/` once, automatically, the first time it runs; you never need to touch it by hand. |
| `config/.env.{development,production}` | Source/server-install layer (git clone, not pipx) — read after the three layers above, so an explicit key there is only used when none of `~/.polyrob/.env`/`./.polyrob/.env`/the process env set it. |

Full precedence (highest wins): process env → `./.polyrob/.env` → `~/.polyrob/.env` →
`~/.rob/.env` (legacy) → root `.env` → `config/.env.{env}` → `config/.env.{env}.local`.

### View Configuration

```bash
polyrob config show    # View merged config (secrets redacted)
polyrob config path    # Show config file locations
```

### Set Default Model

```bash
# Interactive picker (recommended) — lists what your configured keys can reach
polyrob model set-default

# Or set it directly once you know the provider + model name
polyrob model list                     # see what's available
polyrob model set-default <provider> <model>
```

### Enable Features

```bash
# Edit ~/.polyrob/.env
POLYROB_LOCAL=true    # Enable safe autonomy features
GOALS_ENABLED=true    # Enable goal board
SKILLS_WRITABLE=true  # Allow skill creation
```

---

## Where Data Lives

### Directory Structure

```
~/.polyrob/               # Global POLYROB home
├── .env                   # User configuration
├── cli.json               # CLI preferences (e.g. default model)
└── sessions/              # Global sessions (rare)

./.polyrob/                # Project-local (default runtime data root)
├── .env                   # Project overrides
├── memory.db              # Memory store
├── goals.db               # Goal board
├── cron.db                # Scheduled runs
└── sessions/              # Project sessions
    ├── session-abc123/
    │   ├── workspace/     # Working files
    │   ├── screenshots/   # Screenshots
    │   ├── feed/          # Message feed
    │   └── logs/          # Session logs
    └── session-def456/
        └── ...
```

`./.polyrob/` is the default when no project-scoped manager overrides it (e.g. via
`POLYROB_PROJECT_DIR`/`POLYROB_DATA_DIR` — see [configuration.md](configuration.md)).

### Isolation

**Everything is local by default.** No data leaves your machine unless:
- You configure an external LLM provider (required for AI features)
- You enable external integrations (email, Telegram, etc.)

---

## Updating

```bash
polyrob update --check    # current vs latest (exit code 10 when an update exists)
polyrob update            # status + the exact update steps for YOUR install method
polyrob update --apply    # automated update (git/editable installs):
                          #   snapshot → install → migrate (guarded) → verify,
                          #   with automatic rollback on any failure
```

For pip/pipx installs, update through the package manager and then migrate (idempotent —
a no-op when already current):

```bash
pip install -U "polyrob[all]"        # or: pipx upgrade polyrob
python -m migrations.migrate upgrade
```

**Safety net:** every `--apply` first takes a WAL-safe snapshot of your databases,
config, and identity. If an update misbehaves:

```bash
polyrob update --list-snapshots   # see what you can roll back to
polyrob update --rollback         # restore the most recent full snapshot
```

---

## Troubleshooting

### Common Issues

#### Issue: "No API key found"

**Solution:** Configure at least one provider key:

```bash
polyrob init
# Or manually edit ~/.polyrob/.env
echo "OPENAI_API_KEY=sk-..." >> ~/.polyrob/.env
```

#### Issue: "Browser not available"

**Solution:** Install Playwright:

```bash
python -m playwright install chromium
# On Linux, if system libraries are missing (needs sudo):
# python -m playwright install --with-deps chromium
```

#### Issue: "Permission denied" errors

**Solution:** Check file permissions:

```bash
# Ensure POLYROB can write to sessions directory
chmod 755 ./.polyrob/
```

#### Issue: "Import error"

**Solution:** Ensure you installed with the correct extras:

```bash
pip install "polyrob[all]"  # Or specific extras you need
```

#### Issue: "Module not found" for vector memory

**Solution:** Install the memory-vector extra:

```bash
pip install "polyrob[memory-vector]"
```

### Get Help

```bash
polyrob doctor    # Run diagnostics
```

If issues persist:
1. Check [docs/CONFIGURATION.md](../CONFIGURATION.md)
2. Search [GitHub Issues](https://github.com/theselfruleorg/polyrob/issues)
3. Ask in [GitHub Discussions](https://github.com/theselfruleorg/polyrob/discussions)

---

## Platform-Specific Notes

### macOS

```bash
# pipx recommended
pipx install "polyrob[all]"
```

### Linux

```bash
# pipx recommended
pipx install "polyrob[all]"

# Ensure Python 3.11+
python3 --version
```

### Windows (PowerShell)

```bash
# pipx works on Windows too
pipx install "polyrob[all]"

# Or use pip in a virtual environment
python -m venv venv
.\venv\Scripts\activate
pip install "polyrob[all]"
```

---

## Next Steps

### Learn More

- [Configuration Guide](configuration.md) — All flags and options
- [CLI Reference](cli.md) — Complete command documentation
- [API Guide](api.md) — REST API and A2A protocol
- [Architecture](architecture.md) — How POLYROB works
- [Examples](../examples.md) — Real-world usage examples

### Explore Features

- **Memory System** — Cross-session recall
- **Goal Board** — Durable task scheduling
- **Delegation** — Parallel sub-agents
- **Skills** — Reusable agent capabilities
- **Surfaces** — Telegram, Email, API, Web

### Join the Community

- [GitHub Discussions](https://github.com/theselfruleorg/polyrob/discussions)
- [GitHub Issues](https://github.com/theselfruleorg/polyrob/issues)
- [Contributing](../../CONTRIBUTING.md)

---

**Ready to dive deeper?** Check out the [examples](../examples.md) for real-world use cases, or read the [comparison](../comparison.md) to see how POLYROB stacks up against other agent frameworks.
