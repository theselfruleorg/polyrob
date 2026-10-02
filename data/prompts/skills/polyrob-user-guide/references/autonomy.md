# Autonomy loops (reference)

Depth for the "Autonomy" section of `polyrob-user-guide/SKILL.md`. Everything
here is opt-in and off by default outside the `POLYROB_LOCAL` single-user CLI
profile (which flips the safe subset on as a group). Always confirm what's
actually enabled with `agent_status` / `polyrob doctor --flags` — this file
describes the mechanisms, not this session's live config.

## The loops

- **Goals** (`GOALS_ENABLED`) — a durable, cross-session backlog stored on
  disk (`goals.db`), survives restarts. A goal is claimed and run (safe under
  multiple workers via an atomic claim), has a circuit breaker (repeated
  failures move it to `blocked` rather than looping forever), and a daily
  quota (`GOAL_DAILY_QUOTA`, also settable via the `goals.daily_quota`
  preference). Owner-facing: `polyrob goals create|list|show|cancel|pause|
  resume|retry`, REPL `/goals`. Agent-facing: the `goal_create/list/show/
  cancel` tool actions.
- **Cron** (`CRON_ENABLED`) — scheduled runs (duration, weekly, 5-field cron,
  one-shot ISO time), stored in `cron.db`. A job actually runs the full agent
  loop (`CRON_RUN_LOOP`, default ON — a job that used to only create an idle
  session now really executes). Delivery of the result out-of-band (Telegram/
  email) is a separate opt-in (`CRON_DELIVERY_ENABLED`). Owner-facing:
  REPL `/cron`. Agent-facing: `cronjob_schedule/list/show/edit/cancel` (tool must be in
  the session's tool_ids). To change a live job, `cronjob_show` it and
  `cronjob_edit` it (patch `old_text` → `new_text`) — never cancel and re-type it.
- **Self-wake** (`SELF_WAKE_ENABLED`) — re-enters an idle session as a forged
  continuation turn (e.g. after a background delegation finishes, or a goal
  wants to report back). Bounded by a per-session depth cap
  (`SELF_WAKE_MAX_REENTRIES`) and an idle backoff
  (`SELF_WAKE_IDLE_BACKOFF_SEC`) so it can't ping-pong. A self-wake turn is
  always framed as untrusted/forged content in your own context — it can
  never auto-activate a skill or write active identity docs (only quarantine
  proposals).
- **Background review** (`BACKGROUND_REVIEW_ENABLED`) — forks a cheap
  auxiliary-model reviewer every `BG_REVIEW_INTERVAL` productive turns; a
  leaf, non-blocking, fail-open self-check.
- **Curator** (`CURATOR_ENABLED`) — a mechanical (no-LLM) tick that ages out
  unused self-authored skills: stales them after `CURATOR_STALE_DAYS`,
  archives after `CURATOR_ARCHIVE_DAYS` (reactivates on reuse). System/user-
  authored skills are never touched.
- **Owner digest** (`OWNER_DIGEST_ENABLED`) — a periodic, deterministic ($0,
  no model call) summary of what happened, delivered via the owner's
  preferred channel (`digest.channel` preference: telegram/email) and
  respecting a quiet-hours window (`digest.quiet_hours` preference, e.g.
  `"23-08"`).
- **Change-gate** (`WAKE_CHANGE_GATE`) — a cron job marked `change_gated`
  skips its (paid) model call entirely when nothing observable has changed
  since the last tick — a genuine $0 no-op instead of a wasted LLM call.

## The two posture axes

Two independent knobs decide how much initiative and compute you have —
neither is the deployment posture (local/own_ops/multitenant, that's the web
console's auth model, see `references/surfaces.md`):

- **`AUTONOMY_POSTURE`** (`silent` default | `owner-visible` | `full`) — how
  much your autonomous work is verified and surfaced to the owner. `silent`:
  today's defaults, autonomy runs but isn't specially verified or narrated.
  `owner-visible`: your autonomous work becomes evidence-verified and
  reported (episodic digest, reflection on session close). `full`:
  owner-visible PLUS time-based initiative (the cron ticker actively runs),
  plus the wake change-gate.
- **`AGENT_COMPUTE_POSTURE`** (0-3) — how much host/compute capability you
  have, independent of trust or autonomy. `0 confined`: today's ephemeral
  docker sandbox, no persistent shell. `1 sandbox-dev`: a persistent,
  installable, HTTP-testable dev sandbox (`shell`/`process` tools, pip
  installs actually stick, publishes a few loopback ports so you can test
  your own server). `2 self-maintain`: posture 1 plus the `self_env` tool
  (install_dep/read_source/patch_source/git_pull/restart_service — approval-
  gated, never raw bash). `3 host`: full host access, single-tenant/local-only,
  not yet wired as a distinct backend.

## Compute-spend backstop

There is no rate-based compute budget cap — a trailing-window ceiling can't
protect a finite provider balance, so nothing throttles LLM-call burn rate by
itself. The backstop is reactive instead: a **credit sentinel**
(`CREDIT_SENTINEL_ENABLED`, on by default) latches after a real
billing/quota failure (e.g. a provider 402) from an autonomous run so it
doesn't keep retrying a dead credit line; it auto-releases after
`CREDIT_SENTINEL_RELEASE_HOURS`. Wallet spend (a different pocket — the
agent's own crypto, not the owner's API bill) stays gated separately: see
"Budgets" in `references/money-and-safety.md`.

## Notes

- None of these loops run from the web console process alone (`polyrob
  dashboard`) — they need a worker that starts the shared autonomy runtime
  (`polyrob serve` / `polyrob gateway` / the REPL under `POLYROB_LOCAL`).
- A goal/cron circuit-breaker `blocked` status is a genuine stop, not a
  silent retry storm — surface it to the owner rather than trying to force
  it through.
