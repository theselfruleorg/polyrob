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
`profiles`=`profile`, `webgate`=`dashboard`, and `soul`/`persona`/`avatar` are the
three `identity` subgroups also reachable at the top level. `polyrob knowledge`
is a deprecated alias for `polyrob kb export`. Entries marked **related** are
different command trees, not interchangeable aliases (`skills`/`skill`,
`owner`/`approvals`, and the identity subgroups).

**Owner verbs refuse inside the agent.** When the agent runs `polyrob` from its
own shell, only the read verbs work — `version`, `doctor`, `finance`, `journey`,
`--help`, and the `list`/`show`/`status`-style leaves such as `goals list` or
`config get`. Every other verb (`config set`, `wallet export`, `owner pair`,
`run`, the REPL, the surface launchers, …) exits with a plain refusal. A verb
added later is owner-only until it is named a read.

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

#### The status bar

The line at the bottom of the REPL is the live meter:

    claude-sonnet-4-6 · 41.9k↑ 3.2k↓ · ctx 31% · cache 93% · $0.0412 · ready

`ctx` is how full the context window is, read from the provider's own token
count once it has reported one. `cache` is the share of prompt tokens the
provider served from cache on the last request — it is **omitted, never shown
as `0 %`**, when the seat does not report cached tokens, so a blank there means
"unknown", not "nothing cached". `/context` is the long form of both.

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

### `polyrob setup` — first-run setup

```bash
polyrob setup                 # `polyrob init` is the same command
polyrob setup --no-prompt --owner aria --instance-id aria --openai-key sk-...
```

Seven sections: provider keys, default model, toolset, template/persona, owner
pairing, autonomy and guardrails, and the chat surface you want to reach it on.
Writes `~/.polyrob/.env` (mode 600), adds `.polyrob/` to this project's
`.gitignore`, seeds the identity docs if there are none, creates `~/.agents/skills`
for your own skills, and records the install in
`~/.polyrob/.polyrob-bootstrap.json`. Re-run it any time.

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
polyrob doctor --flags                # the public env flags (plus any you set), resolved value and source
polyrob doctor --flags --all          # every tier: public, advanced, internal
polyrob doctor --flags --group memory
polyrob doctor --flags --search x402
polyrob doctor --changed              # only flags set away from their default
```

Plain `doctor` leads with the same snapshot every other seat renders. `--group`,
`--search` and `--changed` each imply `--flags`, combine with AND, search every
tier, and filter `--json` too; no match prints `no flags match …` and exits 0.
A bare `--flags` shows only the public tier and the flags you set; `--all`
shows the rest.

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

### `polyrob doctor`

`polyrob doctor` leads with the status snapshot every other seat renders;
`--full` adds the whole check transcript. `--json` emits the TYPED snapshot
(every section with its `state` / `reason` / `data`, plus the ranked health
items) alongside the rendered prose, and honours `--full` — without it the
`report` field is the one-line pointer, not the transcript. `--flags` dumps
every registered env flag with its resolved value and source; `--perms` audits
the shared data home. The economics section of the snapshot is where you check
whether prompt caching is paying: it names the share of input tokens served
from cache, the tokens written *into* cache (a write costs more than an
ordinary input token, so a prefix rewritten fifty times and one written once
read very differently), the first-call cache hit rate, and the median uncached
tokens per call.

### `polyrob keys` — API access keys

```bash
polyrob keys create --name automation [--expires-days 30]
polyrob keys list
polyrob keys revoke rob_…
```

These keys authenticate the A2A and OpenAI-compatible API surfaces through the
`X-API-KEY` header. `create` prints the full secret once; `list` shows only its
prefix and label. `revoke` accepts the 12-character prefix shown by `list`.
Keys expire after 90 days by default; `--expires-days` accepts 1–365 days.

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

`set` routes by the shape of the key — secret, env flag, or preference. The CLI,
the REPL `/config set KEY VALUE [--global] [--confirm]` and the console all write
through one path, so they validate and report the same way. In the REPL, a flag of
the autonomy loop or posture groups applies live in that session; every other flag
(money, approval, inbound access, the frozen ones) says `takes effect: restart`.
The read verbs take `--json`. How the routing and precedence work:
[configuration.md §1](configuration.md#2-where-a-setting-lives).

### `polyrob model`

```bash
polyrob model                              # the picker (provider, then model)
polyrob model list                         # models + which provider keys are present
polyrob model set-default                  # the same picker
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
`python -m pip install -U "polyrob[<your extras>]"`); schema migrations run on the
next start either way. The reinstall carries the extras this install already has
(`.[docs,gemini,…]`) under `requirements.lock`, so an update never drops a
capability or moves a pinned package.

