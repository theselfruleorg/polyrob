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

Set **at least one** provider key. When you don't pin a provider, polyrob auto-selects the
**first provider with a usable key in canonical order** — `openrouter → anthropic → openai →
gemini → nvidia → deepseek`, then your `providers.yaml` rows in file order — and falls back to
alternatives on error. (So adding an `OPENROUTER_API_KEY` to a box that used Anthropic changes the
default to OpenRouter; pin with `DEFAULT_PROVIDER=<name>` or `-p` to override. Full precedence:
[CONFIGURATION.md](../CONFIGURATION.md).)

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

## Subscription plans (flat-rate providers)

Some plans hand you an **API key** instead of metering you per token. POLYROB
ships provider rows for these, so all you do is set the key — no
`providers.yaml` needed. `polyrob init` names them rather than prompting for
them (nobody without the plan can answer), so set the key directly:

```bash
polyrob config set OLLAMA_API_KEY      # prompts; stays out of shell history
polyrob run -p ollama-cloud "…"
```

### Providers that take an API key

Set the key and go — `polyrob run -p <provider> "…"`. Run `polyrob model list`
for the live table.

| Provider | Key | Endpoint |
|----------|-----|----------|
| `openrouter`, `anthropic`, `openai`, `gemini`, `nvidia`, `deepseek` | the six originals | — |
| `ollama-cloud` | `OLLAMA_API_KEY` | `https://ollama.com/v1` |
| `zai` | `GLM_API_KEY` (or `Z_AI_API_KEY`) | `https://api.z.ai/api/paas/v4` |
| `zai-coding` | `ZAI_API_KEY` | `https://api.z.ai/api/anthropic` |
| `cerebras` | `CEREBRAS_API_KEY` | `https://api.cerebras.ai/v1` |
| `moonshot` / `moonshot-cn` | `KIMI_API_KEY` / `KIMI_CN_API_KEY` | `api.moonshot.ai/v1` |
| `kimi-coding` | `KIMI_CODING_API_KEY` | `https://api.kimi.com/coding` |
| `minimax` / `minimax-cn` | `MINIMAX_API_KEY` / `MINIMAX_CN_API_KEY` | `api.minimax.io/anthropic` |
| `xai` | `XAI_API_KEY` | `https://api.x.ai/v1` |
| `dashscope` | `DASHSCOPE_API_KEY` | DashScope compatible-mode |
| `alibaba-coding` | `ALIBABA_CODING_PLAN_API_KEY` | `coding-intl.dashscope.aliyuncs.com/v1` |
| `stepfun` | `STEPFUN_API_KEY` | `api.stepfun.ai/step_plan/v1` |
| `ai-gateway` (Vercel) | `AI_GATEWAY_API_KEY` | `ai-gateway.vercel.sh/v1` |
| `opencode-zen` / `opencode-go` | `OPENCODE_ZEN_API_KEY` / `OPENCODE_GO_API_KEY` | `opencode.ai/zen…` |
| `kilocode` | `KILOCODE_API_KEY` | `api.kilo.ai/api/gateway` |
| `huggingface` | `HF_TOKEN` | `router.huggingface.co/v1` |
| `xiaomi` | `XIAOMI_API_KEY` | `api.xiaomimimo.com/v1` |
| `tencent-tokenhub` | `TOKENHUB_API_KEY` | `tokenhub.tencentmaas.com/v1` |
| `copilot` | `COPILOT_GITHUB_TOKEN` | `api.githubcopilot.com` |

Flat-rate among these: `ollama-cloud`, `zai-coding`, `kimi-coding`,
`alibaba-coding`, `copilot`. Cerebras serves both plan types on one endpoint, so
it defaults to metered — see `subscription:` below.

Every provider also takes a `*_BASE_URL` override (e.g. `GLM_BASE_URL`) for a
proxy or a regional endpoint.

### Providers that need you to sign in (OAuth)

Some plans issue no API key at all. `polyrob auth add <provider>` runs their
OAuth flow:

| Provider | Plan |
|----------|------|
| `anthropic-oauth` | Claude Pro / Max |
| `openai-codex` | ChatGPT plan (Codex) |
| `github-copilot` | GitHub Copilot |
| `xai-oauth` | SuperGrok / X Premium+ |
| `qwen-oauth` | Qwen portal |
| `minimax-oauth` | MiniMax |

```bash
polyrob config set LLM_OAUTH_ENABLED true   # OFF by default, everywhere
polyrob auth add anthropic-oauth            # prints the ToS note, then the flow
polyrob auth status
```

> **⚠ Read this before connecting one.** These plans issue no OAuth `client_id`
> to third-party applications. The only ids that work are the ones published in
> the vendors' own CLIs (Claude Code, Codex CLI, VS Code, Grok CLI, Qwen CLI),
> so connecting authenticates POLYROB **as that client**. Every open-source
> agent offering "sign in with your Claude/ChatGPT plan" does the same thing,
> but it is a genuine terms-of-service exposure and it lands on **your**
> account, not ours. `polyrob auth add` states this and asks before doing
> anything, and the feature is off by default. Your account, your call.

Notes:

