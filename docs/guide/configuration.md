# Configuring the agent

This page explains how POLYROB is configured and walks you through the settings
you will actually touch. It does not list every flag. The complete reference —
every environment flag, its default, and the code that reads it — is
[`docs/CONFIGURATION.md`](../CONFIGURATION.md). When this page and that file
disagree, that file wins; it is generated from the code and contract-tested.

Read this page top to bottom once. After that, `polyrob doctor` and
`polyrob config explain KEY` answer most questions faster than any document.

---

## 1. How configuration works

POLYROB has three kinds of settings. Each has one home and one command.

| Kind | Example | Where it lives | Set it with |
|---|---|---|---|
| **Secret** (an API key or token) | `ANTHROPIC_API_KEY` | `~/.polyrob/.env` | `polyrob config set ANTHROPIC_API_KEY` (prompts, hidden) |
| **Environment flag** (a switch or number the process reads at start) | `AUTONOMY_ENABLED`, `MEMORY_BACKEND` | `./.polyrob/.env` (this project) or `~/.polyrob/.env` (`--global`) | `polyrob config set KEY VALUE [--global]` |
| **Preference** (an owner setting the running agent reads live) | `style.verbosity`, `goals.daily_quota`, `chat.mode` | `<data_home>/identity/{instance_id}/user_{uid}/preferences.toml` | `polyrob config set style.verbosity brief` |

`polyrob config set` routes by the shape of the key: a secret-shaped name goes
to the **global** env file (so it never vanishes when you `cd` away), a dotted
name that is a known preference goes to `preferences.toml`, and a name in the
flag catalog goes to the **project** env file unless you pass `--global`. Pass
`--project` to keep a secret per directory. Every write tells you which file it
wrote and when the value applies (`live`, `next-turn`, `next-session`, or `restart`).

An **environment flag is read when the process starts**, so a change needs a
restart of whatever is running (the REPL, `polyrob serve`, a surface, a systemd
unit). Two of them are stricter still: `AGENT_COMPUTE_POSTURE` and the approval
flags (`PAYMENT_APPROVAL_MODE`, `APPROVAL_GRANT_TTL_HOURS`,
`PAYMENT_APPROVAL_TIMEOUT_SEC`) are frozen when the module first imports, so
changing them inside a live process is ignored on purpose — a running agent
cannot widen its own host access or approval lane. A **preference** needs no
restart at all.

### Files and precedence

On the CLI (local mode) the first value found wins, in this order:

1. your shell environment,
2. `./.polyrob/.env` — project overrides (`polyrob config set`),
3. `~/.polyrob/.env` — your global config (`polyrob config set --global`, `polyrob auth add`, `polyrob init`),
4. the legacy `~/.rob/.env` (read only; `polyrob` copies `~/.rob` to `~/.polyrob` once, automatically),
5. a repo-root `.env`, then `config/.env.<env>` and `config/.env.<env>.local` (dev checkouts only).

A server started with `polyrob serve` or `python main.py` loads the `config/`
files last and lets them win. A systemd deploy reads its environment from the
unit file (for example `/etc/polyrob/polyrob.env`) and never touches `config/`.

```bash
polyrob config path        # the ENV files this process reads, highest precedence first
polyrob config show        # the merged result, secrets redacted
polyrob config explain KEY # every layer that sets KEY, and which one won
```

`config path` lists env files only. To validate your preferences file as well,
name the tenant: `polyrob config check --user <uid>`.

There are two environment names, `development` (default) and `production`,
resolved from `CONFIG_ENV`, then `ENV`. There is no staging.

### A named profile is a whole separate home

`polyrob -P scout …` (or `POLYROB_PROFILE=scout`) runs the agent from
`~/.polyrob/profiles/scout/`: its own `.env`, characters, skills, memory,
goals, sessions. Everything on this page applies per profile. See
[profiles.md](profiles.md).

### Check what you did