**It waits for the agent to wrap up.** Before swapping code, `--apply` reads the
same signals the production deployer reads — a live turn, a running goal, a cron
job running or due within two minutes, the workspace lock — and waits, printing
what it is waiting on, up to `--wait-idle SECONDS` (default 1800). Still busy
after that: it refuses and names the reason. `--no-wait` refuses at once;
`--force` swaps under a busy agent anyway. After a successful `--apply` it names
what is still running the OLD code — the background service, any `polyrob*`
system unit, and any live REPL or chat surface. A resident server process is a
different matter: waiting never ends it, so the update still refuses there and
prints the stop/start steps for your units. Other options: `--channel
stable|pre|git`, `-y/--yes`, `--json`.

### `polyrob service` — keep it running

```bash
polyrob service install     # systemd user unit (Linux) or launchd agent (macOS)
polyrob service status      # installed? running? which surfaces would start?
polyrob service uninstall
```

Runs `polyrob gateway`, so every enabled surface starts from one unit. On Linux
`loginctl enable-linger $USER` keeps a user unit alive after you log out. The
unit pins `POLYROB_HOME` (and the active profile) explicitly, because a systemd
user unit inherits nothing from your shell.

### `polyrob uninstall`

```bash
polyrob uninstall            # command, PATH entry and service. Data is KEPT.
polyrob uninstall --purge    # also the data home, after a typed confirmation
```

It will not delete the virtualenv it is running from or the package manager's
copy — it prints the exact command for those. `--purge` names **both** homes
first: `~/.polyrob` (per user — keys, settings, the **agent wallet seed**) and
the data home, `~/.polyrob/data` unless `POLYROB_DATA_DIR` names another (memory,
goals, cron, identity). A `./.polyrob` data folder that an older release left in
a project directory is never touched; delete it yourself. Export the mnemonic
(`polyrob wallet export`) before `--purge` if that wallet ever held funds.

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
| `polyrob dashboard` | The web console (`--host`, `--port`, default `127.0.0.1:5050`; `--posture local\|own_ops\|multitenant`, `--multitenant`, `--no-browser`). The console always asks for a sign-in, also on loopback: with no saved owner login it prints a one-run password in this terminal, and `--set-password` saves a lasting username and password to `~/.polyrob/.env`. Alias `polyrob webgate` — see [console.md](console.md) |

Group chats have their own setup and rules: [groups.md](groups.md).

---

## Autonomy & work

### `polyrob autonomy` — the dial

```bash
polyrob autonomy status [--json]        # the posture card: every axis, pauses, loop state
polyrob autonomy on [--mode supervised|autonomous]
polyrob autonomy off
polyrob autonomy pause [WORD…] [--for 6h]
polyrob autonomy resume [WORD…]
polyrob autonomy halt                   # alias of pause with no words
```

