# Jobs, goals, decisions and pauses (reference)

Depth for the "Autonomy" section of `polyrob-user-guide/SKILL.md`. The owner
holds FOUR things when you work without him: a **job**, a **goal**, a
**decision** and a **pause**. Explain every question about standing work with
these four words — author, status and its reason, schedule and cost are details
of one of them. Never call a job a "rail", a "stream" or an "objective": those
words are retired.

Everything here is opt-in and off by default outside the `POLYROB_LOCAL`
single-user CLI profile (which turns the safe subset on as a group). This file
describes the mechanisms, not this session's live state: confirm what is on
with `agent_status`. Every owner verb, with its help line, is in
`references/verbs.md`.

## Which one does the owner want?

| | Job | Goal | Decision | Pause |
|---|---|---|---|---|
| **Trigger** | A time the owner set (`/job new <when> <what>`) | The owner or you put it on the board (`/goal new <what>`); the dispatcher starts it when a slot is free | You need the owner's answer before you act | The owner sends `/pause`, or a limit stops the work |
| **Lives in** | The job list (`/jobs`) | The goal board (`/goals`) | The owner's inbox (`/inbox`) | The pause record (`/status`) |
| **Ends when** | The owner stops it (`/job stop <id>`), or a one-time job has run | It is done, or the owner stops it (`/goal stop <id>`) | The owner answers it | The owner sends `/resume` |
| **Best for** | The same work again and again at set times | One piece of work, done once, in the background | A spend, a post, a new rule, a grant | Stopping all work, or one job or goal, now — without losing it |

## Job — the same work at set times

- `/jobs` lists every job with its next run and last outcome.
- `/job new <when> <what>` creates one: quote a schedule that has spaces, or put
  `|` between the schedule and the work. `tools=a,b` grants it tools;
  `target=<token address> chain=<chain>` is the one token it may buy.
- `/job edit <id> schedule <when>` or `/job edit <id> task <text>` changes one
  field and keeps the rest; `/job pause <id>` / `/job resume <id>` is the job's
  own switch; `/job run <id>` runs it once now; `/job stop <id>` removes it.
- A schedule is a duration (`30m`), a weekday time (`every monday 09:00`), a
  five-field cron expression, or an ISO time for a one-time run.
- Needs `CRON_ENABLED` (on by itself at `AUTONOMY_POSTURE=full`). A job runs
  the full agent loop (`CRON_RUN_LOOP`). Delivery of the result to
  Telegram/email/Twitter is a separate opt-in (`CRON_DELIVERY_ENABLED`). At
  `full`, `WAKE_CHANGE_GATE` lets a `change_gated` job skip its paid model
  call when nothing it watches changed — a $0 run.
