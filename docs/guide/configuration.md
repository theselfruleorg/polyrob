# Configuration

polyrob is configured through environment files. Legacy CLI preferences may
exist in `~/.polyrob/cli.json`; `polyrob init` migrates default provider/model
values into `~/.polyrob/.env`.

> This page is the practical getting-started guide. For the **complete
> environment-flag reference** (every flag, default, and code anchor), see
> [../CONFIGURATION.md](../CONFIGURATION.md) — it is the single source of truth.

---

## Environment file

The recommended path is:

```bash
polyrob init
```

This writes `~/.polyrob/.env` and creates `./.polyrob/sessions`. You can also
write project-local overrides in `./.polyrob/.env` or set variables in your shell;
shell values override file values.

---

## LLM provider keys

Set **at least one** provider key. polyrob automatically selects the available provider and falls back to alternatives on error.

| Variable | Provider |
|----------|----------|
| `ANTHROPIC_API_KEY` | Anthropic (Claude) |
| `OPENAI_API_KEY` | OpenAI (GPT-5.x, o-series) |
| `GEMINI_API_KEY` | Google Gemini |
| `DEEPSEEK_API_KEY` | DeepSeek. Its direct client doesn't support tool-calling and isn't auto-selected — pass `-p deepseek` explicitly, or reach DeepSeek via `OPENROUTER_API_KEY` with model `deepseek/deepseek-chat`. |
| `OPENROUTER_API_KEY` | OpenRouter (proxies many models) |
| `NVIDIA_API_KEY` | NVIDIA NIM |
| `PERPLEXITY_API_KEY` | Perplexity — a web-search *tool*, not an LLM provider (optional) |

---

## Core feature flags