`on`/`off` write `AUTONOMY_ENABLED` to `~/.polyrob/.env` (the active config
home, the only env file the CLI reads) and apply to the next process. `--global`
is still accepted and changes nothing. In the REPL,
`/autonomy on` / `/autonomy off` write the same flag, apply it live, and start or
stop the loops of that session with no restart.
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
polyrob cron edit <id> --max-duration 1800     # raise/lower a job's hard cap (≤1800 s), applies from its next run
polyrob cron edit <id> --slot-cap 6            # $0 preflight: skip the run while the ledger's Open-positions table holds ≥6 rows (`--slot-cap none` drops it)
polyrob cron cancel <id>
polyrob cron prune [--cancelled-older-than 7d] [--dry-run] [--all]
polyrob cron digest ["every day 08:00"] [--off] [--deliver telegram] [--days 1]
```

Schedule specs: a duration (`30m`), `every monday 09:00`, a 5-field cron
expression, or an ISO timestamp for a one-shot.

The agent has the same verbs as tools when `cronjob` is in its tool set:
`cronjob_schedule`, `cronjob_list`, `cronjob_show` (the full task — `list`
truncates it), `cronjob_edit` and `cronjob_cancel`. `cronjob_edit` patches a
live job in place: it replaces one exact passage of the task (the passage must
occur once) and can change the schedule, cap, rig, delivery or pinned skills.
It never rewrites the whole task, so rules elsewhere in the task survive the
edit. An autonomous run may edit only jobs it scheduled itself; a job you
scheduled changes only when you ask in chat or through `polyrob cron edit`.

**Who wrote a job decides what it may do.** A job you schedule — `polyrob cron
schedule`/`digest`, `/cron add`, the console, `/groups service` — is stamped
owner-authored. A job the agent schedules is agent-authored, and so is one
created in an owner turn that had already read third-party content (a page, a
post, a mail, a tool result); the agent tells you when that happens. An
agent-authored job runs under agent limits: the agent's tool ceiling, and no X
post, room moderation, money rig or write job without your approval on each run.
To make one yours, run `/adopt <id>` in chat or the REPL: it shows the whole
task, schedule, rig, tools, target and every payload key, and your Confirm on
the action card stamps it owner-authored. Pinned skills are bound by a content
digest, so a skill edited after adoption makes the job the agent's again until
you re-adopt it. Adoption never grants money tools; grant those yourself.
Goals follow the same rule.

Rows from before 1.3 carry no stamp and now count as the agent's. Stamp them
once (it changes nothing on a second run):

```bash
python -m cron.stamp_authorship --dry-run    # print the plan, write nothing
python -m cron.stamp_authorship              # apply it  [--db PATH] [--cutoff ISO]
```

Unstamped rows created on or after the cutoff become owner-authored; older ones
become agent-authored and appear in `/adopt` for you to confirm.

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

`list`, `status`, `show <tool>`, `permissions`, `export-catalog`, plus
`enable <id>` / `disable <id>`. `status` names the gate and the remedy for every
tool the session cannot use.

`enable` writes the tool's own flag, resolved from the documented flag catalog —
a tool whose flag cannot be resolved that way (the browser, which rides its pip
extra) is refused rather than guessed at. A **money** tool is refused too: it is
a deliberate owner grant, and the spend caps and approval lane are the other
half of it. A high-impact tool is enabled with a warning naming the gates that
stay closed anyway — a sub-agent, a self-wake turn and a correspondent-tainted
session still cannot reach it.

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
polyrob wallet overview           # identities, cached balances, limits, unresolved sends (--json)
polyrob wallet book               # the ledger against every money chain
polyrob wallet set-cap daily|per-tx USD [--env]
polyrob wallet bridge <from> <to> <amount> [--execute] [--token-out native]
polyrob wallet bridges            # bridges not yet proven to have arrived
polyrob wallet deploy-token SYMBOL SUPPLY NAME… [--chain base|solana] [--execute]
polyrob wallet launch SYMBOL NAME… [--buy 0.1] [--logo URL] [--execute]
polyrob wallet curve TOKEN [--buy N | --sell N]
polyrob wallet claim TOKEN [--execute]          # creator fees the launchpad owes this wallet
polyrob wallet lp positions|pool|quote|add|remove|collect …    # Uniswap v3 liquidity
polyrob wallet nft list|info|transfer|revoke …  # non-fungibles this wallet holds
polyrob wallet asset add|list|verify …          # assets the treasury may be paid in
polyrob wallet dapp list|revoke <session_id>    # web-dapp wallet sessions
```

Every write verb quotes and asserts by default and moves nothing without
`--execute`. `wallet export` is never available to the agent. The complete money
model — caps, approval lanes, invoicing, x402, trading:
[payments.md](payments.md).

**`set-cap` writes the live preference.** By default it sets
`budget.wallet_daily_usd` / `budget.wallet_per_tx_usd`, which the spend gate
re-reads immediately — no restart — and then prints the EFFECTIVE cap it
measured afterwards. The daily preference is min-merged with the operator
value, so it can only tighten; RAISING the daily envelope, and disabling it
(`set-cap daily none`), need `--env` and a restart.

**`wallet claim`** collects the creator tax a token you launched has earned.
The escrow credits an ADDRESS, not a token, so one claim collects what every
token this wallet launched has earned; naming a token only tells the verb which
curve to read the escrow address from.

**`wallet nft transfer` is always owner-approved** and irreversible, and
`--max-usd` bounds the transaction FEE — an NFT has no price this can cap.
`wallet nft list` needs an indexer (`ALCHEMY_API_KEY`); without one it says it
could not look, which is not the same as "you own nothing".

