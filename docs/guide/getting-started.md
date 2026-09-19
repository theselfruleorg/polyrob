# Getting started

POLYROB is a self-hosted autonomous AI agent you run on your own machine. This
page takes you from nothing to a working agent, and points at the guide that
owns each subject after that.

The default instance is named `polyrob`, and the command is `polyrob`.

---

## Quick start

```bash
pipx install "polyrob[all]"
polyrob init                        # keys, model, toolset, owner
polyrob doctor                      # catches a missing or malformed key first
polyrob run "summarize https://example.com"
```

That last command fetches the page with the lightweight `web_fetch` tool, so no
browser install is needed. Full browser automation is opt-in
([below](#optional-the-browser-engine)).

`polyrob init` also binds an **owner** — the identity that autonomy, approvals
and self-evolution answer to. Interactively it asks for an instance id and an
owner id. To script it:

```bash
polyrob init --no-prompt --owner aria --instance-id aria --openai-key sk-...
```

`--owner` alone backfills the instance id and the reverse. The pairing lands in
`~/.polyrob/.env` as `POLYROB_OWNER_USER_ID` / `POLYROB_INSTANCE_ID`, and
`polyrob doctor` reports both.

---

## Installing

**pipx (recommended)** — a clean, isolated install:

```bash
pipx install "polyrob[all]"
```

**pip with the extras you want** — smaller, explicit:

```bash
pip install polyrob                      # core agent, keyword memory, CLI
pip install "polyrob[browser]"
pip install "polyrob[server]"
pip install "polyrob[browser,server]"
```

**From source** — for contributors:

```bash
git clone https://github.com/theselfruleorg/polyrob
cd polyrob
pip install -e ".[dev,all]"
```

### Extras

| Extra | What it adds | Size |
|---|---|---|
| *(none)* | Core agent, keyword memory, CLI | ~50 MB |
| `server` | FastAPI REST API + console | +20 MB |
| `browser` | Playwright browser automation | +100 MB |
| `memory-vector` | Hybrid vector recall | +500 MB |
| `crypto` | Web3, x402, the EVM venues | +10 MB |
| `solana` | Solana signing and trading | +15 MB |
| `telegram` | Telegram surface | +5 MB |
| `twitter` | X / Twitter integration | +5 MB |
| `voice` | Voice transcription (faster-whisper) | +50 MB |
| `dev` | Tests, linting, build tooling | +30 MB |
| `all` | Everything above | ~700 MB |

Python 3.11 or newer. pipx works on macOS, Linux and Windows PowerShell alike;
on Windows a plain virtualenv (`python -m venv venv`) is equally fine.

---

## Connecting a provider

You need one LLM provider. `polyrob init` asks for OpenRouter first, because one
OpenRouter key reaches most models:

```bash
# ~/.polyrob/.env
OPENROUTER_API_KEY=sk-or-...
# or ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY
```

Three things worth knowing:

- **You can skip `polyrob init`.** The first interactive `polyrob run` or
  `polyrob chat` with no usable key runs the same key wizard inline, then offers
  to finish full setup. It never prompts in CI, a script, or a piped run.
- **A subscription or sign-in plan** (Claude Pro/Max, ChatGPT Codex, Copilot,
  z.ai, Kimi and others) is connected with `polyrob auth add <provider>`;
  `polyrob auth status` shows every credential's source, health and expiry. Read
  the terms-of-service warning in
  [configuration.md §3](configuration.md#plans-that-need-a-sign-in-oauth) first.
- **No key at all?** Declare any OpenAI- or Anthropic-compatible endpoint in
  `~/.polyrob/providers.yaml` — a local Ollama needs no key:

  ```yaml
  providers:
    ollama:
      base_url: http://127.0.0.1:11434/v1
      auth_type: none
      transport: chat_completions
      default_model: qwen3-coder:30b
      models: [qwen3-coder:30b]
  ```

  Then `polyrob run -p ollama "hello"`. Full rules:
  [configuration.md §3](configuration.md#your-own-endpoint-providersyaml).

DeepSeek has no standalone bootstrap path (its direct client's tool calling is
unreliable); reach it through OpenRouter with a `deepseek/deepseek-chat` model.

### Optional: the browser engine

```bash
# Recommended pipx install (macOS/Linux):
"$(pipx environment --value PIPX_LOCAL_VENVS)/polyrob/bin/playwright" install chromium
# Windows PowerShell:
# & "$(pipx environment --value PIPX_LOCAL_VENVS)\polyrob\Scripts\playwright.exe" install chromium
# A normal virtualenv or pip install:
python -m playwright install chromium
# on a fresh Linux server, also pull the system libraries (needs sudo):
# python -m playwright install --with-deps chromium
```

Only the Playwright `browser` tool (clicking, forms, multi-step navigation)
needs this. `web_fetch` handles "read this page" with no browser at all.

### Verify

```bash
polyrob doctor
```

It reports the pause state, ranked health issues and every status section:
which provider keys are present and usable, what `polyrob run` will actually
resolve to, the memory backend and its optional dependencies, workspace
isolation, and whether autonomy is on. Fix what it flags before going further.

---

## Running your first task

```bash
polyrob run "summarize https://example.com"
polyrob run "search for AI automation trends and summarize the top 5 results"
polyrob run "analyze the code in ./src for security issues"
polyrob run "go to producthunt.com, find the top 3 AI tools, build a comparison table"
```

Each `polyrob run` is a fresh session. `--resume <id>` continues one.

For anything exploratory, use the REPL instead:

```bash
polyrob
```

Type your goal and press Enter. The agent acts step by step and prints each
action and result. `Alt+Enter` (or `Esc` then `Enter`) inserts a newline,
`Ctrl-L` repaints the screen, `Ctrl-C` interrupts the current turn, and `exit`
or `Ctrl-D` leaves.

The startup banner is two quiet lines:

```
$ polyrob
● polyrob vX.Y.Z · claude-sonnet-4-5 (anthropic)
  session a1b2c3d4 · tools filesystem, task · /help · /session
```

### The dozen slash commands you will use

```
/help                    the grouped command list; /help <verb> explains one
/status                  live session status: tokens, cost, context
/usage                   authoritative usage and cost breakdown
/model <provider> <model>  swap the model live and persist it as the default
/memory [search <query>]   the memory provider, or recall across sessions
/tools                   the agent's registered tools
/goals                   the goal board summary
/autonomy                loops, scheduled cron jobs, open goals
/inbox                   everything waiting on a decision from you
/pause [word…] [for 6h]  stop autonomous work now; /resume lifts it
/compact                 compact the conversation through the model
/exit                    leave the REPL
```

The full list: [cli.md](cli.md#slash-commands-repl).

---

## Configuring it

`polyrob config set KEY VALUE` is the one write command; it routes a secret, an
environment flag and a preference to the right file and tells you which one it
wrote and when the value applies.

```bash
polyrob config show          # the merged result, secrets redacted
polyrob config path          # the files this process reads, highest first
polyrob config explain KEY   # every layer that set KEY, and which one won
polyrob model set-default    # interactive picker for provider + model
```

Where each file lives and how precedence resolves:
[configuration.md §1](configuration.md#1-how-configuration-works).

### Turning things on

Out of the box the agent acts on your messages and nothing else. Two switches
govern the rest:

```bash
polyrob config set POLYROB_LOCAL true       # the CLI sets this for you
polyrob autonomy on                         # writes AUTONOMY_ENABLED=true
```

`POLYROB_LOCAL` turns on the **interactive** tools you drive; the
**self-directed loops** — goals, self-wake, the curator, writable skills — need
`AUTONOMY_ENABLED` as well. The four axes, what each one moves, and how to stop
it again: [configuration.md §5](configuration.md#5-the-autonomy-dial).

---

## Where data lives

```
~/.polyrob/                 # your global home
├── .env                    # keys and flags
├── cli.json                # default provider/model from `model set-default`
├── providers.yaml          # your own endpoints (optional)
└── profiles/               # named profiles, each a full home

./.polyrob/                 # this project's runtime data (the default root)
├── .env                    # project overrides
├── memory.db               # cross-session memory
├── goals.db                # the goal board
├── cron.db                 # scheduled runs
├── conversations.db        # per-correspondent conversation log
├── telemetry_events.db     # events, costs, wallet spend
└── sessions/
    └── session-abc123/
        ├── workspace/      # files the agent creates
        ├── screenshots/
        ├── feed/
        └── logs/
```

Those five are examples: `core/db_manifest.py::SIDECAR_DB_NAMES` is the
authoritative list and names **37** sidecar stores (surfaces, dedup cursors,
artifacts, apps, bridges, the session registry…), beside the relational `bot.db`
whose path `DB_PATH` sets. `polyrob update --apply` and the boot-time migration both
snapshot that whole set together, so a rollback never restores a half of it.

`POLYROB_DATA_DIR` moves the runtime root; a named profile replaces both homes
at once ([profiles.md](profiles.md)). Nothing leaves your machine except calls
to the LLM provider you configured and any integration you switch on.

---

## Updating

```bash
polyrob update --check     # exit 0 up to date, 10 newer, 1 unknown/error
polyrob update             # the exact steps for YOUR install method
polyrob update --apply     # git/editable installs: snapshot -> install -> migrate -> verify
polyrob update --rollback  # restore the most recent snapshot
```

`--apply` is the automated path for a git checkout or an editable git install. Any
other install method (pip, pipx, systemd, Docker) prints the exact manual command for
that method and exits non-zero rather than pretending — `pipx upgrade polyrob` for a
pipx install, for example. Schema migrations run on the next start either way. Every
`--apply` takes a WAL-safe snapshot first, and `--rollback` needs one of those (or a
boot migration's) to exist; what a snapshot covers and what `--rollback` restores are
in [upgrading.md](upgrading.md#the-safety-net). Options in full:
[cli.md](cli.md#polyrob-update).

---

## Troubleshooting

| Symptom | What to run |
|---|---|
| "No API key found" | `polyrob init`, or `polyrob auth add <provider>` for a sign-in plan |
| The wrong provider answers | `polyrob model list`, then `polyrob model set-default` |
| "Browser not available" | `pip install "polyrob[browser]"`, then `python -m playwright install chromium` |
| Recall feels shallow | `pip install "polyrob[memory-vector]"`; the log names the missing extension |
| A setting seems ignored | `polyrob config explain KEY`, then `polyrob doctor --flags --changed` to see everything you moved off its default |
| The agent refuses a tool | `polyrob tools status` names the gate and the remedy |
| Autonomy "does nothing" | `polyrob autonomy status` — check the master switch, the posture and any active pause |
| Running two bots collides | give each one a profile: `polyrob profile create scout`, then `polyrob -P scout …` |

Still stuck: [GitHub Issues](https://github.com/theselfruleorg/polyrob/issues)
or [Discussions](https://github.com/theselfruleorg/polyrob/discussions).

---

## Next

- [cli.md](cli.md) — every command and slash command
- [configuration.md](configuration.md) — how settings work, and the ones you will touch
- [owner-controls.md](owner-controls.md) — stopping, pausing and approving
- [skills.md](skills.md) — reusable procedures the agent loads on demand
- [groups.md](groups.md) — putting the agent in a group chat
- [payments.md](payments.md) — the wallet, invoicing and trading
- [security-model.md](security-model.md) — what actually stops the agent
- [self-hosting.md](self-hosting.md) · [deployment-postures.md](deployment-postures.md) — running it on a server
- [api.md](api.md) · [architecture.md](architecture.md) · [examples](../examples.md) · [comparison](../comparison.md)
