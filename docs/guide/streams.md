# Jobs, goals, decisions and pauses

You hold four things when the agent works without you in the room. Everything
else — who wrote a piece of work, its status and its reason, its schedule, its
cost — is a detail of one of these four.

| | Job | Goal | Decision | Pause |
|---|---|---|---|---|
| **Trigger** | A time you set (`/job new <when> <what>`) | You or the agent put it on the board (`/goal new <what>`); the dispatcher starts it when a slot is free | The agent needs your answer before it acts | You send `/pause`, or a limit stops the work |
| **Lives in** | The job list (`/jobs`) | The goal board (`/goals`) | Your inbox (`/inbox`) | The pause record (`/status`) |
| **Ends when** | You stop it (`/job stop <id>`), or a one-time job has run | It is done, or you stop it (`/goal stop <id>`) | You answer it | You send `/resume` |
| **Best for** | The same work again and again at set times | One piece of work, done once, in the background | A spend, a post, a new rule, a grant | Stopping all work, or one job or goal, now — without losing it |

Jobs and goals survive a restart. **A fresh install starts with no jobs and an
empty board, and stays that way until you add work.** POLYROB ships the
mechanism; the work is yours.

---

## 1. Turn it on

```bash
polyrob autonomy on          # AUTONOMY_ENABLED — the master switch
polyrob autonomy status      # every axis, the loop state, any active pause
```