- **`ollama-cloud` is not a local Ollama.** A local server stays a keyless
  `ollama` row in `providers.yaml` (`auth_type: none`, `http://127.0.0.1:11434/v1`).
  Two different products from the same vendor.
- These rows are **appended after** the six built-ins and are excluded from
  automatic failover, so adding them cannot change which provider an existing
  install resolves to.
- **Flat-rate means $0 per-token cost.** A row marked `subscription: true`
  records no marginal API cost in `usage_records` — multiplying tokens by a
  price does not describe any charge you actually incur. Cerebras defaults to
  metered because one endpoint serves both plans; on Code Pro/Max, declare it:

  ```yaml
  providers:
    cerebras:
      subscription: true
  ```
- **Not yet live-verified.** These rows are declared from vendor
  documentation; we have no seat on each plan to run the tool-executing
  verification (proposal 024 §8) against. A wrong model id or endpoint surfaces
  as a provider 4xx, not a wrong answer. Reports welcome.
### Declaring your own OAuth provider

Beyond the built-in seats above, any OAuth provider can be declared in
`providers.yaml` — the flows are driven entirely by this data:

```yaml
providers:
  myplan:
    base_url: https://api.example.com/v1
    transport: chat_completions
    models: [some-model]
    oauth:
      auth_url: https://example.com/oauth/device   # device grant → the DEVICE endpoint
      token_url: https://example.com/oauth/token
      client_id: your-client-id
      scopes: [inference]
      grant: device_code            # or authorization_code
      redirect_mode: manual         # 'manual' (paste the code) or 'loopback'
      device_auth_style: form       # 'json' if the device leg wants JSON
      refresh_skew_sec: 120         # refresh this long before expiry
```

Three redirect shapes are supported, and which you can use depends on where the
agent runs:

| Flow | Works headless / over SSH? | Notes |
|------|---------------------------|-------|
| `device_code` | ✅ | Prints a URL + short code; polls for the token. The default. |
| `authorization_code` + `redirect_mode: manual` | ✅ | Provider shows the code on its own page; you paste it back. |
| `authorization_code` + `redirect_mode: loopback` | ❌ | Opens a `127.0.0.1` listener — **refused unless `POLYROB_LOCAL`**, since a server must never open a redirect listener. |

Both OAuth endpoints must be `https` and are refused otherwise (a token would
cross the network in plaintext).

---

## Custom LLM providers (`providers.yaml`)

Beyond the built-in providers, you can declare **any OpenAI-compatible or
Anthropic-compatible endpoint with zero code** — a local Ollama / LM Studio /
vLLM / llama.cpp server, an aggregator (Groq, Together, Fireworks, LiteLLM), or
a corporate gateway. Declare it in `~/.polyrob/providers.yaml` (path override:
`LLM_CUSTOM_PROVIDERS`):

```yaml
providers:
  ollama:
    base_url: http://127.0.0.1:11434/v1
    auth_type: none                    # no key needed
    transport: chat_completions
    default_model: qwen3-coder:30b
    models: [qwen3-coder:30b, llama3.3:70b]
  mygateway:
    base_url: https://gateway.corp.internal/anthropic
    env_key: MYGATEWAY_API_KEY         # must be *_API_KEY-shaped
    transport: anthropic_messages      # Anthropic-shaped endpoint
    bearer_auth: true                  # Authorization: Bearer, not x-api-key
    default_model: glm-5
    models: [glm-5]
```

> The z.ai GLM Coding Plan used to be the worked example here. It now ships as
> the built-in `zai-coding` row — an existing hand-written row still works and
> simply overrides the built-in.

Then use it like any built-in: `polyrob run -p ollama "…"`, or make it the
default with `DEFAULT_PROVIDER=ollama`. A row can also **override a built-in**
(e.g. give `openai:` a corporate-gateway `base_url`).

Rules worth knowing:

- **`transport`** selects the wire shape: `chat_completions` (OpenAI-style) or
  `anthropic_messages`. A row can never name a client class — only a transport.
- **Declare `models:`** — declared models become listable and route correctly
  through the OpenAI-compat `/v1` surface. A row without `models:` still works,
  but only when named explicitly (`-p <name> -m <model>`).
- **`subscription: true`** marks a flat-rate plan, so per-token cost accounting
  is skipped for it (see [Subscription plans](#subscription-plans-flat-rate-providers)).
- **`prompt_in_init: false`** keeps `polyrob init` from asking for that row's
  key — useful for a plan-specific row that only you have.
- **Security is enforced at load**: `env_key` must be an `*_API_KEY`-shaped
  variable (never a wallet/telegram/JWT secret name), `base_url` must be
  http(s) and never a cloud-metadata endpoint, and a group/world-writable file
  is refused. Every loaded row is logged with its effective endpoint, so a
  redirect is always visible.
- **The file is credential-equivalent** (it decides where your prompts and keys
  go): agent file tools are denied access to it, and it is not writable from
  the webview console. Keep it under `~/.polyrob/` with mode `600`.
- (The `LLM_PROVIDER_REGISTRY` kill-switch was removed on 2026-08-29, two releases after it shipped; the registry is the only path. It used to restore the legacy built-in-only
  provider tables and ignores the file entirely.

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
polyrob config set KEY                         # omit VALUE → prompted, hidden for secrets (no shell history)
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
