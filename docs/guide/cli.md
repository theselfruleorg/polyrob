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
`profiles`=`profile`, `webgate`=`dashboard`, `init`=`setup`, `cron`=`job`, and
`soul`/`persona`/`avatar` are the
three `identity` subgroups also reachable at the top level. `polyrob knowledge`
is a deprecated alias for `polyrob kb export`. Entries marked **related** are
different command trees, not interchangeable aliases (`skills`/`skill`,
`owner`/`approvals`, and the identity subgroups). An installed pack can add its
own commands (the X pack adds `polyrob x-account`); they appear in the same list.

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
polyrob -p anthropic -m <model>
```

A persistent agent session. Type your goal and press Enter; the agent acts step
by step and prints each action. `Alt+Enter` (`Esc` then `Enter`) inserts a
newline, `Ctrl-L` repaints the screen, `Ctrl-C` interrupts the current turn,
`/exit` or `Ctrl-D` leaves. Slash commands: [chat-commands.md](chat-commands.md).

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

    <model> · 41.9k↑ 3.2k↓ · ctx 31% · cache 93% · $0.0412 · ready

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
| `--toolset` | `minimal`, `safe`, `default`, `research`, `trading_research`, `coding`, `development`, `browser`, `social`, `full`, `earn` |
| `--max-steps` | Maximum steps (default `INTERACTIVE_MAX_STEPS`, 100) |
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

`--json` emits the TYPED snapshot (every section with its `state` / `reason` /
`data`, plus the ranked health items) alongside the rendered prose, and honours
`--full` — without it the `report` field is the one-line pointer, not the
transcript. `--perms` audits the shared data home (group, group-write, setgid,
root-owned files); `--perms --strict` exits non-zero when it finds an offender.
The economics section of the snapshot is where you check whether prompt caching
is paying: it names the share of input tokens served from cache, the tokens
written *into* cache (a write costs more than an ordinary input token, so a
prefix rewritten fifty times and one written once read very differently), the
first-call cache hit rate, and the median uncached tokens per call.

### `polyrob auth` — provider credentials

```bash
polyrob auth add <provider>     # key or OAuth sign-in
polyrob auth status [--json]    # every provider: source, health, expiry
polyrob auth list               # credentials in the auth store
polyrob auth refresh <provider>
polyrob auth remove <provider>  # forgets the token; does not revoke it
```

An OAuth seat needs `LLM_OAUTH_ENABLED=true`. Read the terms-of-service warning
in [configuration.md §4](configuration.md#plans-that-need-a-sign-in-oauth) before
you connect one.

### `polyrob keys` — API access keys

```bash
polyrob keys create --name automation [--expires-days 30]
polyrob keys list
polyrob keys revoke <key-prefix>
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
polyrob config list              # every setting: preferences first, then flags (--group, --changed)
polyrob config search TEXT       # fuzzy name + description search
polyrob config explain KEY       # every layer that sets KEY, which one won, and for a gated flag its effective state
polyrob config path              # the env files this process reads
polyrob config edit              # open ~/.polyrob/.env in $EDITOR, then run the `check` pass
polyrob config check             # validate the env files against the catalog
polyrob config migrate           # copy secrets out of the legacy env files
```

`set` routes by the shape of the key — secret, env flag, or preference. The CLI,
the REPL `/config set KEY VALUE [--global] [--confirm]` and the console all write
through one path, so they validate and report the same way. In the REPL, a flag of
the autonomy loop or posture groups applies live in that session; every other flag
(money, approval, inbound access, the frozen ones) says `takes effect: restart`.
The read verbs take `--json`. How the routing and precedence work:
[configuration.md §2](configuration.md#2-where-a-setting-lives).

### `polyrob model`

```bash
polyrob model                              # the picker (provider, then model)
polyrob model list                         # models + which provider keys are present
polyrob model set-default                  # the same picker
polyrob model set-default anthropic <model>
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
agent's busy signals — a live turn, a running goal, a cron
job running or due within two minutes, the workspace lock — and waits, printing
what it is waiting on, up to `--wait-idle SECONDS` (default 1800). Still busy
after that: it refuses and names the reason. `--no-wait` refuses at once;
`--force` swaps under a busy agent anyway.