| Variable | Default | Description |
|----------|---------|-------------|
| `POLYROB_INSTANCE_ID` | `rob` | Which instance to run. See [instances.md](instances.md). |
| `POLYROB_LOCAL` | `false` | Set to `true` for single-user / local mode. Enables the **interactive** local tools (coding, git, knowledge base, memory/RAG, project-context) as a group. The self-directed **autonomy** loops (goals, self-wake, writable skills, curator, insights, …) are OFF by default and additionally need `AUTONOMY_ENABLED` — see [Autonomy](#autonomy). Per-flag values still override. |
| `AUTONOMY_ENABLED` | `false` | Master switch for the self-directed autonomy loops. OFF by default for a new install (the agent is interactive-only). ON automatically when you set an autonomous posture/mode (`AUTONOMY_MODE=autonomous`, or `AUTONOMY_POSTURE` owner-visible/full). See [Autonomy](#autonomy). |
| `MEMORY_BACKEND` | `sqlite` (**`local_vector` under `POLYROB_LOCAL`**) | Memory backend: `sqlite` (keyword FTS), `local_vector` (FTS + vector), `none` / `off` (disabled). |
| `MEMORY_REQUIRE_USER_ID` | `true` | When `true`, memory read/write is refused for anonymous sessions. Set `false` for single-user local installs to use a shared bucket. |
| `SUB_AGENTS_ENABLED` | `true` | Allow the agent to spawn sub-agents for parallel work. |

---

## Memory: sqlite-vec and the apsw note

Keyword memory (FTS) works out of the box with no extra dependencies.

**Vector / semantic recall** requires two things:

1. Install the `memory-vector` extra:
   ```bash
   pip install "polyrob[memory-vector]"
   ```
   This pulls in `sentence-transformers` (used to generate embeddings).

2. The `sqlite-vec` extension must be loadable at runtime. `apsw` and `sqlite-vec`
   ship as base dependencies of polyrob, so this is usually already satisfied —
   the caveat below covers the platforms where it isn't.

**Important caveat:** Python's standard-library `sqlite3` is often compiled **without** extension-loading support, which is why polyrob uses `apsw` (Another Python SQLite Wrapper) to load the `sqlite-vec` extension. If `apsw` is unavailable or the extension file cannot be found, polyrob logs a warning and **transparently degrades to keyword-only FTS** — the agent still works, recall quality is just lower.

To enable vector recall, set:
```
MEMORY_BACKEND=local_vector
```

This is already the default under `POLYROB_LOCAL=true` — you only need to set it
explicitly for a headless/server install that wants vector recall.

If you see a log warning like `sqlite-vec extension unavailable ... Falling back to FTS5 keyword recall`, the agent is still operational; only semantic similarity search is unavailable.

---

## CLI config

```bash
polyrob config show                            # view merged config, with secrets redacted
polyrob config path                            # show project/global config file locations
polyrob config set KEY VALUE                   # write to ./.polyrob/.env (add --global for ~/.polyrob/.env)
polyrob init                                   # interactive first-run setup (writes ~/.polyrob/.env)
polyrob model set-default                      # interactive model picker
polyrob model set-default <provider> <model>   # persist a specific default — see `polyrob model list`
```

Run `polyrob doctor` any time to check that your configuration resolved the way
you expect (provider, model, memory backend). See [cli.md](cli.md#polyrob-doctor).

Global config is stored at `~/.polyrob/.env`. Project overrides live at
`./.polyrob/.env`. Legacy `~/.polyrob/cli.json` may exist for older CLI
preferences and is migrated by `polyrob init` when possible.

Example:
```dotenv
DEFAULT_PROVIDER=anthropic
DEFAULT_MODEL=<model-name>          # run `polyrob model list` to see available models
POLYROB_AGENT_TOOLSET=coding
```

Legacy note: installs from before the project was renamed from `rob` to
`polyrob` used `~/.rob` as the config home (including its `cli.json`).
polyrob copies `~/.rob` to `~/.polyrob` automatically the first time you run
a local-mode command, if `~/.polyrob` doesn't already exist — no manual
migration needed.

---

## Autonomy

**Autonomy is OFF by default for new installs.** Out of the box the agent is
*interactive* — it acts on your messages and nothing else. The self-directed loops —
**self-wake** (re-entering an idle session on its own), the **goal board + planner**
(pursuing background goals across sessions), the **curator** and **background review**,
**episodic continuity**, and **self-writing** (editing its own skills / identity) — only
run once you opt in.

Turn them on with the single master switch:

```bash
AUTONOMY_ENABLED=true        # or run `polyrob init` and answer "yes" to the autonomy prompt
```

`AUTONOMY_ENABLED` also flips ON automatically when you choose an autonomous posture or
mode (`AUTONOMY_MODE=autonomous`, or `AUTONOMY_POSTURE` set to `owner-visible`/`full`), so
a deliberate autonomy choice is never left inert. On the local CLI the loops need **both**
`POLYROB_LOCAL` (the local profile) and `AUTONOMY_ENABLED`; a multi-tenant server keeps
every loop OFF unless you enable it explicitly. An explicit per-flag value always wins.

Check the current state anytime with `polyrob doctor` (the `autonomy:` line) or `/autonomy`
in the chat REPL.

## Advanced / autonomy flags

These are all `false` / conservative by default except where noted. Setting `POLYROB_LOCAL=true`
enables the **interactive** subset as a group; the self-directed autonomy loops in this table
additionally require `AUTONOMY_ENABLED` (see [Autonomy](#autonomy) above).

| Variable | Default | Description |
|----------|---------|-------------|
| `AUTONOMY_ENABLED` | `false` (**ON under `AUTONOMY_MODE=autonomous` / `AUTONOMY_POSTURE` owner-visible/full**) | Master switch for the self-directed autonomy loop group below. |
| `CRON_ENABLED` | `false` | Enable the cron scheduler (runs scheduled tasks). |
| `CRON_DELIVERY_ENABLED` | `false` | Deliver cron results out-of-band (telegram/email/twitter). Needs a recipient (see below). |
| `POLYROB_OWNER_EMAIL` / `BOT_OWNER_EMAIL` | unset | Owner email address for cron **email** delivery on single-owner deploys (used when no `user_directory` service is registered). Telegram delivery uses `POLYROB_OWNER_TELEGRAM_ID`. |
| `GOALS_ENABLED` | `false` | Enable the durable goal board (background goal pursuit). Goals can depend on other goals — the agent's `goal_create` tool takes `depends_on`, and a goal waits for its prerequisites before running. |
| `GOAL_BLOCKED_PROVIDER_RETRY_MIN` | `30` | Minutes before a goal blocked by a *transient* provider outage self-heals back to `ready` (a goal blocked because it `needs_input`, or because a prerequisite failed, is never auto-requeued — only the owner or the dependency sweep clears those). |
| `SKILLS_WRITABLE` | `false` | Allow the agent to create and edit skills. |
| `BACKGROUND_REVIEW_ENABLED` | `false` | Enable periodic background aux-model review (fires every N productive turns). |
| `CURATOR_ENABLED` | `false` | Enable the skill curator (archives stale authored skills). |
| `SELF_WAKE_ENABLED` | `false` | Allow the agent to re-enter idle sessions autonomously. |
| `CODE_EXEC_ENABLED` | `false` | Enable local code execution (subprocess). **Not sandboxed — single-user only.** |
| `THINKING_CONFIG_ENABLED` | `false` | Enable extended reasoning tokens (Claude, DeepSeek, OpenAI reasoning models). |
| `PROJECT_CONTEXT_AUTOLOAD` | `false` | Auto-load a per-repo context file (`polyrob.md` > `POLYROB.md` > `AGENTS.md` > `CLAUDE.md` > `.cursorrules`, highest-precedence name wins, not concatenated) as steering context. Use `polyrob.md` to give POLYROB per-repo guidance without touching the file your other coding agents read. |

---

## Run budget (spend ceiling)

| Variable | Default | Description |
|----------|---------|-------------|
| `RUN_BUDGET_USD` | `0` (off) | A per-session dollar ceiling. When set above `0`, a run **halts honestly** the moment its summed real provider cost reaches the cap — reported as a stopped run with a budget marker, never a fabricated "completed". The cap counts real provider cost (not the marked-up user price); sub-agents share the parent's budget. Anonymous sessions with no database are ungated (fail-open). |

The remaining budget is surfaced in the agent's environment block, so the model can
see how much room it has left and pace itself.

---

## Reliability defaults (on by default)

These two are **on by default** because they're pure hygiene — set them to `false`
only if you have a specific reason.

| Variable | Default | Description |
|----------|---------|-------------|
| `DEAD_TARGET_REGISTRY` | `true` | Skip outbound sends to provably-dead targets (a chat you've been blocked from, or a deleted conversation) and auto-revive the target the next time it messages you. Only definitively-classified failures are suppressed; ambiguous errors are unaffected. |
| `COMPACTION_PROMPT_GUARD` | `true` | Frame the context-compaction summarizer prompt (and the prior-summary block it rebuilds) so adversarial text captured earlier in a long conversation can't hijack the summarization step. |

---

## Code execution

Code execution is **off by default** and never in the default toolset. When you
enable it, choose a backend that matches your trust boundary.

| Variable | Default | Description |
|----------|---------|-------------|
| `CODE_EXEC_ENABLED` | `false` | Enable the `run_code` tool. |
| `CODE_EXEC_BACKEND` | `local_subprocess` | `local_subprocess` (a plain host subprocess — a convenience, **not a sandbox**; single-user/local only), `docker` (hardened container — the multi-tenant/server choice), or `ssh` (runs on a remote host). |
| `CODE_EXEC_SSH_HOST` / `_USER` / `_PORT` / `_KEY` | unset / unset / `22` / unset | Target for `CODE_EXEC_BACKEND=ssh`, run over your system `ssh` binary. `HOST` is required when `ssh` is selected. |
| `CODE_EXEC_SSH_SANDBOXED` | `false` | Operator attestation that the SSH host is hardened/disposable. By default the `ssh` backend is honestly reported as **not** a sandbox (agent code runs with the SSH user's full privileges), so a server refuses it unless you attest here. |

On a server (`POLYROB_LOCAL` unset), a non-sandboxed backend is refused by
construction — see [security-model.md](security-model.md) and
[`tools/code_exec/SANDBOX_SECURITY.md`](../../tools/code_exec/SANDBOX_SECURITY.md).

---

## MCP: outbound client and inbound server

polyrob is both an MCP *client* (it reaches out to external MCP servers) and,
optionally, an MCP *server* (other tools reach in to it).

| Variable | Default | Description |
|----------|---------|-------------|
| `MCP_ENABLED` | `false` | Enable the outbound MCP client — connect the `mcp` tool to external MCP servers configured in `config/mcp_config.json` (STDIO / SSE / HTTP / Streamable HTTP). |
| `MCP_SERVE_ENABLED` | `false` | Mount the **inbound** MCP server surface at `POST /mcp`, so an MCP client (Claude Desktop, Cursor) can connect to polyrob as a tool provider. v1 is read-only and exposes five tenant-scoped tools; auth is the same `X-API-KEY` / bearer-JWT / x402 policy as the REST/A2A surface. See [api.md](api.md#mcp-server-inbound). |
| `MCP_OAUTH_ENABLED` | `false` | For outbound connections only: inject and refresh OAuth `Authorization` headers on SSE/HTTP MCP servers whose config declares an `auth` block. Forward-looking scaffolding — no shipped server declares `auth` yet. |