**`wallet dapp revoke`** flips the durable record. A bridge still live inside a
running agent process holds its own in-memory envelope until that session ends
or the agent runs `dapp_disconnect`.

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
polyrob owner groups allow|deny|list|mode|set|role|admins|tail|service …
polyrob owner correspondents --history <surface> <address>   # the stored transcript
```

⚠️ **A paired sender is treated as you, the owner, on that surface.** Pairing is
issued by you: an unknown sender DMs the bot and receives a code, and you run
`polyrob owner pair approve <code>`. Approve only a code that your own account
received, never a code that somebody else gives you. Codes expire after 15
minutes and work once. A paired sender gets full owner routing, but no host
execution (`run_code`, `run_tests`) in local mode. Remove a pairing with
`polyrob owner pair revoke <user_id>`.

`owner correspondents`, `owner approve --all` and `owner invoices` are scoped to
this instance's owner tenant; pass `--all-tenants` to see every bucket on the
box. `owner groups admins <surface> <chat>` lists the ROLES POLYROB recorded for
a room — the platform's own admin list needs a live connection to that surface
(`/groups admins here` on Telegram).

**Pending and asks**

```bash
polyrob owner inbox [-n N]        # everything waiting on a decision, blocking first
polyrob owner pending             # self-evolution proposals
polyrob owner show-pending <kind> <id>
polyrob owner promote <kind> <id> / polyrob owner reject <kind> <id>
polyrob owner asks [--json]       # what the agent needs from you to unblock work
polyrob owner fulfill <id> [answer…]   # the words after the id are your answer
polyrob owner missed [-n N]       # notices the delivery rail could not send live
```

**Money**

```bash
polyrob owner invoices [-n 50] [--status pending] [--all-tenants]
polyrob owner settle <id> [--tx-hash HASH]
polyrob owner sub list|cancel
polyrob owner paid list|show|enable|disable|price|asset|offers|cancel …
```

`owner paid` is what a room SELLS: `price <chat> mute 0.50` sets one verb's
price, `asset <chat> <id>` names the asset it is paid in (pin it first with
`wallet asset add`), `enable` refuses while nothing is priced, and `list` with
no `--chat` shows every credit the treasury OWES a payer whose effect never
landed.

**Control**

```bash
polyrob owner halt / polyrob owner resume            # aliases of autonomy pause / resume
polyrob owner pause-entries / resume-entries         # no NEW treasury positions; exits still run
polyrob owner pause-streams / resume-streams         # no new stream reseeds
```

`polyrob approvals` manages the approval-gated action set and renders on the
`owner` row in `--help`. `list` shows every gate with the source that set it
(pref / env / posture) and the active provider; `add <action>` writes the gate
immediately (tightening needs no review); `remove <action>` does NOT remove it —
removing a gate loosens policy, so it QUEUES the change for your review and
prints the `polyrob owner promote pref_change …` that applies it. Only a
pref-added gate can be removed at all: an env or posture gate is
operator-controlled and is explained rather than removed.

`cron prune` deletes CANCELLED jobs older than a cutoff and nothing else — a
live, failed or completed job is never removed. `--dry-run` lists exactly the
rows the delete would take.

### `polyrob identity`

The instance's identity, in three subgroups:

- `polyrob identity soul init` — scaffold the operator-authored SOUL docs.
- `polyrob identity persona init <slug> [--from NAME]` · `list` · `show [slug]` — the character (voice) layer.
- `polyrob identity avatar` — `show`, `set <file|url>`, `set --nft chain:contract:id`, `clear`, `push`.
- `polyrob identity register [--chain base] [--execute]` — mint this instance's
  ERC-8004 identity from its own wallet. It is a dry run by default. ⚠️
  `register()` is NOT idempotent: the verb reads the CHAIN for an existing token
  before it signs, and a FAILED read refuses rather than reading as
  "not registered".
- `polyrob identity set-uri <agent_id> [--execute]` — update the published
  registration document for an agentId you already hold. Mints nothing.

The avatar is one image the instance shows on every surface; a new instance
shows the polyrob mark until you set your own. `set` takes a file,
a URL, or an NFT (its metadata `image`); `push` sends it to X or Discord.
POLYROB generates no faces. `polyrob soul`, `polyrob persona` and
`polyrob avatar` reach the same three groups directly.

### `polyrob profile`

Lifecycle: `create`, `list`, `use`, `show`, `path`, `rename`, `delete`, `adopt`,
`alias`. Distribution: `export`, `import`, `install`, `update`, `info`. Full
guide: [profiles.md](profiles.md).

---

## Other

- `polyrob datagen run|export` — run a batch of tasks as rollouts and export a label-filtered trajectory corpus.
- `polyrob x-account capture-session|import-session|status|signup|oauth-login|oauth-status|oauth-import|oauth-refresh` — the agent's own X account: the owner login ceremony (`--out` writes portable storage state), the server-side import of a desktop capture or of the `auth_token`/`ct0` cookies, the stored session, the supervised signup flow, and the OAuth2 half (`oauth-login` runs the PKCE
  ceremony, `oauth-status` shows the stored grant and its scopes, `oauth-import`
  takes a token bundle, `oauth-refresh` renews one).

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
| `/compact` (`/compress`) | Age old tool results to a pointer, then compact what is left through the model, in the background |
| `/export <format> [output]` | Export this session's data |
| `/replay <session-id>` | Replay a session's feed — a visual history, not a re-attach |
| `/new` | Start fresh — clear the history without leaving the REPL |
| `/start` | A welcome and a short tour of what this instance can do |

**needs you**

| Command | Description |
|---|---|
| `/inbox [n]` | Everything waiting on a decision from you, blocking first |
| `/pending [show\|approve\|reject <kind> <id>]` | The agent's pending self-evolution proposals |
| `/approve [<id>\|all]` | Decide what is waiting on you — bare lists it, `<id>` approves that one |
| `/reject <id>` | Discard one pending item |
| `/gates [list\|add <action>\|remove <action>]` | Which actions need your approval before they run |
| `/asks [list]` | Open asks — what the agent needs from you to unblock work |
| `/fulfill <id> [answer]` | Mark an ask fulfilled and unblock its goals; the words after the id are your answer, carried into the goal's retry prompt |
| `/missed [n]` | Messages the delivery rail could not send live |

**work**

| Command | Description |
|---|---|
| `/goals` | Goal board summary |
| `/adopt [<id>]` | Make a cron job or goal the agent wrote your own: bare lists them, `<id>` shows one in full, then confirm on its action card |
| `/cron` (`/crons`) `[list\|show <id>\|add <schedule> <task…> [tools=a,b] [target=<address> chain=<chain>]\|edit <id> schedule\|task <value>\|cancel <id>]` | Durable scheduled runs: list, show, add, edit one field, or cancel |
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
| `/check <address> [chain]` · `/check <TICKER>` | Read any wallet or token: a wallet's holdings, or a token's identity, price, safety screen and holders |
| `/book` | The ledger against every money chain: one verdict, then what disagrees |
| `/invoices [<status>]` | Agent invoices (x402 receivables). Statuses: `pending`, `settling`, `completed`, `settled_no_tx`, `expired`, `refund_due` |
| `/settle <id> [tx-hash]` | Attest an invoice as paid |
| `/deploy <SYMBOL> <supply> <name…> [on <chain>] [go]` | Deploy a fixed-supply token; quotes unless you add `go` |
| `/launch <SYMBOL> <name…> [buy <amount>] [go]` | Launch a token on the Pons launchpad; quotes unless you add `go` |
| `/lp positions\|pool\|quote\|add\|remove\|collect` | Uniswap v3 liquidity; dry-runs unless you add `go` |
| `/claim <token> [go]` | Claim the creator fees a launched token has earned |
| `/nft list\|info\|transfer\|revoke …` | Collectibles this wallet holds |
| `/dapp list\|revoke <session-id>` | Web pages the wallet is armed for, and how to cut one off |
| `/paid …` | Paid room actions: status, pricing, offers |
| `/send <amount> <native\|token-address> to <address> on <chain> [max <usd>] [go]` | Send native value or a token to an address; quotes unless you add `go`. The quote is an action card: `/card_<id>_ok` sends exactly that quote, at most the quote + 5%. Your own send passes the autonomous ceiling and the pause; the per-transaction and daily caps still bind |
| `/swap <amount> <native\|token> to <token> on <chain> [slippage <bps>] [max <usd>] [go]` | Swap one token for another; the quote (route, minimum out, value, caps) is an action card, and Confirm swaps at most the quote + 5%. `/send` or `/swap` alone builds the order step by step: type the numbered card token for each choice |
| `/writeoff <chain> <address> [go] [reason]` | Write a holding off: the loss is its recorded cost, and nothing is sold |
| `/unquarantine <chain> <address> [go]` | Undo the quarantine of a look-alike holding, so it counts as open again |
| `/cards [<id> ok\|no\|re\|1-6]` | Open action cards: confirm a quote, refresh it, cancel it, or answer the agent's question. Typing a card's token (`/card_<id>_ok`) is the same as tapping its button |

**control**

| Command | Description |
|---|---|
| `/pause [word…] [for 6h]` | Pause autonomous work now — everything, or `trading`, `background`, `messages`, `deploying`, or a raw scope |
| `/halt` | `/pause` with no words |
| `/resume [word…]` | Lift the pause, everything or one scope |
| `/autonomy [on\|off] [--global]` | Autonomy loops, scheduled cron jobs and open goals; `on`/`off` switch the loops in this session (live, no restart) and persist the choice |
| `/cancel` | Stop the task running now |
| `/run [pause\|resume\|stop <n>]` | What runs in the background now, numbered; pause, resume or stop one run |
| `/mode` | The effective autonomy posture, and how to change it |
| `/dev <text>` | Message the on-host dev and ops loop directly |

**remember**

| Command | Description |
|---|---|
| `/memory [search <query>]` | The active memory provider; `search` recalls cross-session |
| `/kb [list [collection]\|search <query>]` | List and search the knowledge base |
| `/journey [window]` (`/recap`) | What the agent did, learned, changed, and earned |
| `/self` (`/soul`) | The instance identity: SOUL + SELF docs, read-only |

**look**

| Command | Description |
|---|---|
| `/status` | The status snapshot — health first, then session, goals, loops, wallet |
| `/meter` | THIS turn's token meter (the REPL-only counter `/status` used to be) |
| `/doctor` | The `polyrob doctor` health report |
| `/contacts [<surface> <address>]` | Who the agent has written to, and the transcript with one of them |
| `/thread [n \| <hours>h]` | Your conversation with the agent across every session and rail, newest last |
| `/files [n]` | Recent files the agent produced |
| `/why [n]` | The last n refusals (default 5, at most 20): when, which tool, and the gate's reason word for word |
| `/context` | What is in the window: per-slot token counts and share, a row for the one-shot messages riding this turn, and the provider's own figures for the last request (prompt · cached · written · uncached). A `≈` marks a number that is still a local estimate |
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
| `/groups [allow\|deny\|list\|use\|mode\|set\|role\|tail\|service\|admins]` | Room presence admin (default-DENY) |
| `/mute` · `/unmute` · `/ban` · `/unban` | Silence a room, or bound one member's reach in it |

**display**

| Command | Description |
|---|---|
| `/verbose` | Toggle the live trace: steps, tools, reasoning |
| `/quiet` | Mute or restore the default tool transcript |
| `/avatar` `[show\|set <path\|url>]` | Show or set the agent avatar |
| `/identity register\|set-uri [on <chain>] [go]` | The instance's ON-CHAIN identity (ERC-8004). It is no longer an alias of `/self` |

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
proposals. **`/approve <id>` DECIDES one of them** — the same decider Telegram
and the console run, over the same union `/pending` reads. Approval *policy*
(which actions need a tap at all) is `/gates`; `/approve list|add|remove` still
routes there as a deprecated alias and says so.

User-facing replies and tool progress render as they arrive. Raw model-stream
content may contain structured internal state, so it remains buffered rather
than being printed as if it were a user-facing answer.

---

## Where configuration lives

`~/.polyrob/.env` is your global config, and a profile's `.env` replaces it. A
project folder's `.env` or `./.polyrob/.env` is not read: opening an untrusted
checkout cannot change your keys or policy. The full precedence order and the file
each command writes are in
[configuration.md §1](configuration.md#2-where-a-setting-lives); `polyrob
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

Signal inbound identity uses the account UUID (`sourceUuid`) supplied by signal-cli.
A phone-only envelope is refused. On upgrade, re-pair the owner and re-approve contacts
using their UUIDs; review phone-based block/allow records before enabling the surface.
Historical phone-based conversations remain separate and are not automatically merged.