**With the background service running, it goes through the service.** The service
finishes its current work, restarts, and applies the update as it starts —
snapshot, install, migrate, verify, then the new version — and `--apply` waits for
its report (`✓ Updated and back online on 1.4.0`). A failed step, or a new
version that does not start, rolls back. `--no-restart` keeps the old behaviour:
refuse while a server process runs. With no service running it updates directly
and then names what still runs the OLD code (`polyrob service restart`, any
`polyrob*` system unit, a live REPL). Other options: `--channel stable|pre|git`,
`-y/--yes`, `--json`. The full story, and `/update` from chat:
[operate-the-service.md](operate-the-service.md).

### `polyrob service` — keep it running

```bash
polyrob service install     # systemd user unit (Linux) or launchd agent (macOS)
polyrob service status      # running version vs disk, open restart, waiting settings
polyrob service restart     # finish the current work, restart, report the version
polyrob service restart --now        # restart at once
polyrob service start | stop [--now]
polyrob service logs [-f] [-n 100]
polyrob service uninstall
```

Runs `polyrob gateway`, so every enabled surface starts from one unit. On Linux
`loginctl enable-linger $USER` keeps a user unit alive after you log out. The
unit pins `POLYROB_HOME` (and the active profile) explicitly, because a systemd
user unit inherits nothing from your shell.

`restart` asks the running service to finish its work (up to `--wait-idle
SECONDS`, default 600), exit with code 75 — the unit's `RestartForceExitStatus=75`
starts it again — and report back; busy past the wait, nothing restarts and the
reason is printed. `stop` lets a live turn finish (up to 60 s). As root, every verb
acts as the user that owns the service (`--user NAME` when several do), with the
user bus filled in. `/restart` and `/update` do the same from chat:
[operate-the-service.md](operate-the-service.md).

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
| `polyrob gateway` | Every enabled surface in one process, Feishu/Lark and DingTalk included (they have no command of their own). `--port` / `--host` (default `0.0.0.0:8080`) bind the shared webhook server; `--telegram-token`; `--skip telegram,email` leaves surfaces to their own process; `--no-autonomy` leaves the scheduled-job, goal and curator loops to another process; `--idle-when-empty` stays up with nothing to run |
| `polyrob serve` | The REST API (`--host`, `--port`, `--workers`; default `127.0.0.1:9000`) — see [api.md](api.md) |
| `polyrob dashboard` | The web console (`--host`, `--port`, default `127.0.0.1:5050`; `--posture local\|own_ops\|multitenant`, `--multitenant`, `--no-browser`). The console always asks for a sign-in, also on loopback: with no saved owner login it prints a one-run password in this terminal, and `--set-password` saves a lasting username and password to `~/.polyrob/.env`. Alias `polyrob webgate` — see [console.md](console.md) |

```bash
polyrob surfaces list [--probe]    # every surface: enabled flag, credentials, probe state
polyrob surfaces add <surface>     # ask for its credentials and write them (never echoed), then probe
polyrob surfaces probe <surface>   # prove a credential with a read (`all` = every surface)
```

`surfaces add` writes `~/.polyrob/.env`, or
`/etc/polyrob/polyrob.env` on a deployed box; `--env-file` names another file,
`--no-enable` leaves its `*_SURFACE_ENABLED` flag alone, and
`--no-probe` skips the read. Feishu/Lark and DingTalk: [feishu-dingtalk.md](feishu-dingtalk.md).
The agent reads and sends X direct messages with its tools, not with a surface
process: [x-twitter.md](x-twitter.md).

Signal identifies a sender by the account UUID (`sourceUuid`) that signal-cli
supplies, and refuses a message that carries only a phone number. Pair the owner
and approve contacts by their UUIDs.
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

`create`, `list`, `show`, `edit`, `pause`, `retry`, `stop`, `events` and `tree`.