- Your own tools: `cronjob_schedule/list/show/edit/cancel` (the tool must be in
  this session's tool_ids). To change a live job, `cronjob_show` it and
  `cronjob_edit` it (patch `old_text` → `new_text`) — never cancel and re-type it.
- A group can run a catch-up job over its own messages:
  `/groups service here every 30m`.

## Goal — one piece of work, done once

- `/goal new <what>` puts it on the board; `/goals` shows the board as
  `NOW · STUCK · NEXT · OVER`, each line with who asked for it (`you launched
  it`, `you asked for it`, `I made this one up`).
- `/goal show <id>` prints the result, the cost, the status with its reason and
  the decision it waits on. `/goal retry <id>` clears the failures and puts it
  back in the queue.
- `/goal stop <id>` stops this run once; `--forever` switches the title OFF (no
  job adds it again and you cannot create it again). `/goal allow <id|title>`
  turns it back on.
- A goal has a title, a body (the instructions the run reads), a priority
  `1`–`10` (higher first), an optional acceptance line (what *done* must prove —
  a goal that cannot be verified completes as *done (unverified)*), and an
  optional tools list, used as written.
- Pace: the `goals.daily_quota`, `goals.max_concurrent` and
  `goals.notify_on_done` settings apply live; `GOAL_DAILY_QUOTA` and
  `GOAL_MAX_CONCURRENT` (default 2) are the env ceiling.
- **Blocked is not dead.** After `GOAL_MAX_RETRIES` failures a goal is
  `blocked`, with its reason on its line. A provider-outage block goes back to
  the queue within `GOAL_BLOCKED_PROVIDER_RETRY_MIN` minutes; any other block
  waits for `/goal retry` or ages out after `GOAL_BLOCKED_MAX_AGE_DAYS`. Tell
  the owner the reason — never try to force it through.
- Your own tools: `goal_create/list/show/cancel`. A goal you write for yourself
  may ask only for the safe set (filesystem, task, browser, perplexity, MCP,
  AnySite, coding, `web_fetch`, Twitter, email, `message`, `x402_invoice`,
  knowledge, read-only `defi_data`); under an armed autonomous mode that set can
  include `defi_trade`, bounded by the money limits and the pause.

## Decision — what waits on the owner

- `/inbox` lists every open decision, blocking ones first: approvals, your asks,
  quotes and choices on action cards, and signer approvals. `/pending`, `/asks` and `/cards`
  show parts of the same list.
- He answers with the item's tap, `/approve <id>`, `/reject <id>`, or
  `/fulfill <id>` for an ask — which puts the goals behind it back in the queue.
- You open a decision; you never answer one. To put a verb in front of him, use
  `propose_action` — it becomes a card he confirms.

## Pause — stop now, keep everything

- `/pause` stops all autonomous work; `/pause <scope>` one scope;
  `/pause <job id|goal id>` one job or goal. `/resume` lifts it. No restart;
  jobs keep their schedules and goals keep their place.
- You may pause your own work (`autonomy_control(pause)`); only the owner
  resumes. A plain "stop" in chat is a message, not a switch.

## What standing work may do

Every job and goal runs its tools as written, posts without a per-run tap and
moderates only the room its text names. A job or goal you create from an
autonomous run may ask only for the safe set; only his own turn may ask for any
rig or put a write verb on a job. Under the `supervised` money regime no job or
goal moves funds unattended.

## Answering "what runs next / what changed / which skill / why not"

- **What runs next, tonight, while I am away** — `agent_status` (the loops
  section and the health first), and `cronjob_list` for each job's next run.
- **What changed in your last update** — `agent_status(release_notes=
  'unreleased')` for what is past the last release, `'latest'` for the running
  release; on a deploy `'latest'` leads with the unreleased part. If no
  CHANGELOG ships with this install, say so.
- **Which skill did you use** — this run: the `<skill>` blocks in your context;
  an earlier run: `recent_activity` names each run's skills.
- **Why did you not do X** — `agent_status` (security: refusals and their top
  reasons; health: blocked goals and open decisions). The owner's own `/why`
  lists each refusal.
- Never answer "no" or "I can't" from memory: check `agent_status` and
  `references/verbs.md` first.

## Other loops

- **Self-wake** (`SELF_WAKE_ENABLED`) — re-enters an idle chat as a forged
  continuation turn (e.g. after a background delegation finishes). Bounded by
  `SELF_WAKE_MAX_REENTRIES` and `SELF_WAKE_IDLE_BACKOFF_SEC`. A self-wake turn
  is always framed as untrusted content — it can never auto-activate a skill
  or write active identity docs.
- **Background review** (`BACKGROUND_REVIEW_ENABLED`) — a cheap auxiliary
  reviewer every `BG_REVIEW_INTERVAL` productive turns; leaf, non-blocking.
- **Curator** (`CURATOR_ENABLED`) — a no-LLM tick that ages out unused
  self-authored skills (`CURATOR_STALE_DAYS`, `CURATOR_ARCHIVE_DAYS`).
- **Owner digest** (`OWNER_DIGEST_ENABLED`) — a $0 periodic summary on the
  owner's channel (`digest.channel`), outside `digest.quiet_hours`.

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
  gated, never raw bash; an approved restart_service asks the service for the
  drained restart — it finishes this turn, restarts and reports to the owner). `3 host`: full host access, single-tenant/local-only,
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
