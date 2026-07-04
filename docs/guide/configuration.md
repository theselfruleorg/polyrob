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
| `POLYROB_LOCAL` | `false` | Set to `true` for single-user / local mode. Enables safe autonomy features (writable skills, goals, background review, curator, insights) as a group. Per-flag values still override. |
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

## Advanced / autonomy flags

These are all `false` / conservative by default except where noted. Setting `POLYROB_LOCAL=true` enables the safe subset as a group.

| Variable | Default | Description |
|----------|---------|-------------|
| `CRON_ENABLED` | `false` | Enable the cron scheduler (runs scheduled tasks). |
| `CRON_DELIVERY_ENABLED` | `false` | Deliver cron results out-of-band (telegram/email/twitter). Needs a recipient (see below). |
| `POLYROB_OWNER_EMAIL` / `BOT_OWNER_EMAIL` | unset | Owner email address for cron **email** delivery on single-owner deploys (used when no `user_directory` service is registered). Telegram delivery uses `POLYROB_OWNER_TELEGRAM_ID`. |
| `GOALS_ENABLED` | `false` | Enable the durable goal board (background goal pursuit). |
| `SKILLS_WRITABLE` | `false` | Allow the agent to create and edit skills. |
| `BACKGROUND_REVIEW_ENABLED` | `false` | Enable periodic background aux-model review (fires every N productive turns). |
| `CURATOR_ENABLED` | `false` | Enable the skill curator (archives stale authored skills). |
| `SELF_WAKE_ENABLED` | `false` | Allow the agent to re-enter idle sessions autonomously. |
| `CODE_EXEC_ENABLED` | `false` | Enable local code execution (subprocess). **Not sandboxed — single-user only.** |
| `THINKING_CONFIG_ENABLED` | `false` | Enable extended reasoning tokens (Claude, DeepSeek, OpenAI reasoning models). |
| `PROJECT_CONTEXT_AUTOLOAD` | `false` | Auto-load a per-repo context file (`polyrob.md` > `POLYROB.md` > `AGENTS.md` > `CLAUDE.md` > `.cursorrules`, highest-precedence name wins, not concatenated) as steering context. Use `polyrob.md` to give POLYROB per-repo guidance without touching the file your other coding agents read. |