```bash
polyrob goals create "Draft the weekly report" -b "…" -p 7 --acceptance report.md
polyrob goals list --status ready --json
polyrob goals pause <id>                 # hold it until `retry`
polyrob goals retry <id>                 # re-queue a blocked or paused goal (clears the failures)
polyrob goals stop <id> [--forever]      # once; --forever switches the title off for good
polyrob goals tree                       # goals with their sub-goals
polyrob goals events <id>                # the event timeline for one goal
```

`create` also takes `--parent`, `--tools`, `--triage`, `--force` and
`--memory-regime shared|scoped|sealed`. `/goal allow <id>` in chat re-arms a
title you stopped `--forever`. Jobs, goals, decisions and pauses:
[streams.md](streams.md).

### `polyrob job` — scheduled jobs

```bash
polyrob job new "every day 08:00" check the feed [--max-duration 180] [--user ID]   # or: 30m | check the feed
polyrob job list [--all] [--cancelled]          # next run, last outcome, who wrote it
polyrob job show <id>
polyrob job edit <id> --max-duration 1800     # raise/lower a job's hard cap (≤1800 s), applies from its next run
polyrob job edit <id> --schedule "every day 09:00" --tools a,b --rig NAME --deliver telegram
polyrob job pause <id>                        # the job's own state: shown as paused, no next run
polyrob job resume <id>
polyrob job run <id>                          # run it on the next scheduler pass (its schedule is kept)
polyrob job stop <id>                         # end it for good (the job records who stopped it)
polyrob job stop <id> --forever [--reason TEXT]   # also stop its lane: no new job in it except by you
polyrob job allow <lane>                      # re-arm a lane stopped with --forever
polyrob job prune [--cancelled-older-than 7d] [--dry-run] [--all]
polyrob job digest ["every day 08:00"] [--off] [--deliver telegram] [--days 1]
```

`polyrob cron` is an alias of `polyrob job` (with `schedule` and `cancel` as
aliases of `new` and `stop`).
When the scheduler is off, the terminal, Telegram and this console print the
same sentence and the one command that turns it on.

Schedule specs: a duration (`30m`), `every monday 09:00`, a 5-field cron
expression, an ISO timestamp for a one-shot, or a fixed N-day cycle
`every 3d from 2026-10-26T18:00Z` (that time, then every 3 days). A
day-of-month step such as `0 18 */3 * *` restarts on the 1st of every month,
so it fires on the 31st and again on the 1st; `job edit` says so when you set one.

`job edit` also takes `--priority money|ops`, `--preempts/--no-preempts`,
`--task-file` and `--post-spacing-hours`; `--tools none` clears an explicit
toolset. It goes through the same checks and audit record as a chat or agent
edit: an unknown delivery target or a schedule faster than 5 minutes is
refused, a stopped job cannot be edited, and an edit that changes nothing says so. `prune` deletes CANCELLED jobs older than the cutoff and nothing else —
a live, failed or completed job is never removed — and `--dry-run` lists exactly
the rows the delete would take.

Every scheduled job, goal and helper run stops at one step limit,
`AUTONOMOUS_MAX_STEPS`, and at `AUTONOMOUS_RUN_BUDGET_USD` of spend, helpers
included; the defaults are in [`docs/CONFIGURATION.md`](../CONFIGURATION.md). At the limit it reports once, and a run that
stops before it finished names the reason in its record, the CLI and the status.

The agent has the same verbs as tools when `cronjob` is in its tool set:
`cronjob_schedule`, `cronjob_list`, `cronjob_show` (the full task — `list`
truncates it), `cronjob_edit` and `cronjob_cancel`. `cronjob_edit` patches a
live job in place: it replaces one exact passage of the task (the passage must
occur once) and can change the schedule, cap, rig, delivery or pinned skills.
It never rewrites the whole task, so rules elsewhere in the task survive the
edit.