```bash
polyrob doctor                     # health first: providers, memory, autonomy, pauses
polyrob doctor --flags --changed   # only the flags you set away from their default
polyrob doctor --flags --group memory
polyrob config check               # validate the env files against the catalog
polyrob config search "quiet"      # find a setting by name or description
```

`doctor --changed` is the fastest way to answer "what did I change on this box".

---

## 2. First run

```bash
polyrob init
```

The wizard connects a provider key, picks a default model, chooses a starter
template (`general`, `research`, `coding`, `social`, `trading`, `blank`) and
writes `~/.polyrob/.env`. Scripts use `polyrob init --no-prompt` with `--anthropic-key`
/ `--openai-key`, `--default-provider`, `--default-model`, `--toolset`, `--template`.

Two options matter beyond keys:

- `--owner <user id>` binds the instance to you as its owner. Owner binding is
  what lets chat surfaces tell you apart from strangers (§7).
- `--instance-id <name>` names the instance; the default is `polyrob`. The
  instance id is the tenant key for memory, goals and identity. Change it only
  before the first real session.

To give the instance its own character at init time use `--character <slug>`
or `--character-from <name>`; see §8.

---

## 3. Providers and models

### Keys

Set at least one key. With no provider pinned, POLYROB picks the **first
provider with a usable key** in this order: `openrouter`, `anthropic`,
`openai`, `gemini`, `nvidia`, then the rows of your `providers.yaml` in file
order. It fails over to the next usable provider on a billing, quota or
rate-limit error.

```bash
polyrob config set OPENROUTER_API_KEY     # one key that reaches most models
polyrob config set ANTHROPIC_API_KEY
polyrob model list                        # every provider: key present, models, default
```

Adding a key can change the auto-selected default (a new `OPENROUTER_API_KEY`
on a box that used Anthropic moves the default to OpenRouter). Pin it:

```bash
polyrob model set-default anthropic claude-sonnet-4-5   # persists DEFAULT_PROVIDER / DEFAULT_MODEL
polyrob run -p openai -m gpt-5 "…"                      # per run
```

`DEEPSEEK_API_KEY` is never auto-selected (its direct client has no tool
calling); pass `-p deepseek` or reach DeepSeek through OpenRouter.
`PERPLEXITY_API_KEY` is a web-search tool key, not a provider.

### Flat-rate and subscription plans

Some plans hand you a key instead of metering tokens. POLYROB ships provider
rows for them, so setting the key is all you do:

```bash
polyrob config set ZAI_API_KEY        # then: polyrob run -p zai-coding "…"
polyrob config set KIMI_CODING_API_KEY
polyrob config set OLLAMA_API_KEY     # ollama-cloud — NOT a local Ollama
```

`polyrob model list` prints the live table of these rows (`ollama-cloud`,
`zai`, `zai-coding`, `cerebras`, `moonshot`, `kimi-coding`, `minimax`, `xai`,
`dashscope`, `alibaba-coding`, `stepfun`, `ai-gateway`, `opencode-zen`,
`kilocode`, `huggingface`, `xiaomi`, `tencent-tokenhub`, `copilot`, and their
regional variants). Every provider also takes a `*_BASE_URL` override for a
proxy or regional endpoint.

A row marked `subscription: true` records no per-token cost, because you incur
none. Cerebras serves both plan types on one endpoint and defaults to metered;
on a flat plan declare it in `providers.yaml`:

```yaml
providers:
  cerebras:
    subscription: true
```

These rows are appended after the six built-ins and are excluded from
automatic failover, so adding one never changes what an existing install
resolves to.

### Plans that need a sign-in (OAuth)

Claude Pro/Max, ChatGPT (Codex), GitHub Copilot, SuperGrok, Qwen and MiniMax
issue no API key. `polyrob auth add <provider>` runs their sign-in:

