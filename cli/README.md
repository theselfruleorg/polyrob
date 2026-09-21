# CLI Package — terminal-native `polyrob`

_Last reviewed: 2026-09-21. For the user-facing command reference see [../docs/guide/cli.md](../docs/guide/cli.md); for env flags see ../docs/CONFIGURATION.md._

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
- Owner-control and money seats: `autonomy`, `approvals`, `apps`, `cron`,
  `goals`, `keys`, `surface`, `wallet`, `identity`, `finance`, `profile`.

## Design principles

- **One agent core.** The REPL and `polyrob run` drive the *same* Task agent the
  API and network surfaces run — the CLI is a peer surface, not a wrapper.
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
├── config_store.py       # Key-aware provider/model resolution from ~/.polyrob/.env
│                         #   (auto-detects the provider whose API key is present)
├── inventory.py          # Product-facing tool catalog backing `polyrob tools`
├── keys.py               # provider-key presence guard + onboarding preflight
│                         #   (NOT the API-key seat — that is commands/keys.py)
├── _admin_home.py        # admin_data_dir(write=…) + @as_root_option: the ONE
│                         #   data home an owner/admin verb acts on (031 / 057)
├── session_paths.py      # session_directory(): the ONE session-dir lookup —
│                         #   it REFUSES an ambiguous id rather than guessing
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
│   ├── serve.py dashboard.py    #   local REST API + POLYROB Console
│   ├── telegram.py email.py owner.py  # chat-surface runners + owner admin
│   ├── approvals.py apps.py cron.py goals.py  # the autonomy control seats
│   ├── keys.py           #   `polyrob keys` — API keys for A2A / OpenAI-compat
│   ├── surface.py        #   per-surface circuit breakers + health
│   ├── wallet.py wallet_lp.py   #   the money seat (caps, bridge, deploy,
│   │                     #     launch, claim, nft, lp, asset, dapp)
│   └── identity.py       #   SOUL / persona / avatar + ERC-8004 registration
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

- **One data home per owner verb.** Every verb that reads or writes the DAEMON's
  stores resolves through `cli/_admin_home.py::admin_data_dir(write=…)`, which
  adopts the DEPLOYED home when the shell declares none and REFUSES when it
  cannot read it — never a second resolver, never a `POLYROB_DATA_DIR or "data"`
  fallback. `write=True` also arms the euid guard: a root-run mutating verb on a
  deployed box refuses with the `sudo -u polyrob-agent` remedy (or `--as-root`).
- **One tenant per owner verb.** The tenant is
  `core.admin_data_home.admin_owner_principal()`, not the shell-local
  `resolve_identity()` and never a literal `"local"` — the two differ exactly
  where it matters, in an SSH shell on a deployed box.
- **A read never creates a store.** Owner reads existence-guard their sqlite
  files: opening one to answer "none" leaves behind a decoy the service never
  writes.
- **Honest empty states.** One grammar (`cli/ui/candy.py::empty`). An unreadable
  source renders its reason; it never renders `0`, `$0.00` or `[]`.
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