**What a job may do.** Every job carries the same standing authority: its rig and
tools are used as written, it may post on X without a per-run approval, and it may
moderate a room its task names. A job the agent schedules from an autonomous run may
ask only for tools inside the agent's self-goal ceiling, and only your own turn may
put a write verb on a job. Goals follow the same rule.

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
polyrob session prune [--days N] [--max N] [--dry-run] [--yes]
```

`prune` deletes the directory trees older than `SESSION_RETENTION_DAYS` (or `--days`).
It keeps a tree of any age that holds a registered file, that a live goal
names, or that a helper still owns, and it refuses the whole sweep when it cannot
read the goal board or the registry of produced files.

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
polyrob apps start <slug>        # run a stopped app again on its approved address
polyrob apps remove <slug> [--yes]   # retire it: container, files, record and address
polyrob apps logs <slug> [-n 50]
polyrob apps supervise [--once] [--interval N]   # the reconcile loop the unit runs
polyrob apps install [--domain D] [--publish-root DIR] [--no-publish] [--dry-run]
```

`install` sets up the serving side of durable apps and the static publish host
for this box's layout; `--dry-run` prints the plan and the files and changes
nothing.

`supervise` is the owner-run process that holds the Docker, nginx and firewall
privilege the agent never has. Setting it up:
[deployment-postures.md](deployment-postures.md).

### `polyrob skills` and `polyrob skill`

- `polyrob skills` — read and authoring: `list`, `validate`, `export`.
- `polyrob skill` — the install pipeline: `install <spec>`, `approve`, `list`, `info`, `remove`, `update`, `search`, and `tap add|list|remove`.

```bash
polyrob skills list
polyrob skills validate [id]
polyrob skill install acme/repo/pdf     # folder, owner/repo, git URL, or SKILL.md URL (--ref, --trust local|prompt)
polyrob skill approve pdf               # activate a quarantined install
polyrob skill update [name]             # re-fetch from the source and re-apply the trust rules
polyrob skill search pdf [--refresh]    # search the taps by name or description
polyrob skill tap add acme/skills       # a GitHub repo that publishes skills (owner/repo[/subdir])
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
polyrob kb reindex [--limit N] [--dry-run]    # index the documents the agent already wrote
```

`kb export` writes notes, episodes, skills, identity and goals as an
Obsidian-compatible markdown vault. `polyrob knowledge export` is a deprecated
alias for it.

### `polyrob workers`

```bash
polyrob workers list                 # approved workers and the ones waiting for you
polyrob workers show <id>
polyrob workers new <id> --description "…" --instructions "…" [--tools a,b] [--model M] [--provider P]
polyrob workers edit <id> …          # unset options keep their value
polyrob workers approve <id>         # a worker the agent proposed
polyrob workers remove <id>          # archived, never destroyed
```

Each one is a named, reusable helper that the agent dispatches by name. In the REPL,
`/workers stop <run-id>` and `/workers steer <run-id> <text>` act on a running one.

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

### `polyrob browser`

`install [--mode cdp|server] [--port N] [--listen IP] [--with-deps] [--dry-run]`,
`status`, `update [--with-deps]`, `render <file>`. The isolated browser service a
custody deployment connects to: `install` creates the service user, installs
Chromium, writes the unit, the browser data directory and the egress rules, and
starts it. `cdp` runs it on the agent's host; `server` runs a Playwright server on a second host that the agent
reaches over `BROWSER_WSS_URL`. `update` re-installs Chromium for the current
Playwright pin. See [self-hosting.md](self-hosting.md).

---

## Money

### `polyrob wallet`

```bash
polyrob wallet                    # addresses, balances, network, caps (--json, --no-balances)
polyrob wallet init               # create the wallet (--from-mnemonic / --from-seed to import)
polyrob wallet export [--venue V]  # reveal the seed and per-venue keys — TTY only, typed confirm
polyrob wallet overview           # identities, cached balances, limits, unresolved sends (--json)
polyrob wallet book               # the ledger against every money chain
polyrob wallet set-cap daily|per-tx USD [--env]
polyrob wallet bridge <from> <to> <amount> [--execute] [--token-in native|usdc] [--token-out native]
polyrob wallet bridges            # bridges not yet proven to have arrived
polyrob wallet deploy-token SYMBOL SUPPLY NAME… --chain <chain> [--execute]
polyrob wallet launch SYMBOL NAME… [--buy 0.1] [--logo URL] [--execute]
polyrob wallet curve TOKEN [--buy N | --sell N]
polyrob wallet claim TOKEN [--execute]          # creator fees the launchpad owes this wallet
polyrob wallet lp positions|pool|quote|add|remove|collect … --chain <chain>   # Uniswap v3 liquidity
polyrob wallet nft list|info|transfer|revoke … --chain <chain>  # non-fungibles this wallet holds
polyrob wallet asset add|list|verify …          # assets the treasury may be paid in
polyrob wallet dapp list|revoke <session_id>    # web-dapp wallet sessions
polyrob wallet pins [--chain C]                 # the tokens you pinned as THE symbol on a chain
polyrob wallet pin-token <chain> <address> <SYMBOL> [--note TEXT]
polyrob wallet unpin-token <chain> <address>
polyrob wallet submissions [--json]             # unresolved sends and the chain evidence for each
polyrob wallet release-submission <ref> --charge-usd N | --never-sent --reason TEXT
```

