# Jobs, goals, decisions and pauses (reference)

Depth for the "Autonomy" section of `polyrob-user-guide/SKILL.md`. The owner
holds FOUR things when you work without the owner: a **job**, a **goal**, a
**decision** and a **pause**. Explain every question about standing work with
these four words — author, status and its reason, schedule and cost are details
of one of them. Never call a job a "rail", a "stream" or an "objective": those
words are retired.

Everything here is opt-in and off by default outside the `POLYROB_LOCAL`
single-user CLI profile. How autonomy is turned on, which loop belongs to
which setting, and the run limits: `references/configuration.md` (generated).
This file describes the mechanisms, not this session's live state: confirm
what is on with `agent_status`. Every owner verb, with its help line, is in
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
- `/job edit <id> schedule <when>`, `/job edit <id> what <text>` or
  `/job edit <id> grant tools=a,b` changes one field and keeps the rest;
  `/job pause <id>` / `/job resume <id>` is the job's own switch;
  `/job run <id>` runs it on the next scheduler pass; `/job stop <id>` removes it.
- A schedule is a duration (`30m`), a weekday time (`every monday 09:00`), a
  five-field cron expression, or an ISO time for a one-time run.
- A job runs only while the scheduler is on (`/jobs` says so when it is not,
  and names the fix). Delivery of the result to Telegram/email/Twitter is a
  separate opt-in (`CRON_DELIVERY_ENABLED`). At `AUTONOMY_POSTURE=full` a
  `change_gated` job skips its paid model call when nothing it watches
  changed — a $0 run.
- Your own tools: `cronjob_schedule/list/show/edit/run_now/cancel` (the tool
  must be in this session's tool_ids). How to shape a job, and the $0 watch:
  the `standing-work` skill. To change a live job, `cronjob_show` it and
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
  turns it back on. `/goal grant <id> tools=a,b` is the owner's grant of tools
  to a goal that already exists.
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
- Your own tools: `goal_create`, `goal_list` and `goal_show` in every main session (a leaf returns a proposal);
  `goal_ask` holds your own goal for an owner decision. `goal_update`,
  `goal_unblock` and `goal_cancel` work only in the owner's own turn — in a goal
  or scheduled-job run they refuse; name `/goal retry <id>` or `/goal stop <id>` to the
  owner instead. Inside a run: the `goal-run` skill. A goal you write for yourself
  may ask only for the safe set (filesystem, task, browser, perplexity, MCP,
  AnySite, coding, `web_fetch`, Twitter, email, `message`, `x402_invoice`,
  knowledge, read-only `defi_data`); under an armed autonomous mode that set can
  include `defi_trade`, bounded by the money limits and the pause.

## Decision — what waits on the owner

- `/inbox` lists every open decision, blocking ones first: approvals, your asks,
  quotes and choices on action cards, and signer approvals. `/pending`, `/asks` and `/cards`
  show parts of the same list.
- The owner answers with the item's tap, `/approve <id>`, `/reject <id>`, or
  `/fulfill <id>` for an ask — which puts the goals behind it back in the queue.
- You open a decision; you never answer one. To put a verb in front of the
  owner, use `propose_action` — it becomes a card the owner confirms.

## Pause — stop now, keep everything

- `/pause` stops all autonomous work; `/pause <scope>` one scope;
  `/pause <job id|goal id>` one job or goal. `/resume` lifts it. No restart;
  jobs keep their schedules and goals keep their place.
- Only on the owner's explicit request, in the owner's own turn, you call
  `autonomy_control(action="pause", scopes=[…])` and quote the result; a
  goal or scheduled-job run cannot pause or resume. Only the owner resumes (`/resume`). A
  plain "stop" in chat is a message, not a switch.

## What standing work may do

Every job and goal runs its tools as written, posts without a per-run tap and
moderates only the room its text names. A job or goal you create from an
autonomous run may ask only for the safe set; only the owner's own turn may ask for any
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

- **Self-wake** — re-enters an idle chat as a forged continuation turn (e.g.
  after a background delegation finishes). Bounded by
  `SELF_WAKE_MAX_REENTRIES` and `SELF_WAKE_IDLE_BACKOFF_SEC`. A self-wake turn
  is always framed as untrusted content — it can never auto-activate a skill
  or write active identity docs.
- **Background review** — a cheap auxiliary reviewer every
  `BG_REVIEW_INTERVAL` productive turns; leaf, non-blocking.
- **Curator** — a no-LLM tick that ages out unused self-authored skills
  (`CURATOR_STALE_DAYS`, `CURATOR_ARCHIVE_DAYS`).
- **Owner digest** (`OWNER_DIGEST_ENABLED`) — a $0 periodic summary on the
  owner's channel (`digest.channel`), outside `digest.quiet_hours`.

Which setting turns each loop on, and the limits that stop a run (steps and
AI cost per run): `references/configuration.md`. The credit sentinel
(`CREDIT_SENTINEL_ENABLED`) stops autonomous runs after a real provider
billing failure and releases after `CREDIT_SENTINEL_RELEASE_HOURS`.

## Notes

- None of these loops run from the web console process alone (`polyrob
  dashboard`) — they need a worker that starts the shared autonomy runtime
  (`polyrob serve` / `polyrob gateway` / the REPL under `POLYROB_LOCAL`).
- A goal/cron circuit-breaker `blocked` status is a genuine stop, not a
  silent retry storm — surface it to the owner rather than trying to force
  it through.