```bash
polyrob config set LLM_OAUTH_ENABLED true   # off by default, everywhere
polyrob auth add anthropic-oauth            # anthropic-oauth | openai-codex | github-copilot | xai-oauth | qwen-oauth | minimax-oauth
polyrob auth status                         # source, health, expiry of every credential
polyrob auth refresh anthropic-oauth
polyrob auth remove anthropic-oauth         # forgets the token; does not revoke it
```

> **Read before you connect one.** These plans publish no OAuth client id for
> third-party apps. The only ids that work are the ones inside the vendors'
> own CLIs, so connecting authenticates POLYROB **as that client**. Every
> open-source agent that offers "sign in with your Claude/ChatGPT plan" does
> the same thing. It is a real terms-of-service exposure and it lands on
> **your** account. `polyrob auth add` says this and asks before it acts.

The device-code and manual-paste flows work headless and over SSH. The
loopback flow opens a listener on `127.0.0.1` and is refused unless
`POLYROB_LOCAL` is set. Both OAuth endpoints must be `https`.

### Model behaviour knobs

`THINKING_CONFIG_ENABLED=true` turns on extended reasoning for models that
support it (off by default because it changes cost and latency).
`RUN_BUDGET_USD=<dollars>` sets a per-session ceiling on real provider cost;
the run halts honestly when it is reached, and the remaining budget is shown to
the model so it can pace itself.

### Your own endpoint (`providers.yaml`)

Any OpenAI-compatible or Anthropic-compatible endpoint can be declared with no
code, in `~/.polyrob/providers.yaml` (path override `LLM_CUSTOM_PROVIDERS`):
a local Ollama, LM Studio, vLLM or llama.cpp server, an aggregator, or a
corporate gateway.

```yaml
providers:
  ollama:
    base_url: http://127.0.0.1:11434/v1
    auth_type: none
    transport: chat_completions
    default_model: qwen3-coder:30b
    models: [qwen3-coder:30b, llama3.3:70b]
  mygateway:
    base_url: https://gateway.corp.internal/anthropic
    env_key: MYGATEWAY_API_KEY          # must be *_API_KEY-shaped
    transport: anthropic_messages
    bearer_auth: true
    default_model: glm-5
    models: [glm-5]
```

Then `polyrob run -p ollama "…"` or `DEFAULT_PROVIDER=ollama`. A row can
override a built-in (give `openai:` a gateway `base_url`). Rules:

- `transport` is `chat_completions` or `anthropic_messages`. A row names a wire
  shape, never a client class.
- Declare `models:` so they are listable and route through the `/v1` surface.
- `prompt_in_init: false` keeps the wizard from asking for that row's key.
- Load-time checks: `env_key` must look like an API key (never a wallet or
  token name), `base_url` must be http(s) and never a cloud-metadata address,
  and a group- or world-writable file is refused. Each loaded row is logged
  with its endpoint.
- The file decides where your prompts and keys go. Keep it mode `600`. Agent
  file tools cannot read it and the console cannot write it.

Any OAuth provider can be declared in the same file with an `oauth:` block
(`grant: device_code` or `authorization_code`, `redirect_mode: manual` or
`loopback`); the flows are driven from that data.

---

## 4. Memory

Cross-session memory is on by default and tenant-scoped.

| `MEMORY_BACKEND` | What you get |
|---|---|
| `sqlite` (server default) | keyword recall over SQLite FTS5, no extra dependencies |
| `local_vector` (default under `POLYROB_LOCAL`) | hybrid keyword + vector recall; needs `pip install "polyrob[memory-vector]"` |
| `none` | no cross-session memory |

Vector recall loads the `sqlite-vec` extension through `apsw`, because the
standard-library `sqlite3` is often built without extension loading. If the
extension or the embedder is missing, the agent logs one warning and degrades
to keyword recall. It keeps working.