There is no default network: a verb that acts on one network needs `--chain`.

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

**`wallet pin-token`** records that one address IS the symbol on that network: the
token reads as verified, and a swap refuses any other contract that calls itself
that symbol there. A new pin replaces the old one. The agent has no verb for this;
`/wallet trust <chain> <address> [SYMBOL] go` in chat writes the same store.

**`wallet submissions`** lists sends that never resolved. Any row there blocks
EVERY send (network and x402) until it is settled. `release-submission` writes it
to the audit log the limits read — at the worst-case `--charge-usd`, or $0 with
`--never-sent --reason` when you KNOW no money moved — and refuses while the network
evidence says the transaction may still land.

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
polyrob owner invite <surface> <address> <session_id>   # register a correspondent of a session
polyrob owner approve …                         # approve a pending correspondent (--all [surface])
polyrob owner allow <surface> <target>          # outbound send permission
polyrob owner deny <surface> <target>
polyrob owner allowlist
polyrob owner pair pending|approve <code>|revoke <user>
polyrob owner groups allow|deny|list|mode|set|role|roles|tools|audit|admins|tail|service …
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
box. `owner groups roles <surface> <chat>` lists the ROLES POLYROB recorded for
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
polyrob owner memory scopes list|show|promote|purge <label>   # quarantined goal/cron findings
```

An identity draft (`self_context`, `owner_doc`, `contract`) is decided as
`<id>@<rev>`, the revision `owner pending` printed. If the draft changed since, or
you were never sent it, the decision is refused: run `owner pending` again.

A scope holds what one goal tree (`goal:<root>`) or one scheduled job (`cron:<job>`)
learned, out of every other chat's recall until the root goal completes
verified, when the dispatcher promotes it. `promote` moves a scope's findings into
shared recall by hand (threat-scanned when the scan is on); `purge` deletes them.

**Money**

```bash
polyrob owner invoices [-n 50] [--status pending] [--all-tenants]
polyrob owner invoices --stuck          # rows held in settling that no reaper releases
polyrob owner settle <id> [--tx-hash HASH]
polyrob owner reopen <id> --confirm     # a never-submitted settling claim -> pending
polyrob owner refunded <id> [--tx HASH] [--note TEXT]   # refund_due -> refunded
polyrob owner deposits [--user ID] [--onchain] [--json]  # deposit sweep states (read-only)
polyrob owner deposit-resolve <id> --sent|--not-sent --confirm   # decide a sweep stuck in sweeping
polyrob owner invoice-resolve <id> --unpaid|--refund-due --confirm   # decide a held invoice
polyrob owner sub list|cancel|reactivate <id>   # reactivate: grace/suspended -> active for one period, no payment
polyrob owner paid list|show|enable|disable|price|asset|offers|cancel …
```

`owner paid` is what a room SELLS: `price <chat> mute 0.50` sets one verb's
price, `asset <chat> <id>` names the asset it is paid in (pin it first with
`wallet asset add`), `enable` refuses while nothing is priced, and `list` with
no `--chat` shows every credit the treasury OWES a payer whose effect never
landed.

`deposit-resolve --sent` needs `--tx HASH` when no tx was recorded. After a
local-signer sweep it names any open wallet submission: an open one refuses
every wallet send until `wallet release-submission` or `wallet book` closes it.

**Control**

```bash
polyrob owner halt / polyrob owner resume            # aliases of autonomy pause / resume
polyrob owner pause-entries / resume-entries         # no NEW treasury positions; exits still run
```

`polyrob approvals` manages the approval-gated action set and renders on the
`owner` row in `--help`. `list` shows every gate with the source that set it
(pref / env / posture) and the active provider; `add <action>` writes the gate
immediately (tightening needs no review); `remove <action>` does NOT remove it —
removing a gate loosens policy, so it QUEUES the change for your review and
prints the `polyrob owner promote pref_change …` that applies it. Only a
pref-added gate can be removed at all: an env or posture gate is
operator-controlled and is explained rather than removed.

### `polyrob identity`

The instance's identity, in three subgroups:

- `polyrob identity soul init` — scaffold the operator-authored SOUL docs.
- `polyrob identity persona init <slug> [--from NAME]` · `list` · `show [slug]` — the character (voice) layer.
- `polyrob identity avatar` — `show`, `set <file|url>`, `set --nft chain:contract:id`, `clear`, `push`.
- `polyrob identity register --chain <chain> [--execute]` — mint this instance's
  ERC-8004 identity from its own wallet. It is a dry run by default. ⚠️
  `register()` is NOT idempotent: the verb reads the CHAIN for an existing token
  before it signs, and a FAILED read refuses rather than reading as
  "not registered".
- `polyrob identity set-uri <agent_id> --chain <chain> [--execute]` — update the published
  registration document for an agentId you already hold. Mints nothing.

The avatar is one image the instance shows on every surface; a new instance
shows the polyrob mark until you set your own. `set` takes a file,
a URL, or an NFT (its metadata `image`); `push` sends it to X, Telegram (the bot,
or a group with `--telegram-chat`) or Discord.
POLYROB generates no faces. `polyrob soul`, `polyrob persona` and
`polyrob avatar` reach the same three groups directly.

### `polyrob profile`

Lifecycle: `create`, `list`, `use`, `show`, `path`, `rename`, `delete`, `adopt`,
`alias`. Distribution: `export`, `import`, `install`, `update`, `info`. Full
guide: [profiles.md](profiles.md).

### `polyrob pack`

```bash
polyrob pack list                    # every installed pack and its state
polyrob pack info <id>               # what it declares (from its pack.toml) and its state
polyrob pack search [query]          # the curated pack index (a local file; no network)
polyrob pack install <source> [--accept-capabilities]   # an index id, git+https://…@<sha>, or a path
polyrob pack enable|disable <id>     # saved for the next process
polyrob pack doctor                  # refusals and errors, with reasons
polyrob pack remove <id>             # prints the command that removes it
```

Packs are optional extensions; the first-party ones ship inside `polyrob`. Full
model: [packs.md](packs.md).

---

## Other

- `polyrob datagen run|export` — run a batch of tasks as rollouts and export a label-filtered trajectory corpus.
- `polyrob x-account capture-session|import-session|status|signup|oauth-login|oauth-status|oauth-import|oauth-refresh` — the agent's own X account: the owner login ceremony (`--out` writes portable storage state), the server-side import of a desktop capture or of the `auth_token`/`ct0` cookies, the stored session, the supervised signup flow, and the OAuth2 half (`oauth-login` runs the PKCE
  ceremony, `oauth-status` shows the stored grant and its scopes, `oauth-import`
  takes a token bundle, `oauth-refresh` renews one).

---

## Slash commands (REPL)

Inside the REPL every command starts with `/`. `/help` lists them grouped;
`/help <verb>` explains one. The same commands run on Telegram, the other chat
apps and the console. The full list, with the seats each one runs on:
[chat-commands.md](chat-commands.md).

---

## Where configuration lives

`~/.polyrob/.env` is your global config, and a profile's `.env` replaces it. A
project folder's `.env` or `./.polyrob/.env` is not read: opening an untrusted
checkout cannot change your keys or policy. The full precedence order and the file
each command writes are in
[configuration.md §2](configuration.md#2-where-a-setting-lives); `polyrob
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
