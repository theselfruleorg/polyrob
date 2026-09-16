# CLI reference

`polyrob` is the whole product on the terminal: the agent, the surfaces, the
autonomy dial, the wallet and the owner seat. This page is the complete command
list. For what a setting *means*, read [configuration.md](configuration.md); for
the flag names and defaults, [`docs/CONFIGURATION.md`](../CONFIGURATION.md).

Everything here is also available as `--help`: `polyrob --help` for the grouped
list, `polyrob <group> --help` for a group, `polyrob <group> <verb> --help` for
one verb's options.

---

## Global usage

```
polyrob [ROOT OPTIONS] <command> [args] [options]
```

Running `polyrob` with no command opens the interactive REPL.

| Root option | Meaning |
|---|---|
| `-P, --profile NAME` | Run as a named profile — an isolated home with its own `.env`, characters, memory and identity. Applies to every subcommand (`polyrob -P scout telegram`). See [profiles.md](profiles.md) |
| `--project PATH` | Persistent project workspace; the agent reads and writes there across sessions (sets `POLYROB_PROJECT_DIR`) |
| `-m, --model` / `-p, --provider` | Defaults for bare REPL, `chat`, and `run`; command-local options override them |
| `--toolset NAME` | Default toolset for bare REPL, `chat`, and `run` |
| `--plain` | Plain, line-oriented output (no ANSI, no toolbar) |
| `-V, --version` | Print the version and exit |

`polyrob --help` groups the commands under **Start here**, **Surfaces**,
**Autonomy & work**, **Money**, **Owner** and **Other**. Aliases render on the
canonical row and stay invocable: `sessions`=`session`, `models`=`model`,
`profiles`=`profile`, `webgate`=`dashboard`, and `soul`/`persona`/`pfp` are the
three `identity` subgroups also reachable at the top level. `polyrob knowledge`
is a deprecated alias for `polyrob kb export`. Entries marked **related** are
different command trees, not interchangeable aliases (`skills`/`skill`,
`owner`/`approvals`, and the identity subgroups).

---

## Start here

### `polyrob` / `polyrob chat` — the REPL

```bash
polyrob                 # or: polyrob chat
polyrob --plain
polyrob -p anthropic -m claude-sonnet-4-5
```