`MEMORY_REQUIRE_USER_ID` (default on) refuses memory for anonymous sessions;
set it to `false` only on a single-user box that wants one shared bucket.
The knowledge base (`polyrob kb add|search|list|remove|export`) is a separate,
document-shaped store behind `KB_ENABLED`, on by default under `POLYROB_LOCAL`.

Inspect the active provider from the REPL with `/memory`.

---

## 5. The autonomy dial

Out of the box the agent is **interactive**: it acts on your messages and on
nothing else. Autonomy is a set of loops you opt into, governed by four axes.
Set them in this order and stop when you have what you want.

| Axis | Default | What it moves |
|---|---|---|
| `POLYROB_LOCAL` | off | The single-user profile. Turns on the interactive tools (coding, git, knowledge base, memory, project context) as a group. Set automatically by the CLI. |
| `AUTONOMY_ENABLED` | off | The master switch for the self-directed loops: self-wake, the goal board and planner, the curator, background review, episodic continuity, writable skills. |
| `AUTONOMY_POSTURE` | `silent` | How visible the autonomous work is. `owner-visible` adds completion judging, blocker escalation and continuity; `full` adds time-based initiative (cron) and the no-change wake gate. Recommended for a single-user box: `owner-visible`. |
| `AUTONOMY_MODE` | `supervised` | `autonomous` = act-and-report on a single-owner box: capability flags (Twitter, MCP, groups, email surface, invoicing) default on, approvals become allow-audit-notify, outbound policy opens. Effective only with `POLYROB_LOCAL` and a bound owner; otherwise it clamps back to `supervised`. |

```bash
polyrob autonomy status            # the posture card: every axis, pauses, loop state
polyrob autonomy on                # writes AUTONOMY_ENABLED=true (add --global to keep it out of this project)
polyrob autonomy on --mode autonomous
polyrob autonomy off
```

**If you set axis 3 or 4, skip axis 2 — they turn it on.** Choosing
`AUTONOMY_MODE=autonomous` or an `owner-visible`/`full` posture flips
`AUTONOMY_ENABLED` on for you, so a deliberate choice is never left inert. An
explicit per-flag value always wins over any group default.

**What never moves with the dial:** spending money, host access and secrets.
Each keeps its own gate (§9, §6) under every mode.

### Stopping it

A pause is immediate and needs no restart:

```bash
polyrob autonomy pause                   # everything
polyrob autonomy pause trading --for 6h  # one scope, timed
polyrob autonomy resume
```

The same words work in chat ("stop", "halt", "pause", "freeze", "standby";
"resume", "unpause", "unfreeze"). `polyrob owner …` and the console expose the
same record. Details: [owner-controls.md](owner-controls.md).

### Compute posture

`AGENT_COMPUTE_POSTURE` is a separate axis for how much of the host the agent
may use. `0` (default) is a confined, ephemeral sandbox. `1` gives it a
persistent dev container with a `shell` and pip installs. `2` adds the
approval-gated `self_env` verbs that patch and restart the agent itself. `3`
means the host and requires `POLYROB_LOCAL`. Leave it at `0` unless you are
building software with the agent. See [security-model.md](security-model.md).

---

## 6. Tools and capabilities

### Toolsets

A session runs with a named toolset. Pick one with `--toolset` or
`POLYROB_AGENT_TOOLSET`: `minimal`, `safe`, `default`, `research`,
`trading_research`, `coding`, `development`, `browser`, `social`, `full`,
`earn`, `owner_interactive`.

With `POLYROB_AGENT_TOOLSET` unset the CLI runs the **default** toolset —
`filesystem`, `task`, `web_fetch` — plus `coding`, `anysite` and `defi_data`
when their own flags are on. Nothing else is loaded, which is why an agent that
"refuses" a tool is usually one that was never given it.

```bash
polyrob tools list        # the catalog
polyrob tools status      # the same rows plus WHY each disabled one is disabled
```

