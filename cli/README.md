# CLI Package — terminal-native `polyrob`

_Last reviewed: 2026-07-12. For the user-facing command reference see [../docs/guide/cli.md](../docs/guide/cli.md); for env flags see ../docs/CONFIGURATION.md._

## Overview

The `cli` package is the terminal-native surface for polyrob. It is a **first-class
surface** that runs the *same* Task agent as the API — not a thin wrapper. It
provides:

- `polyrob run <task>` — one-shot, non-interactive execution.
- `polyrob` / `polyrob chat` — an interactive REPL with live tool transcripts,
  slash commands, and a bottom-anchored status toolbar.
- `polyrob init` / `doctor` / `config` — setup, diagnostics, and configuration.
- Surface runners and admin: `serve`, `dashboard`, `telegram`, `email`, `owner`,
  `kb`, plus `tools`, `skills`, `model`, `session`.

## Design principles

- **One agent core.** The REPL connects to the agent through the same `Surface`
  contract the network surfaces use (`cli_surface.py::CLISurface`), so CLI output
  ordering matches every other surface.
- **The renderer owns pixels.** All bubble/dedup/finalize logic lives in the Rich
  renderer; the surface only forwards the unified outbound stream into it.
- **Fail-open rendering.** A rendering error must never break the agent loop.
- **Local-first.** Under `POLYROB_LOCAL=true` the CLI container defaults the *safe*
  autonomy flags on as a group; the server never does this.

## Package structure

```
cli/
├── polyrob.py            # Click entry point (console_scripts) — registers every
│                         #   subcommand; bare invocation / `chat` open the REPL
├── cli_surface.py        # CLISurface — the CLI as a Surface contract consumer;
│                         #   forwards the outbound stream into the renderer
├── config_store.py       # Key-aware provider/model resolution from ~/.polyrob/.env
│                         #   (auto-detects the provider whose API key is present)
├── inventory.py          # Product-facing tool catalog backing `polyrob tools`
├── keys.py               # API-key helpers
├── gitignore.py          # ensures ./.polyrob is gitignored in a project
├── commands/             # One Click module per subcommand (thin entry points)
│   ├── _bootstrap.py     #   shared container/bootstrap helpers
│   ├── _errors.py        #   uniform error formatting
│   ├── run.py            #   `polyrob run` (--model/-m, --provider/-p, --tools/-t,
│   │                     #     --toolset, --max-steps, --plain, --verbose/-v)
│   ├── chat.py           #   REPL launcher (run_repl)
│   ├── init.py           #   first-run setup wizard
│   ├── doctor.py         #   environment diagnostics
│   ├── config.py         #   show / set / path
│   ├── model.py          #   set-default <provider> <model> (alias: models)
│   ├── session.py        #   cancel <id> (alias: sessions)
│   ├── tools.py skills.py kb.py
│   ├── serve.py dashboard.py    #   local REST API + single-user web dashboard
│   └── telegram.py email.py owner.py  # chat-surface runners + owner admin
└── ui/                   # REPL rendering + input
    ├── app.py            #   prompt_toolkit PromptSession + bottom toolbar
    ├── persistent_loop.py#   bottom-anchored persistent-input loop
    │                     #     (gated POLYROB_PERSISTENT_INPUT)
    ├── rich_renderer.py  #   Rich inline-scrollback renderer (the pixel owner)
    ├── blocks.py         #   pure RenderEvent → Rich renderable builders
    ├── activity.py       #   the single transient "working…" indicator
    ├── lifecycle.py      #   SSOT for "is a turn active, and for how long"
    ├── live_hooks.py events.py event_registry.py
    ├── banner.py dialog.py bootstrap_notice.py
    └── commands/         #   slash-command registry + handlers
        ├── registry.py   #     CommandRegistry / Command
        └── handlers.py   #     /help /status /model /memory /autonomy /goals /finance … (registry-generated /help is the authoritative list)
```

## Key invariants

- **Provider/model auto-resolves** from whichever API key is present
  (`config_store.resolve_provider_model`); explicit `-p`/`-m` or `DEFAULT_PROVIDER`
  still win.
- **Interactive idle-gate.** The REPL marks itself busy per turn so background
  goal/cron tickers skip a tick while a live turn runs — they share one CWD, so
  this prevents file corruption (`core/interactive_gate.py`).
- **Sub-agent output is suppressed** in the transcript (a producer-side concern;
  the surface carries no agent id).
- **Tool calls are visible by default** (`→ name(args)` / `✓ name·dur·preview`);
  `/quiet` mutes them, `/verbose` shows the raw trace. Args/previews are
  secret-scrubbed before display (`cli/ui/secrets.py`).

## Related

- Surface contract: `core/surfaces/surface.py`
- Autonomy tickers started by the REPL under local mode: `core/autonomy_runtime.py`
- Network chat surfaces: [`../surfaces/`](../surfaces/README.md)