A persistent agent session. Type your goal and press Enter; the agent acts step
by step and prints each action. `Alt+Enter` (`Esc` then `Enter`) inserts a
newline, `Ctrl-L` repaints the screen, `Ctrl-C` interrupts the current turn,
`/exit` or `Ctrl-D` leaves. See [Slash commands](#slash-commands-repl).

While work is running, `/steer`, `/pause`, `/pending`, `/status`, and other safe
controls remain available. A normal follow-up or a command that changes the
conversation is kept in the editor until the turn finishes; it is never silently
discarded. Interactive approval prompts accept their displayed decision words
through this same input box. History persists within the active profile;
credential-shaped inputs and credential setup commands are omitted.

Use `/attach "path to image.png"` to stage local files for the next agent step.
The command prints the saved paths. If the conversation is idle, type a message
after attaching to start the next turn.

### `polyrob run <task>` — one shot

```bash
polyrob run "summarize https://example.com"
polyrob run "scrape these pages into a CSV" --toolset research --max-steps 80
polyrob run --resume abc123
polyrob run --task-file task.txt --output-format json
cat task.txt | polyrob run - --no-input --output-format jsonl
polyrob run "describe this image" --attach diagram.png
```

Give a task **or** `--resume SESSION_ID`, not both. The agent runs until the task
is done or it hits a step or budget limit.

| Option | Description |
|---|---|
| `--resume SESSION_ID` | Continue using saved settings; model/provider/tool/step-budget overrides are rejected |
| `--task-file PATH` | Read a UTF-8 task file; `-` reads stdin. A positional task of `-` also reads stdin |
| `--attach PATH` | Stage a local file/image in the session workspace; repeat for multiple files |
| `--output-format text\|json\|jsonl` | Human transcript, one structured result, or normalized events followed by a result |
| `--no-input` | Disable onboarding and deny interactive approval prompts; structured formats imply it |
| `-m, --model` / `-p, --provider` | Model and provider for this run |
| `-t, --tools` | Comma-separated tool ids; takes precedence over `--toolset` |
| `--toolset` | `minimal`, `safe`, `default`, `research`, `trading_research`, `coding`, `development`, `browser`, `social`, `full`, `earn`, `owner_interactive` |
| `--max-steps` | Maximum steps (default 50) |
| `--plain` / `-v, --verbose` | Plain output / debug logging on the console |

`--verbose` raises the console level only; `bot.log` follows `LOG_LEVEL`.

Structured output owns stdout; diagnostics go to stderr. Results carry
`schema_version: 1`, `kind: "result"`, `session_id`, `success`, `exit_code`,
`answer`, `error`, and `usage`. JSONL event records have `kind: "event"`.
A failed run exits nonzero; invalid CLI arguments exit 2. Early argument errors
use Click's stderr diagnostics before a run/result stream is created.
`--no-input` does not bypass approval policy: interactive requests are denied.
Queued approval providers retain their own decision/timeout behavior.

### `polyrob init` — first-run setup

```bash
polyrob init
polyrob init --no-prompt --owner aria --instance-id aria --openai-key sk-...
```

Writes `~/.polyrob/.env` (mode 600) and creates `./.polyrob/sessions` for this
project. Re-run it any time.

| Option | Description |
|---|---|
| `--owner ID` | Bind this instance to an owner principal |
| `--instance-id NAME` | Name the deployment (default `polyrob`) |
| `--profile NAME` | Write the identity keys into profile `NAME` instead of the global env; creates the profile if needed |
| `--character SLUG` / `--character-from NAME` | Scaffold this instance's own character and select it |
| `--template` / `--toolset` | Pre-fill from a starter template; activate a toolset |
| `--default-provider` / `--default-model` | Set the defaults without prompting |
| `--anthropic-key` / `--openai-key` | Pass a key instead of being prompted |
| `--quick` | Keys and model only; skip the toolset and template sections |
| `--no-prompt` (alias `--non-interactive`) | No prompts, for scripts |

### `polyrob doctor` — health and flags

```bash
polyrob doctor                        # status snapshot: pause state, ranked health, sections
polyrob doctor --full                 # plus the full check transcript
polyrob doctor --json
polyrob doctor --flags                # every env flag with its resolved value and source
polyrob doctor --flags --group memory
polyrob doctor --flags --search x402
polyrob doctor --changed              # only flags set away from their default
```

Plain `doctor` leads with the same snapshot every other seat renders. `--group`,
`--search` and `--changed` each imply `--flags`, combine with AND, and filter
`--json` too; no match prints `no flags match …` and exits 0.

### `polyrob auth` — provider credentials

```bash
polyrob auth add <provider>     # key or OAuth sign-in
polyrob auth status [--json]    # every provider: source, health, expiry
polyrob auth list               # credentials in the auth store
polyrob auth refresh <provider>
polyrob auth remove <provider>  # forgets the token; does not revoke it
```

An OAuth seat needs `LLM_OAUTH_ENABLED=true`. Read the terms-of-service warning
in [configuration.md §3](configuration.md#plans-that-need-a-sign-in-oauth) before
you connect one.

### `polyrob keys` — API access keys

```bash
polyrob keys create --name automation [--expires-days 30]
polyrob keys list
polyrob keys revoke rob_…
```

These keys authenticate the A2A and OpenAI-compatible API surfaces through the
`X-API-KEY` header. `create` prints the full secret once; `list` shows only its
prefix and label. `revoke` accepts the 12-character prefix shown by `list`.

### `polyrob config` — settings

```bash
polyrob config set KEY [VALUE]   # omit VALUE to be prompted (hidden for secrets)
polyrob config unset KEY
polyrob config get KEY           # effective value + source + description
polyrob config show              # merged config, secrets redacted
polyrob config list              # every setting: preferences first, then flags
polyrob config search TEXT       # fuzzy name + description search
polyrob config explain KEY       # every layer that sets KEY, and which one won
polyrob config path              # the env files this process reads
polyrob config check             # validate the env files against the catalog
polyrob config migrate           # copy secrets out of the legacy env files
```

`set` routes by the shape of the key — secret, env flag, or preference. The read
verbs take `--json`. How the routing and precedence work:
[configuration.md §1](configuration.md#1-how-configuration-works).

### `polyrob model`

```bash
polyrob model list                         # models + which provider keys are present
polyrob model set-default                  # interactive picker
polyrob model set-default anthropic claude-sonnet-4-5
```

### `polyrob update`

```bash
polyrob update                # status + the exact steps for YOUR install method
polyrob update --check        # exit 0 up to date, 10 newer, 1 unknown/error
polyrob update --dry-run
polyrob update --apply        # snapshot -> install -> guarded migrate -> verify -> auto-rollback
polyrob update --list-snapshots
polyrob update --rollback [--snapshot NAME]
```

**`--apply` is automated for git and editable installs only.** A pip or pipx
install prints the manager command instead (`pipx upgrade polyrob`,
`python -m pip install -U polyrob`); schema migrations run on the next start either
way. Other options: `--channel stable|pre|git`, `-y/--yes`, `--force`
(overrides the in-use guard, risks database corruption), `--json`.

### `polyrob version`

Version and environment info.

---

## Surfaces

Each surface is a long-running process. Every one takes `-v/--verbose`.

| Command | What it runs |
|---|---|
| `polyrob telegram` | Telegram bot, long polling (`--token`, else `TELEGRAM_BOT_TOKEN`) |
| `polyrob whatsapp` | WhatsApp Cloud API webhook server (`--port`, default 8080) |
| `polyrob email` | Email correspondent surface, IMAP poll + SMTP (`--poll`) |
| `polyrob discord` | Discord bot over the gateway websocket (`--token`) |
| `polyrob slack` | Slack bot in Socket Mode (`--bot-token`, `--app-token`) |
| `polyrob signal` | Signal, against a `signal-cli` daemon (`--daemon-url`, `--account`) |
| `polyrob x` | X (Twitter) DM bot, polling |
| `polyrob gateway` | Every enabled surface in one process (`--port`, `--telegram-token`) |
| `polyrob serve` | The REST API (`--host`, `--port`, `--workers`; default `127.0.0.1:9000`) — see [api.md](api.md) |
| `polyrob dashboard` | The web console (`--host`, `--port`, default `127.0.0.1:5050`; `--posture local\|own_ops\|multitenant`, `--multitenant`, `--no-browser`). Alias `polyrob webgate` — see [console.md](console.md) |

Group chats have their own setup and rules: [groups.md](groups.md).

---

## Autonomy & work

### `polyrob autonomy` — the dial

```bash
polyrob autonomy status [--json]        # the posture card: every axis, pauses, loop state
polyrob autonomy on [--mode supervised|autonomous] [--global]
polyrob autonomy off
polyrob autonomy pause [WORD…] [--for 6h]
polyrob autonomy resume [WORD…]
polyrob autonomy halt                   # alias of pause with no words
```

`on`/`off` write `AUTONOMY_ENABLED` and apply to the next process.
`pause`/`resume` are live and need no restart — they write the one pause record
every loop, timer and script reads. See [owner-controls.md](owner-controls.md).

### `polyrob goals` — the durable goal board

`create`, `list`, `show`, `ready`, `edit`, `cancel`, `pause`, `resume`, `retry`,
`events`, `tree`, and the `objective` subgroup (`add`, `list`, `show`, `pause`,
`activate`, `drop`).

```bash
polyrob goals create "Draft the weekly report" -b "…" -p 7 --acceptance report.md
polyrob goals list --status ready --json
polyrob goals tree                       # objectives with their goals
polyrob goals events <id>                # the event timeline for one goal
```

`create` also takes `--parent`, `--objective`, `--tools`, `--triage` and
`--force`. Standing work and objectives: [streams.md](streams.md).

### `polyrob cron` — scheduled runs

```bash
polyrob cron schedule "check the feed" 30m [--max-duration 180] [--user ID]
polyrob cron list
polyrob cron show <id>
polyrob cron cancel <id>
polyrob cron digest ["every day 08:00"] [--off] [--deliver telegram] [--days 1]
```

Schedule specs: a duration (`30m`), `every monday 09:00`, a 5-field cron
expression, or an ISO timestamp for a one-shot.

`digest` is the owner daily digest — the roll-up of everything the delivery
rail could not send you live. It composes from the ledger, the event log and
your open asks with no model call, so it costs nothing to run. Calling it again
moves the schedule rather than adding a second one; with no argument it prints
what is scheduled. See
[owner-controls.md](owner-controls.md#what-did-i-miss).

### `polyrob session`

```bash
polyrob session list [--all] [--json]
polyrob session show <id> [--json]
polyrob session tail <id> [-f|--follow]     # watch a running session live
polyrob session history <id> [--dump N]     # compaction checkpoints
polyrob session artifacts <id>
polyrob session costs <id> [--json]
polyrob session tools <id> [--json]
polyrob session cancel|pause|resume <id>
polyrob session export <id> [-o FILE] [--format json|txt|raw|sharegpt|openai]
```

`tail --follow` lets a second terminal watch any running session, including a
goal or cron run. `raw`, `sharegpt` and `openai` are training-corpus formats. To
continue a session's execution, use `polyrob run --resume <id>`.

Pause/cancel/resume controls need no provider key. Live controls request a change
through the session's durable control mailbox and wait briefly for acknowledgement.
Pause and cancel take effect at the next **step boundary**, after the current
operation finishes; the CLI reports an outstanding request honestly. `session show`
includes the request and acknowledgement. A paused live process can be resumed in
place. An older process without a control endpoint must be stopped in its own
terminal; the CLI will not pretend that a metadata update stopped it.
Pause suspends the main agent; independently running delegates may finish their
work. Cancel also signals the session's registered agents at their step boundaries.
Session lists print full IDs. Receipt commands accept only unambiguous prefixes.

### `polyrob apps` — the durable app service

```bash
polyrob apps list
polyrob apps show <slug>
polyrob apps approve <slug>      # the owner decision; the supervisor deploys within a tick
polyrob apps reject <slug>
polyrob apps kill <slug>
polyrob apps logs <slug> [n]
polyrob apps supervise [--once] [--interval N]   # the reconcile loop the unit runs
```

`supervise` is the owner-run process that holds the Docker, nginx and firewall
privilege the agent never has. Setting it up:
[deployment-postures.md](deployment-postures.md).

### `polyrob skills` and `polyrob skill`

- `polyrob skills` — read and authoring: `list`, `validate`, `export`.
- `polyrob skill` — the install pipeline: `install <spec>`, `approve`, `list`, `info`, `remove`.

```bash
polyrob skills list
polyrob skills validate [id]
polyrob skill install acme/repo/pdf     # folder, owner/repo, git URL, or SKILL.md URL
polyrob skill approve pdf               # activate a quarantined install
```

A compliant skill folder dropped into `~/.agents/skills/` (or a repo's
`./.agents/skills/`) is discovered with no install step. Full model:
[skills.md](skills.md).

### `polyrob kb` — the knowledge base

```bash
polyrob kb add ./docs --collection handbook   # --recursive/--no-recursive, --glob
polyrob kb search "refund policy" [--collection C] [--limit N]
polyrob kb list [--collection C]
polyrob kb remove --source ./docs/old.md      # or --collection C to clear it
polyrob kb export [--out ./knowledge-vault] [--since 7d] [--user ID]
```

`kb export` writes notes, episodes, skills, identity and goals as an
Obsidian-compatible markdown vault. `polyrob knowledge export` is a deprecated
alias for it.

### `polyrob tools`

`list`, `status`, `show <tool>`, `permissions`, `export-catalog`. `status` names
the gate and the remedy for every tool the session cannot use.

### `polyrob surface`

`list`, `pause <surface>`, `resume <surface>` — the per-surface circuit
breakers, persisted across restarts.

### `polyrob subagents`

`list [--session-id ID] [--json]`, `show <id> [--session-id ID] [--json]`,
`info` (delegation capability and limits). List/show read tenant-scoped durable
background delegation receipts, including status and result. Reused delegation
IDs require `--session-id`. Synchronous children are inspected with `/subagents`
in the resident REPL.

### `polyrob todos`

`list`, `add`, `done <n>`, `clear`, `stats` over a standalone workspace
`todo.md`. This is not the agent's live session TODO tool.

---

## Money

### `polyrob wallet`

```bash
polyrob wallet                    # addresses, balances, network, caps (--json, --no-balances)
polyrob wallet init               # create the wallet (--from-mnemonic / --from-seed to import)
polyrob wallet export             # reveal the seed and per-venue keys — TTY only, typed confirm
polyrob wallet book               # the ledger against every money chain
polyrob wallet set-cap daily|per-tx USD
polyrob wallet bridge <from> <to> <amount> [--execute] [--token-out native]
polyrob wallet bridges            # bridges not yet proven to have arrived
polyrob wallet deploy-token SYMBOL SUPPLY NAME… [--chain base|solana] [--vanity b0b] [--execute]
polyrob wallet launch SYMBOL NAME… [--buy 0.1] [--logo URL] [--execute]
polyrob wallet curve TOKEN [--buy N | --sell N]
```

Every write verb quotes and asserts by default and moves nothing without
`--execute`. `wallet export` is never available to the agent. The complete money
model — caps, approval lanes, invoicing, x402, trading:
[payments.md](payments.md).

### `polyrob finance`

`--days N` (default 7). The unified ledger as two blocks that are never summed:
**Treasury** (the agent's own income, spend, pending, net) and **Runtime** (your
compute cost).

### `polyrob journey`

`--since 24h|7d|30d`. A timeline of what the agent did, learned and changed, and
what it earned.

---

## Owner

### `polyrob owner`

Who may command the agent, and who it may talk to.

**Access and pairing**

```bash
polyrob owner show                              # the bound owner and per-surface posture
polyrob owner correspondents                    # third parties the agent is bound to
polyrob owner invite <session_id> …             # register a correspondent of a session
polyrob owner approve …                         # approve a pending correspondent (--all [surface])
polyrob owner allow <surface> <target>          # outbound send permission
polyrob owner deny <surface> <target>
polyrob owner allowlist
polyrob owner pair pending|approve <code>|revoke <user>
polyrob owner groups allow|deny|list|mode|set|role|tail|service …
```

**Pending and asks**

```bash
polyrob owner inbox [-n N]        # everything waiting on a decision, blocking first
polyrob owner pending             # self-evolution proposals
polyrob owner show-pending <kind> <id>
polyrob owner promote <kind> <id> / polyrob owner reject <kind> <id>
polyrob owner asks [--json]       # what the agent needs from you to unblock work
polyrob owner fulfill <id>
polyrob owner missed [-n N]       # notices the delivery rail could not send live
```

**Money**

```bash
polyrob owner invoices
polyrob owner settle <id> [tx-hash]
polyrob owner sub list|cancel
```

**Control**

```bash
polyrob owner halt / polyrob owner resume            # aliases of autonomy pause / resume
polyrob owner pause-entries / resume-entries         # no NEW treasury positions; exits still run
polyrob owner pause-streams / resume-streams         # no new stream reseeds
```

`polyrob approvals` (`list`, `add <action>`, `remove <action>`) manages the
approval-gated action set and renders on the `owner` row in `--help`.

### `polyrob identity`

The instance's identity, in three subgroups:

- `polyrob identity soul init` — scaffold the operator-authored SOUL docs.
- `polyrob identity persona init <slug> [--from NAME]` · `list` · `show [slug]` — the character (voice) layer.
- `polyrob identity avatar` — `generate`, `randomize`, `pick`, `keep`, `show`, `say`, `push`, `studio`.

`generate` mints a random draft face and voice; `randomize` re-rolls it; `keep`
freezes it permanently; `say` plays the voice signature through the system TTS.
`polyrob soul`, `polyrob persona` and `polyrob pfp` reach the same three groups
directly.

### `polyrob profile`

Lifecycle: `create`, `list`, `use`, `show`, `path`, `rename`, `delete`, `adopt`,
`alias`. Distribution: `export`, `import`, `install`, `update`, `info`. Full
guide: [profiles.md](profiles.md).

---

## Other

- `polyrob datagen run|export` — run a batch of tasks as rollouts and export a label-filtered trajectory corpus.
- `polyrob x-account capture-session|status|signup` — the agent's own X account: the owner login ceremony, the stored session, and the supervised signup flow.

---

## Slash commands (REPL)

Inside the REPL every command starts with `/`. `/help` lists them grouped;
`/help <verb>` explains one. You never have to use a command — plain words work
("schedule a check every morning at 9").

**talk**

| Command | Description |
|---|---|
| `/sessions` | List all known sessions |
| `/session` (`/info`) | This session's identity: instance, owner, user, model, memory, workspace |
| `/history` | This conversation's turns |
| `/clear` | Clear history, keep the system prompt |
| `/compact` (`/compress`) | Compact history through the model, in the background |
| `/export <format> [output]` | Export this session's data |
| `/replay <session-id>` | Replay a session's feed — a visual history, not a re-attach |

**needs you**

| Command | Description |
|---|---|
| `/inbox [n]` | Everything waiting on a decision from you, blocking first |
| `/pending [show\|approve\|reject <kind> <id>]` | The agent's pending self-evolution proposals |
| `/asks [list]` | Open asks — what the agent needs from you to unblock work |
| `/fulfill <id>` | Mark an ask fulfilled and unblock its goals |

**work**

| Command | Description |
|---|---|
| `/goals` | Goal board summary |
| `/cron` (`/crons`) `[list]` | Scheduled cron jobs, read-only |
| `/apps` | Deployed apps and their state |
| `/todos` | Workspace todos from `todo.md` |
| `/subagents` | Delegation capability and limits |
| `/skills [query\|list\|info <id>\|install <spec>\|approve <id>\|remove <id>]` | List, search and install skills |
| `/learn <description>` | Describe a procedure; distill it into a pending skill for review |

**money**

| Command | Description |
|---|---|
| `/finance [days]` | Treasury and runtime cost, as two blocks that are never summed |
| `/usage` (`/cost`) | Authoritative usage breakdown for this session |
| `/book` | The ledger against every money chain: one verdict, then what disagrees |
| `/invoices [pending\|completed\|expired]` | Agent invoices (x402 receivables) |
| `/settle <id> [tx-hash]` | Attest an invoice as paid |
| `/deploy <SYMBOL> <supply> <name…> [on <chain>] [vanity <hex>] [go]` | Deploy a fixed-supply token; quotes unless you add `go` |
| `/launch <SYMBOL> <name…> [buy <amount>] [go]` | Launch a token on the Pons launchpad; quotes unless you add `go` |

**control**

| Command | Description |
|---|---|
| `/pause [word…] [for 6h]` | Pause autonomous work now — everything, or `trading`, `background`, `messages`, `deploying`, or a raw scope |
| `/halt` | `/pause` with no words |
| `/resume [word…]` | Lift the pause, everything or one scope |
| `/autonomy` | Autonomy loops, scheduled cron jobs and open goals |
| `/approve [list\|add <action>\|remove <action>]` | Which actions need your decision before they run |

**remember**

| Command | Description |
|---|---|
| `/memory [search <query>]` | The active memory provider; `search` recalls cross-session |
| `/kb [list [collection]\|search <query>]` | List and search the knowledge base |
| `/journey [window]` (`/recap`) | What the agent did, learned, changed, and earned |
| `/self` (`/identity`, `/soul`) | The instance identity: SOUL + SELF docs, read-only |

**look**

| Command | Description |
|---|---|
| `/status` | Live session status: tokens, cost, context |
| `/doctor` | The `polyrob doctor` health report |
| `/context` | Context assembly: per-slot token counts and share of the window |
| `/steps` | The last turn's steps and tools |
| `/telemetry [window]` | Cross-session event counts and wallet spend |
| `/tools` | The agent's registered tools and actions |
| `/logs` | Recent log entries for this session |

**set up**

| Command | Description |
|---|---|
| `/config [list [group]\|get KEY\|set KEY VALUE [--confirm]\|explain KEY\|search QUERY\|check]` | View and change preferences and flags |
| `/auth` | Provider credentials and how to connect one |
| `/model <provider> <model>` | Swap the session model live and persist it as the default; also `<provider>/<model>` or an alias |
| `/toolset [name]` | List toolsets, or set the default for new sessions |
| `/persona [name-or-text]` | List personas, or set the default for new sessions |
| `/profile` | The active named profile and its homes |
| `/mcp [list\|add <id> <https-url> [key]\|remove <id>\|test <id>]` | MCP servers: list, add, remove, test |
| `/allow <surface> <target>` · `/deny <surface> <target>` · `/allowlist` | Who the agent may message on its own |

**display**

| Command | Description |
|---|---|
| `/verbose` | Toggle the live trace: steps, tools, reasoning |
| `/quiet` | Mute or restore the default tool transcript |
| `/pfp` (`/avatar`) `[status\|generate [force]\|show]` | Show or generate the agent avatar |

**leave**

| Command | Description |
|---|---|
| `/help` (`/h`, `/?`) | The grouped command list; `/help <verb>` for one |
| `/exit` (`/quit`, `/q`) | Leave the REPL |
| `/cwd` | The session workspace directory |
| `/missed [n]` | Owner notices the delivery rail could not send live |

`/toolset` and `/persona` apply to the **next** session — there is no live tool
re-registration.

The REPL also supports `/steer`, `/attach`, `/wallet`, `/trade`, `/bridge`,
`/dev`, `/goal`, `/files`, `/mode`, `/prefs`, and `/reject`. `/goal` operates on
one goal; `/goals` shows the board. Use `/help <verb>` for their current syntax.
`/pending` includes tool approvals and pending contacts as well as self-evolution
proposals. `/approve` configures approval policy; it does not decide a pending
request.

User-facing replies and tool progress render as they arrive. Raw model-stream
content may contain structured internal state, so it remains buffered rather
than being printed as if it were a user-facing answer.

---

## Where configuration lives

`~/.polyrob/.env` is your global config, `./.polyrob/.env` is the per-project
override, and a profile replaces both. The full precedence order and the file
each command writes are in
[configuration.md §1](configuration.md#1-how-configuration-works); `polyrob
config path` prints them for the process you are running.

---

## Tips

- `polyrob run` is stateless — every call starts a new session. Use `--resume <id>` to continue one, or the REPL for a memory-carrying conversation.
- `polyrob session tail <id> --follow` in a second terminal shows what a goal or cron run is doing right now.
- To run a second bot on one machine, use a profile (`polyrob -P scout …`) rather than editing the global config.

### Terminal rendering and displayed metrics

The live footer describes the active provider/model; the initial banner records
what the session launched with. A successful fallback updates the footer without
changing your saved default. Footer usage is cumulative for the session; turn
summaries report that turn's main-agent steps/tools and usage delta. Costs are
estimates; missing pricing appears as unknown, including partially priced turns.

Long drafts wrap in an editor capped at ten rows. Narrow terminals preserve the
run status and mark clipped labels with an ellipsis. `NO_COLOR` keeps an uncolored
interactive prompt; `--plain`, redirected output, and `TERM=dumb` avoid cursor UI.
Raw model chunks can contain internal state, so replies are displayed through
typed messages or finalized answers rather than painting those chunks directly.
