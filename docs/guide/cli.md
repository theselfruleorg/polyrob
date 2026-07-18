# CLI Reference

The `polyrob` command-line interface is the primary way to interact with polyrob locally.

---

## Global usage

```
polyrob [command] [args] [flags]
```

Running `polyrob` with no arguments opens the interactive REPL. Add `--plain` to
force plain, line-oriented output (no ANSI / toolbar).

---

## Commands

### `polyrob` (no subcommand) / `polyrob chat`

Open the interactive REPL chat session.

```bash
polyrob          # or: polyrob chat
polyrob --plain  # plain output, no toolbar
```

The REPL starts a persistent agent session. Type your goal and press Enter. The agent reasons and acts step by step, printing each action and result. Type `exit` (or `Ctrl-C`) to quit. Press `Ctrl-L` to clear and repaint the screen if the terminal ever renders artifacts. See [Slash commands](#slash-commands-repl) below.

---

### `polyrob run <task>`

Run a single task non-interactively and print the result.

```bash
polyrob run "summarize https://example.com"
polyrob run "find the top 5 Python repos on GitHub today" --provider anthropic
polyrob run "scrape these pages into a CSV" --toolset research --max-steps 80
polyrob run --resume abc123   # continue an existing session instead of starting a new task
```

The agent runs autonomously until the task is complete or it hits a step/budget limit.
Provide a `TASK`, or `--resume SESSION_ID` to continue an existing session — exactly one of
the two.

| Flag | Description |
|------|-------------|
| `--resume` | Resume an existing session by id (continue it) instead of starting a new task |
| `--model`, `-m` | Model name (e.g. `gpt-5`, `gemini-2.5-flash`) |
| `--provider`, `-p` | Provider: `openai`, `anthropic`, `gemini`, `openrouter`, `nvidia`. (DeepSeek isn't a direct `-p` value — use `-p openrouter -m deepseek/deepseek-chat`.) |
| `--tools`, `-t` | Comma-separated tool list (e.g. `browser,mcp,filesystem`); takes precedence over `--toolset` |
| `--toolset` | Named toolset: `minimal`, `default`, `research`, `coding`, `development`, `browser`, `full`, `safe` |
| `--max-steps` | Maximum steps (default: 50) |
| `--plain` | Force plain output (no ANSI / panels) |
| `--verbose`, `-v` | Show debug logging |

With `--verbose` the console shows DEBUG detail, while `bot.log` follows `LOG_LEVEL`
(set `LOG_LEVEL=DEBUG` to capture DEBUG in the file too).

---

### `polyrob init`

First-run setup. Writes global config to `~/.polyrob/.env` (chmod 600) and creates
`./.polyrob/sessions` for the current project.

```bash
polyrob init
```

Run this once after installing. It prompts you for provider keys, a default
provider/model, toolset, and template. You can re-run it at any time to
reconfigure.

| Flag | Description |
|------|-------------|
| `--quick` | Prompt only for keys + model; skip toolset/template sections |
| `--template` | Pre-fill toolset and persona from a named template (e.g. `research`, `coding`) |
| `--toolset` | Toolset to activate (e.g. `research`, `coding`, `full`) |
| `--default-provider` / `--default-model` | Set the default provider/model without prompting for it |
| `--anthropic-key` / `--openai-key` | Pass a provider key directly instead of being prompted |
| `--no-prompt` (alias `--non-interactive`) | Skip interactive prompts (for scripts/tests) |

---

### `polyrob doctor`

Diagnose your setup — checks for provider keys, optional dependencies (Playwright,
vector memory), and common misconfigurations.

```bash
polyrob doctor
```

---

### `polyrob config`

View or edit the current configuration (merged from `~/.polyrob/.env` and
`./.polyrob/.env`; project config wins over global).

```bash
polyrob config show           # print the merged config, secrets redacted
polyrob config set KEY VALUE  # set a config value (--global writes ~/.polyrob/.env)
polyrob config path           # show config file locations
```

See [configuration.md](configuration.md) for the full environment-flag reference.

---

### `polyrob update`

Check for and apply POLYROB updates.

```bash
polyrob update --check        # report current vs. latest version and exit
polyrob update --dry-run      # print the update plan without changing anything
polyrob update --apply        # snapshot -> install -> guarded migrate -> verify -> auto-rollback
polyrob update --rollback     # restore the most recent snapshot (databases, config, identity)
```

| Flag | Description |
|------|-------------|
| `--check` | Report current vs. latest and exit (`0` up-to-date, `10` if newer) |
| `--dry-run` | Print the update plan without changing anything |
| `--channel` | `stable` (latest release), `pre` (include prereleases), or `git` (track branch) |
| `--apply` | Automated apply with snapshot + guarded migration + auto-rollback on failure |
| `--rollback` / `--snapshot NAME` | Restore the most recent (or a named) snapshot |
| `--list-snapshots` | List restorable snapshots |
| `-y`, `--yes` | Assume yes (non-interactive) |
| `--force` | With `--rollback`/`--apply`, override the in-use guard (risks DB corruption) |
| `--json` | Machine-readable output |

---

### `polyrob model`

Manage LLM models and providers. `polyrob models` is a plural alias.

```bash
polyrob model list                            # available models + provider API-key status
polyrob model set-default                     # no args -> interactive picker
polyrob model set-default openrouter deepseek/deepseek-chat  # or a positional provider+model pair
```

Run `polyrob model list` first to see which providers/models are actually available to
you (this depends on which API keys you've set). `set-default` persists the chosen
provider+model to `~/.polyrob/cli.json` for future `polyrob run`/REPL sessions.

---

### `polyrob session`

Manage task sessions. `polyrob sessions` is a plural alias.

```bash
polyrob session list
polyrob session show abc123
polyrob session tail abc123
polyrob session cancel abc123
```

| Subcommand | Description |
|------------|-------------|
| `list` | List recent sessions (`--all` includes completed ones) |
| `show <id>` | Show detailed information about a session |
| `tail <id>` | Stream a session's feed (reads from the feed directory) |
| `cancel <id>` | Cancel a running session |
| `pause <id>` / `resume <id>` | Pause / resume a running session |
| `costs <id>` | Show cost breakdown for a session |
| `tools <id>` | Show tools used in a session |
| `artifacts <id>` | List artifacts (screenshots, downloads, outputs) for a session |
| `history <id>` | List (or `--dump N`) compaction checkpoints for a session |
| `export <id>` | Export a session's data (transcript, messages, artifacts) |
| `attach <id>` | **Not yet implemented** — use `polyrob run --resume <id>` instead |

---

### `polyrob tools`

Inspect the product-facing tool catalog, current status, and permission classes.

```bash
polyrob tools list
polyrob tools status --json
polyrob tools show filesystem
polyrob tools permissions
polyrob tools export-catalog
```

---

### `polyrob skills` and `polyrob skill`

Skills are reusable task procedures the agent loads on demand. There are two
command groups:

- **`polyrob skills`** — read/authoring surface: `list`, `validate`, `export`.
- **`polyrob skill`** — install pipeline: `install <spec>`, `approve`, `list`,
  `info`, `remove`.

```bash
polyrob skills list                 # skills the agent can load
polyrob skills validate [id]        # check authored skills against the SKILL.md standard
polyrob skill install acme/repo/pdf # install from a folder / owner-repo / git URL / SKILL.md URL
polyrob skill approve pdf           # activate a quarantined install
```

You can also drop a compliant skill folder into `~/.agents/skills/` (or a repo's
`./.agents/skills/`) and POLYROB auto-discovers it — no install step. See the full
**[Skills guide](skills.md)** for scopes, the install flow, and the safety model.
Run `polyrob skills --help` / `polyrob skill --help` for every option.

---

### `polyrob kb`

Manage the local knowledge base — ingest files/folders the agent can search and
recall from, organized into named collections.

```bash
polyrob kb add ./docs --collection handbook   # ingest a file or directory
polyrob kb search "refund policy"             # search a collection (default: "default")
polyrob kb list                               # list ingested sources
polyrob kb remove --source ./docs/old.md      # remove one source, or --collection to clear it all
```

| Subcommand | Description |
|------------|-------------|
| `add <path>` | Ingest a file or directory (`--collection`, `--recursive/--no-recursive`, `--glob`) |
| `search <query>` | Search the knowledge base (`--collection`, `--limit`) |
| `list` | List ingested sources (`--collection` to filter) |
| `remove` | Remove a source (`--source`), or an entire collection (`--collection`, no `--source`) |

---

### Surfaces & serving

| Command | Description |
|---------|-------------|
| `polyrob serve` | Start the local REST API (default `http://localhost:9000`) — see [api.md](api.md) |
| `polyrob dashboard` | Start the local web dashboard/console (default `http://localhost:5050`); `polyrob webgate` is an alias. `--posture` (`local`/`own_ops`/`multitenant`) controls bind address and auth — see [deployment-postures.md](deployment-postures.md) |
| `polyrob telegram` | Run the Telegram surface (requires the `telegram` extra and a bot token) |
| `polyrob whatsapp` | Run the WhatsApp Cloud API surface (webhook server; requires Meta creds) |
| `polyrob email` | Run the email surface (IMAP poll + SMTP) |
| `polyrob discord` | Run the Discord surface (gateway websocket; requires a bot token) |
| `polyrob slack` | Run the Slack surface (Socket Mode; requires app + bot tokens) |
| `polyrob signal` | Run the Signal surface (against a `signal-cli` daemon) |
| `polyrob x` | Run the X/Twitter DM surface (polling; reuses the `TWITTER_*` creds) |
| `polyrob gateway` | Run all enabled surfaces in one process |
| `polyrob surface` | Inspect/pause/resume per-surface circuit breakers: `list`, `pause`, `resume` |
| `polyrob owner` | Owner/correspondent admin: `show`, `correspondents`, `approve`, `invite`, `pending`/`show-pending`/`promote`/`reject` (self-evolution review), `asks`/`fulfill`, `allow`/`deny`/`allowlist` (outbound messaging), `invoices`/`settle` (x402), `groups` (group-chat allowlist) |
| `polyrob version` | Show version and environment info |

### Autonomy & workspace

| Command | Description |
|---------|-------------|
| `polyrob goals` | Manage the durable goals board: `create`, `list`, `show`, `cancel`, `pause`/`resume`, `retry`, `objective` (standing objectives), … |
| `polyrob cron` | Schedule/inspect/cancel durable cron jobs: `schedule <task> <spec>`, `list`, `show`, `cancel` (specs: `30m`, `every monday 09:00`, 5-field cron, ISO one-shot) |
| `polyrob subagents` | Inspect agent delegation / subagent activity: `list`, `show`, `info` |
| `polyrob todos` | Manage a standalone workspace `todo.md` (`list`/`add`/`done`/`clear`/`stats`) |

### Owner, money & knowledge

| Command | Description |
|---------|-------------|
| `polyrob journey` | Timeline recap: what the agent did, learned, earned, changed (`--since 24h\|7d`) |
| `polyrob finance` | Balance sheet over the unified ledger: earned, spent, pending invoices, net (`--days`) |
| `polyrob wallet` | Agent wallet: addresses/balances/caps; `set-cap daily\|per-tx` writes the money-authoritative env caps |
| `polyrob approvals` | Manage the approval-gated action set: `list`, `add`, `remove` |
| `polyrob knowledge` | `export` the notes/episodes/skills/identity/goals knowledge vault (Obsidian-compatible) |
| `polyrob datagen` | Synthetic dataset generation: `run`, `export` (trajectory corpus) |
| `polyrob pfp` | Avatar/profile picture: `show`, `generate`, `pick`, `push`, `studio` |

---

## Slash commands (REPL)

Inside the interactive REPL, commands are prefixed with `/`. Type `/help` to list them.

| Command | Description |
|---------|-------------|
| `/help` (`/h`, `/?`) | Show available commands |
| `/exit` (`/quit`, `/q`) | Leave the REPL |
| `/status` | Live session status (tokens, cost, context) |
| `/usage` (`/cost`) | Authoritative usage breakdown (DB / estimate) |
| `/telemetry [window]` | Cross-session event counts + wallet spend (e.g. `/telemetry 24h`) |
| `/journey [window]` (`/recap`) | Timeline: what the agent did, learned, earned, changed |
| `/finance [days]` | Balance sheet: earned, spent, pending invoices, net |
| `/learn <description>` | Describe a procedure; distill it into a pending skill for review |
| `/tools` | List the agent's registered tools/actions |
| `/toolset [name]` | List named toolsets, or set the default toolset for new sessions (persists `session.toolset`; applies next session — no live tool re-registration) |
| `/persona [name-or-text]` | List personas, or set the default persona for new sessions (persists `session.persona`; a known template key or literal text, threat-scanned; applies next session) |
| `/sessions` | List all known sessions |
| `/replay <session-id>` (`/resume`) | Replay a session's feed (visual history) — not a re-attach; continue a session with `polyrob run --resume <id>` |
| `/history` | Show this conversation's turns |
| `/clear` | Clear history (keep the system prompt) |
| `/compact` (`/compress`) | Compact history via the LLM (async) |
| `/model <provider> <model>` | Swap the session model live and persist it as the default. Also accepts `<provider>/<model>` or a configured alias |
| `/cwd` | Show the session workspace directory |
| `/session` (`/info`) | Session identity: instance, owner, user, model, memory, workspace |
| `/self` (`/identity`, `/soul`) | Show the instance identity (SOUL + SELF docs, read-only) |
| `/memory [search <query>]` | Show the active cross-session memory provider; `search <query>` recalls from it |
| `/verbose` | Toggle the live trace (steps, tools, reasoning) |
| `/quiet` | Mute/restore the default tool transcript |
| `/steps` | Show the last turn's steps/tools trace |
| `/autonomy` | Show autonomy loops + scheduled cron jobs / open goals |
| `/goals` | Show goals board summary |
| `/subagents` | Show delegation capability info |
| `/todos` | Show workspace todos from `todo.md` |
| `/logs` | Show recent log entries for this session |
| `/export <format> [output]` | Export current session data |
| `/skills [query\|list\|info <id>\|install <spec>\|approve <id>\|remove <id>]` | List/search skills; manage the install pipeline |
| `/cron` (`/crons`) `[list]` | List scheduled cron jobs (read-only) |
| `/mcp [list]` | List configured MCP servers and their status |
| `/kb [list [collection]\|search <query>]` | List + search the local knowledge base |
| `/pending [show\|approve\|reject <kind> <id>]` | Review the agent's pending self-evolution proposals (skills, identity notes) — owner-only |
| `/approve [list\|add <action>\|remove <action>]` | Manage approval gates: which actions need your OK before they run |
| `/config [list [group]\|get KEY\|set KEY VALUE [--confirm]\|check]` | View/change preferences and flags |
| `/context` | Context-assembly breakdown: per-slot token counts + % of context |

---

## Config location

| File | Purpose |
|------|---------|
| `~/.polyrob/.env` | Global config: provider keys, default provider/model, toolset |
| `~/.polyrob/cli.json` | Default provider/model set via `model set-default`/`/model`, plus any `model_aliases` you've defined; folded into `.env` the next time you run `polyrob init` |
| `./.polyrob/.env` | Project-local overrides |
| `./.polyrob/sessions/` | Project session workspaces and logs |

---

## Tips

- `polyrob run` is stateless by default — each call starts a new session. To continue a
  previous one, pass `--resume SESSION_ID`. For an interactive, memory-carrying flow across
  turns, use the REPL (`polyrob` with no arguments).
- You can chain `polyrob run` in shell scripts for batch processing.
- To use a different instance (e.g. a second named bot), set `POLYROB_INSTANCE_ID=<name>` before running. See [instances.md](instances.md).