`/toolset` in the REPL lists the toolsets and sets the default for new sessions.
Under the dynamic tool rig (`TOOL_PROGRESSIVE_DISCLOSURE`, on under
`POLYROB_LOCAL`) the agent sees every loadable tool and pulls one in mid-session
with `load_tool`; money tools are never loadable that way.

### Code execution

Off by default and never in a default toolset.

```bash
polyrob config set CODE_EXEC_ENABLED true
polyrob config set CODE_EXEC_BACKEND docker      # local_subprocess | docker | ssh
```

`local_subprocess` is a plain host process and **not a sandbox**; a server
refuses it. `docker` is the hardened container. `ssh` runs on a remote host
you name with `CODE_EXEC_SSH_HOST` and must be attested disposable with
`CODE_EXEC_SSH_SANDBOXED=true` before a server accepts it.

### MCP

`MCP_ENABLED=true` connects the outbound `mcp` tool to the servers in
`config/mcp_config.json` (secrets as `${VAR}` placeholders). Adding a server
from chat — and what an added server can and cannot reach — is in
[owner-controls.md](owner-controls.md#mcp-servers-giving-the-agent-new-tools).
`MCP_SERVE_ENABLED=true` mounts POLYROB as an inbound MCP server at `POST /mcp`
for Claude Desktop or Cursor. See [api.md](api.md#mcp-server-inbound).

### Browser, web and search

`web_fetch` is on by default; browser automation needs the `browser` extra
and `python -m playwright install chromium`. `PERPLEXITY_API_KEY` enables the
search tool. Fetched content always enters the model as untrusted data.

### Project context

Under `POLYROB_LOCAL` the agent auto-loads one context file from the current
repository (`polyrob.md`, then `POLYROB.md`, `AGENTS.md`, `CLAUDE.md`,
`.cursorrules`; highest precedence wins, never concatenated). Use `polyrob.md`
to steer POLYROB without touching the file your other coding agents read.
Switch with `PROJECT_CONTEXT_AUTOLOAD`.

---

## 7. Surfaces and who may talk to it

Every chat surface runs the same agent. Start one with `polyrob telegram`,
`polyrob email`, `polyrob discord`, `polyrob slack`, `polyrob signal`,
`polyrob whatsapp`, `polyrob x`, or all enabled ones with `polyrob gateway`.

### The owner

Bind yourself before exposing any surface:

```bash
polyrob config set POLYROB_OWNER_TELEGRAM_ID 123456789   # your numeric Telegram id
polyrob config set ALLOWED_TELEGRAM_USER_IDS 123456789   # the DM lock
polyrob owner show                                        # the bound owner and per-surface posture
```

With no allowlist the bot is locked and replies with the sender's id so you
can copy it. `POLYROB_OWNER_USER_ID` is the internal principal and you
normally do not set it; a single Telegram id in the allowlist becomes the owner
alias automatically.

### Strangers

Private chats are permission-list by default. Beyond the owner, a sender is
either **paired** (`POLYROB_REQUIRE_PAIRING=true`, then `polyrob owner pair
approve <code>`) or a **correspondent** the agent contacted first, whose
replies arrive as data, never as instructions (`CORRESPONDENT_ACCESS_ENABLED`).
Email senders are always correspondent-or-denied.

### Groups

Groups are a different regime: the agent answers anyone in a room you have
allowed, from a read-only room toolset, with per-chat roles and a per-chat
policy (`chat.mode`, `chat.instructions`, caps, quiet hours). Enable with
`GROUP_CHAT_ENABLED=true` (on under `AUTONOMY_MODE=autonomous`), then
`/groups allow here` in the room. The full setup, including the BotFather
privacy-mode step, is in [groups.md](groups.md).

### Outbound

Who the agent may message on its own is an allowlist (`polyrob owner allow
telegram <id>`), widened by `OUTBOUND_POLICY` and capped by
`OUTBOUND_DAILY_SEND_CAP`.

Everything the agent sends **you** rides one delivery rail, bounded so a
runaway loop cannot fill your chat:

| Bound | Default | Flag |
|---|---|---|
| messages per day | 30 | `USER_DELIVERY_DAILY_CAP`, pref `delivery.daily_cap` |
| messages per hour | 10 | `USER_DELIVERY_RATE_PER_HOUR`, pref `delivery.rate_per_hour` |
| identical text suppressed for | 24h | `USER_DELIVERY_DEDUP_HOURS` |
| framework status pings per day | 10 | `USER_DELIVERY_LIFECYCLE_DAILY_CAP` |
| slots reserved from low-priority traffic | 8 | `USER_DELIVERY_RESERVED_SLOTS` |
| gap before the agent repeats the same text to you | 2h | `OWNER_MESSAGE_COOLDOWN_SEC` |

Three rules make those bounds safe to live with:

- **Safety-bearing messages are exempt, and do not spend your budget.** An
  approval the agent is blocked on, a blocked goal, a transaction that has
  already moved money, a credit-death or security notice — none of them queue
  behind ordinary traffic, and none of them consume a slot that ordinary
  traffic could be denied for.
- **A bounded message is never lost.** It is recorded durably and read back
  with `polyrob owner missed` / `/missed`, which names the bound that held it.
  Dedup is the one exception, and only because you already received that text.
- **The preference wins unless the deployment set a ceiling.** With no explicit
  `USER_DELIVERY_DAILY_CAP` in the environment, `polyrob config set
  delivery.daily_cap 60` raises your cap. With one set, the preference may only
  tighten it.

Quiet hours (`digest.quiet_hours`) defer rather than drop. Framework run pings
(`▶ goal started`) are OFF by default in every posture —
`AUTONOMY_START_NOTICE=true` turns them on. See
[owner-controls.md](owner-controls.md#what-did-i-miss).

---

## 8. Identity, character and profiles

- **Instance id** (`POLYROB_INSTANCE_ID`, default `polyrob`): the name of this
  deployment and the tenant key. See [instances.md](instances.md).
- **SOUL** (`polyrob identity soul`): operator-authored identity documents the
  agent cannot change.
- **Character** (`polyrob identity persona`, `PERSONALITY_DEFAULT_CHARACTER`):
  the voice layer, a `<name>.character.json`. The package ships the neutral
  `polyrob` character plus six curated ones — `default`, `writer`, `researcher`,
  `analyst`, `coder`, `ops` — and your own go in your data or profile dir, which
  wins over both.
- **Avatar** (`polyrob identity avatar`): the generated face used on cards
  and profiles.
- **Profiles** (`polyrob profile create|use|export|install`): whole isolated
  homes for running several bots on one machine. See [profiles.md](profiles.md).

---

## 9. Money

Everything that touches money is off by default and has its own gates that
no autonomy setting moves: the wallet daily and per-transaction caps, the
approval lane (`PAYMENT_APPROVAL_MODE`, `APPROVAL_PROVIDER`), the DeFi
autonomous ceiling, and the trading grants.

The ceilings are environment flags — `WALLET_DAILY_CAP_USD`,
`AGENT_WALLET_MAX_PER_TX_USD`, `DEFI_AUTONOMOUS_MAX_USD`. Each has a preference
twin (`budget.wallet_daily_usd`, `budget.wallet_per_tx_usd`,
`budget.defi_autonomous_usd`) you can set live from chat, and the effective
value is the **minimum of the two**: a preference only ever tightens what the
environment already allows.

Start with [payments.md](payments.md); it is the complete reference for the
wallet, invoicing, x402, trading and token deployment.

---

## 10. Preferences you set from chat or the CLI

Preferences apply live or on the next turn and never need a restart. They are
per owner (and per room for `chat.*`). `polyrob config list` prints them all
with their current value; from chat, `/config set KEY VALUE`.

| Group | Keys | Notes |
|---|---|---|
| Style | `style.verbosity`, `style.tone`, `style.language` | how the agent writes to you |
| Session | `session.toolset`, `session.persona` | next session |
| Approvals | `approvals.require`, `approvals.deny`, `approvals.provider` | tighten only; an env value is the floor |
| Budget | `budget.wallet_daily_usd`, `budget.wallet_per_tx_usd`, `budget.defi_autonomous_usd` | effective value is the minimum of pref and env |
| Goals | `goals.daily_quota`, `goals.max_concurrent`, `goals.notify_on_done` | |
| Autonomy | `autonomy.self_wake`, `autonomy.background_review` | |
| Delivery | `delivery.rate_per_hour`, `delivery.daily_cap`, `digest.enabled`, `digest.channel`, `digest.quiet_hours`, `progress.telegram`, `pause.phrases` | owner notices and the daily digest |
| Outbound | `outbound.policy`, `outbound.domains`, `outbound.max_new_recipients_per_day`, `outbound.daily_send_cap` | |
| Rooms | `chat.mode`, `chat.name`, `chat.instructions`, `chat.wake_words`, `chat.reply_cap_per_hour`, `chat.member_cooldown_sec`, `chat.context_lines`, `chat.quiet_hours`, `chat.mute_until`, `chat.tone`, `chat.verbosity`, `chat.language` | per room; set with `/groups set here KEY VALUE` |
| UI | `ui.show_avatar` | |

A guarded preference (approvals, budget) can only tighten what the environment
allows. The agent may propose a preference change itself; proposals wait in
`polyrob owner pending` until you promote them.

---

## 11. Server and console

A server (`polyrob serve`) and the console (`polyrob dashboard`) derive their
posture from how you bind them: `local` on loopback with no auth, `own_ops`
on a public host with owner login (`--host 0.0.0.0` upgrades to this so a
no-auth console is never exposed by accident), `multitenant` with wallet
sign-in and billing. `WEBVIEW_READ_ONLY=true` makes the console a monitor
that cannot act.

Setting `WEBVIEW_READ_ONLY=false` on a **non-local** console has two
preconditions, checked at boot: a bound owner (`POLYROB_OWNER_USER_ID`) and
`SESSION_REGISTRY_BACKEND=sqlite` on both the console and the agent. Without
them the console refuses to start rather than write into a tenant nobody chose
or resume a session another process owns. See
[deployment-postures.md](deployment-postures.md),
[self-hosting.md](self-hosting.md) and [console.md](console.md).

---

## 12. Troubleshooting

Two reliability defaults you can leave alone, before the table:
`DEAD_TARGET_REGISTRY` skips sends to a chat that has blocked or deleted the
bot and revives it when it writes again. `COMPACTION_PROMPT_GUARD` frames the
context-compaction step so old adversarial text cannot hijack it. Both are on.

| Symptom | Check |
|---|---|
| "polyrob stopped seeing my key" | `polyrob config explain ANTHROPIC_API_KEY` — a key written with `--project` lives in `./.polyrob/.env` and only applies in that directory. |
| The wrong provider answers | `polyrob model list`; pin with `polyrob model set-default`. |
| Recall feels shallow | the log says the `sqlite-vec` extension was unavailable; install the `memory-vector` extra. |
| Autonomy "does nothing" | `polyrob autonomy status` — check the master switch, the posture and whether a pause is active. |
| A flag has no effect | `polyrob doctor --flags --search NAME` shows the resolved value and its source; `polyrob config check` finds a mistyped name. |
| The agent will not run a tool | `polyrob tools status` names the gate — `disabled (missing config: KEY)` or `disabled (gated by FLAG=false)`. |

The complete flag reference: [`docs/CONFIGURATION.md`](../CONFIGURATION.md).
The CLI reference: [cli.md](cli.md). What actually stops the agent from doing
something harmful: [security-model.md](security-model.md).