The goal board comes up with `AUTONOMY_ENABLED` on a local install. Jobs need
`CRON_ENABLED` too, which turns on by itself at `AUTONOMY_POSTURE=full`. The
autonomy axes are explained in
[configuration.md §5](configuration.md#6-the-autonomy-dial).

You can add a goal with autonomy off — the board is a database. The CLI warns
you when you do, because nothing will start it.

---

## 2. Goals — one piece of work, done once

```text
/goal new Draft the Q3 summary from reports/*.md and name the three biggest changes
/goals                    the board: NOW · STUCK · NEXT · OVER
/goal show <id>           result, cost, status and its reason, the decision it waits on
/goal retry <id>          clear the failures and put it back in the queue
/goal stop <id>           stop this run; add --forever to switch the title off
```

The same board from the terminal:

```bash
polyrob goals create "Draft the Q3 summary" \
  -b "Read reports/*.md, write one page, name the three biggest changes." \
  --acceptance "a file at reports/q3.md" \
  --priority 7

polyrob goals list --status ready     # ready|running|done|blocked|cancelled|triage
polyrob goals show <id>               # fields, tools, events, files
polyrob goals events <id>             # what happened, timestamped
polyrob goals edit <id> --priority 9 --acceptance "…"
polyrob goals retry <id>              # clears the failures; also resumes a paused goal
polyrob goals pause <id>              # paused by you, until retry
polyrob goals stop <id> [--forever]   # once; --forever switches the title off
```

A goal carries a title, a body (the instructions the run reads), a priority
`1`–`10` (default `5`; the **higher** number is served first), an optional
`--acceptance` line that states what *done* must prove, and an optional
`--tools` list. `--triage` parks it for your review instead of queueing it. Add
`--json` to any read command for a script.

**Write acceptance criteria.** A completion judge reads the agent's claim
against the evidence the run left — files, ids, the action record — and a goal
that cannot be verified completes as *done (unverified)* and stays out of the
learning loops. Naming the file turns a claim into a check.

**`/goal stop <id>` stops this run once.** Add `--forever` to switch the title
OFF: no job adds it again, and the agent cannot create it again. `/goal allow
<id|title>` turns it back on; `/goals` lists what is off in an `OFF` section.
Each board line also says who asked for the goal — `you launched it`, `you
asked for it`, or `I made this one up`.

### Pace

Three live settings set the rhythm without a restart:

```bash
polyrob config set goals.daily_quota 6       # goal runs started per rolling 24h
polyrob config set goals.max_concurrent 2    # runs in flight at once
polyrob config set goals.notify_on_done true # tell me when one finishes
```

A setting can only tighten what the environment allows: `GOAL_DAILY_QUOTA` and
`GOAL_MAX_CONCURRENT` (default 2) are the ceiling, and they are an owner env
edit. See `polyrob doctor --flags --group autonomy`.

### Blocked is not dead

A failing goal stops after `GOAL_MAX_RETRIES` failures and lands in `blocked`,
with the reason on its line ("failed twice: x_post 401"). A block that a
provider outage caused goes back to the queue within
`GOAL_BLOCKED_PROVIDER_RETRY_MIN` minutes. Any other block waits for you
(`/goal retry <id>`) or ages out to `cancelled` after
`GOAL_BLOCKED_MAX_AGE_DAYS`. A goal that needs something only you can supply
opens a decision in your inbox (§4) and names it.

### What the agent may give its own goals

A goal the agent writes for itself may ask only for a safe set of tools:
filesystem, task, browser, perplexity, MCP, AnySite, coding, `web_fetch`,
Twitter, email, `message`, `x402_invoice`, knowledge and the read-only
`defi_data`. Money spend, code execution, scheduling and the goal and skill
tools are not in it.

Under effective `AUTONOMY_MODE=autonomous` with the DeFi grant armed
(`DEFI_AGENT_AUTONOMY`) that set widens to the autonomous-mode toolset, which
can include `defi_trade`. That is deliberate. What bounds it is not the
toolset: it is the per-transaction limit, the rolling daily limit, the pause
and the owner approval above `DEFI_AUTONOMOUS_MAX_USD`. See
[payments.md](payments.md) and [security-model.md](security-model.md).

A goal's `payload.tools` is used **as written** when it runs. Anything an owner
seat writes there — `polyrob goals create --tools`, `/trade` in chat — is a
grant, with no filter on the way through. Only a group service run overrides
it, down to the group's read-only toolset.

---

## 3. Jobs — the same work at set times

```text
/jobs                                   every job: next run, last outcome, author
/job new "every day 09:00" Summarise the inbox and post the three items that need me
/job new every monday 09:00 | Check the site and report broken links
/job edit <id> schedule every 12h       change one field, keep the rest
/job edit <id> task <new instructions>
/job pause <id>                         the job's own switch; /job resume <id> turns it on
/job run <id>                           run it now, once
/job stop <id>                          remove it
/job stop <id> --forever                remove it and switch its lane off: the agent cannot
                                        create a job in that lane again until you allow it
/job allow <lane>                       switch a lane back on
```

`/job new` takes the schedule first, then the work: quote a schedule that has
spaces, or put `|` between the two. `tools=a,b` grants the job those tools;
`target=<token address> chain=<chain>` is the one token the job may buy. You
can also ask in plain words ("check the site every morning at nine") and the
agent writes the job for you.

A job's lane is its first pinned skill, or its rig when it pins no skill.
`/jobs` lists the lanes you stopped. A job created while `/pause cron`
is on waits paused and runs after `/resume cron`.

A schedule is a duration (`30m`), a weekday time (`every monday 09:00`), a
five-field cron expression, or an ISO timestamp for a one-time run. The
terminal has the same jobs:

```bash
polyrob job new "every day 09:00" Summarise the inbox and post the three items that need me
polyrob job list
polyrob job show <id>
polyrob job pause <id>      # or resume / run / stop
```

`--max-duration` sets the hard limit per run in seconds (default 180). Jobs
belong to one owner: you can only change your own.

Two behaviours are worth knowing:

- **Delivery.** A job can deliver its result to Telegram, email or Twitter
  instead of keeping it in a chat, behind `CRON_DELIVERY_ENABLED`.
- **Change-gated wakes.** At `AUTONOMY_POSTURE=full`, `WAKE_CHANGE_GATE` lets a
  review-shaped job skip its paid model call when nothing it watches changed
  since the last run — a $0 run instead of a model paid to find that out. A
  delivery job is never gated.

A group chat can run a catch-up pass over its own messages at set times instead
of waiting for a mention. That is a job bound to the group, created with
`/groups service here every 30m`. See [groups.md](groups.md#the-service-job).

---

## 4. Decisions — what waits on you

`/inbox` lists every open decision, blocking ones first: approvals, the
agent's questions (asks), quotes and choices on action cards, signer approvals,
and the jobs and goals the agent wrote that you can make yours (§6).
`/pending`, `/asks` and `/cards` show parts of the same list. Answer one with
its own tap, or with `/approve <id>`, `/reject <id>` or `/fulfill <id>` (an
ask). Answering an ask puts the goals behind it back in the queue.

The terminal reads the same list: `polyrob owner inbox`, `polyrob owner asks`
and `polyrob owner fulfill <id>`. The full list of approval kinds and the
`/gates` that decide which actions need you first are in
[owner-controls.md](owner-controls.md).

---

## 5. Pause — stop now, keep everything

```text
/pause                    stop all autonomous work now
/pause <scope>            one scope only (for example trading)
/pause <job id|goal id>   one job or one goal
/resume                   lift the pause
```

The terminal has `polyrob autonomy pause` and `polyrob autonomy resume`. A
pause needs no restart and loses nothing: jobs keep their schedules and goals
keep their place on the board. A plain "stop" in chat is a message to the
agent, not a switch. Only you lift a pause: the agent can pause its own work,
but it cannot resume it. See [owner-controls.md](owner-controls.md).

---

## 6. What standing work may do

Every job and goal carries the same standing authority. When it runs, its tools and
`tools=` are used as written; it may post on X, and a job may run its write verb, use
its buy target and moderate the group its text names, without asking you each time.

When work is created, your own turn may ask for any rig. A job or goal the agent
writes from an autonomous run may ask only for the safe set in §2, and only your turn
may put a write verb on a job. A buy target is trusted only when you wrote it from a
seat or typed the address in your message. Under the `supervised` money regime no
goal or cron run moves funds unattended.

---

## 7. Where to look when nothing runs

| Symptom | Check |
|---|---|
| A goal sits in `ready` and never starts | `/status` (or `polyrob autonomy status`) — master switch, posture, and whether a pause is active. |
| "the goal board is durable, but no dispatcher will pick goals up" | `AUTONOMY_ENABLED`/`GOALS_ENABLED` is off. `polyrob autonomy on`. |
| A job never runs | `/jobs` — is it paused, and when is its next run? `CRON_ENABLED` must be on. |
| Goals run but you never hear about them | `goals.notify_on_done`, and `AUTONOMY_POSTURE` — `silent` means autonomous work is not reported. |
| The agent keeps asking for the same thing | `/inbox`, then answer the decision (`/fulfill <id>` for an ask). |
| Everything stopped at once | A pause is in force: `/status`, then `/resume`. |

Flag names and defaults: [`docs/CONFIGURATION.md`](../CONFIGURATION.md), or
`polyrob doctor --flags --group autonomy`. Commands: [cli.md](cli.md).
