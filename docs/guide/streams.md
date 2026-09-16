# Goals & standing work

POLYROB gives you three durable ways to make the agent act when you are not in
the room:

| You want | Use | Survives a restart |
|---|---|---|
| One job, done once, in the background | a **goal** on the board | yes |
| A prompt on a clock | a **cron job** | yes |
| A standing mission the agent keeps serving | an **objective** the planner decomposes into goals | yes |

**A fresh install starts with an empty board and stays empty until you put work
on it.** POLYROB ships no missions, no seeded objectives and no timers of its
own: a framework ships mechanism, and the work is yours.

---

## 1. Turn it on

```bash
polyrob autonomy on          # AUTONOMY_ENABLED — the master switch
polyrob autonomy status      # every axis, the loop state, any active pause
```

The goal board and its planner come up with `AUTONOMY_ENABLED` on a local
install. Cron is a separate switch, `CRON_ENABLED`, which turns on by itself at
`AUTONOMY_POSTURE=full`. The autonomy axes are explained in
[configuration.md §5](configuration.md#5-the-autonomy-dial).

You can write a goal with autonomy off — the board is just a database. The CLI
warns you when you do, because nothing will pick it up.

Stopping is one command and needs no restart:

```bash
polyrob autonomy pause            # everything
polyrob autonomy resume
```

In chat, "pause", "stop" and "halt" do the same thing. See
[owner-controls.md](owner-controls.md).

---

## 2. The goal board in one screen

```bash
polyrob goals create "Draft the Q3 summary" \
  -b "Read reports/*.md, write one page, name the three biggest changes." \
  --acceptance "a file at reports/q3.md" \
  --priority 7

polyrob goals list --status ready     # ready|running|done|blocked|cancelled|triage
polyrob goals show <id>               # fields, tools, events, artifacts
polyrob goals tree                    # objectives with their goals, orphans last
polyrob goals events <id>             # what actually happened, timestamped
polyrob goals edit <id> --priority 9 --acceptance "…"
polyrob goals ready <id>              # triage/blocked -> ready
polyrob goals pause <id>              # -> blocked
polyrob goals resume <id>
polyrob goals retry <id>              # clears the failure count
polyrob goals cancel <id>
```

A goal carries a title, a body (the instructions the run actually reads), a
priority `1`–`10` (default `5`; the **higher** number is served first), an optional
`--acceptance` line stating what *done* must prove, an optional `--tools` list,
and an optional `--objective` or `--parent` link. `--triage` parks it for your
review instead of queueing it. Add `--json` to any read command for a script.

**Write acceptance criteria.** A completion judge reads the agent's claim
against the evidence the run left — files, ids, action ledger — and a goal that
cannot be verified completes as *done (unverified)* and is excluded from the
learning loops. Naming the artifact is what turns a claim into a check.

### From chat

`/task <goal>` starts a job now. `/goals` prints the board summary. `/goal show|ready|pause|resume|retry|cancel <id>` steers one row. `/asks` lists what the
agent needs from you; `/fulfill <id>` clears it and unblocks the goals behind
it. The same two asks commands are `polyrob owner asks` and `polyrob owner
fulfill <id>`, and everything waiting on you is in `polyrob owner inbox`.

### Pace

Three live preferences set the rhythm without a restart:

```bash
polyrob config set goals.daily_quota 6       # goal runs started per rolling 24h
polyrob config set goals.max_concurrent 2    # runs in flight at once
polyrob config set goals.notify_on_done true # tell me when one finishes
```

A preference can only tighten what the environment allows; the environment
values (`GOAL_DAILY_QUOTA`, `GOAL_MAX_CONCURRENT`) are the ceiling and are an
owner env edit. See `polyrob doctor --flags --group autonomy`.

### Blocked is not dead

A failing goal trips a circuit breaker after `GOAL_MAX_RETRIES` failures and
lands in `blocked`. A block caused by a provider outage requeues itself within
`GOAL_BLOCKED_PROVIDER_RETRY_MIN` minutes. Any other block waits for you
(`polyrob goals retry <id>`) or ages out to `cancelled` after
`GOAL_BLOCKED_MAX_AGE_DAYS`. A goal blocked on something only you can supply
files an **ask**, which is where `polyrob owner asks` gets its rows.

---

## 3. Objectives — the durable "why"

An objective is a standing mission. The planner reads the active ones and
writes goals to serve them when the ready queue runs thin.

```bash
polyrob goals objective add "Grow the newsletter" \
  -b "Publish one issue a week; grow list size and open rate." \
  --priority 5 \
  --success-criteria "list size and open rate trending up, judged monthly" \
  --goal-budget 20

polyrob goals objective list --status active
polyrob goals objective show <id>        # criteria, budget use, live children
polyrob goals objective pause <id>       # planner stops adding; running goals continue
polyrob goals objective activate <id>
polyrob goals objective drop <id>
```

`--success-criteria` goes verbatim into the planner's prompt: it is what the
model is told to measure against. From chat, `/goal objective
list|pause|activate|drop [id]` does the same steering from a phone.

### The goal budget is a lifetime tally

`--goal-budget` (default `OBJECTIVE_GOAL_BUDGET`, 25) caps how many child goals
an objective may accumulate that were not cancelled — **`done` children count
forever**. That is the right rail for a bounded project: it is what stops the
planner opening "Round 9" on something that never finishes. Pass `0` for no
cap. An objective that carries a stream id (§5) is exempt, because a standing
rail is bounded by cadence instead.

### What the planner may grant itself

A goal the agent writes for itself may request only from a safe allowlist:
filesystem, task, browser, perplexity, MCP, AnySite, coding, `web_fetch`,
Twitter, email, `message`, `x402_invoice`, knowledge and the read-only
`defi_data`. Money-spend, code execution, cron and the meta goal/skill tools
are not in it.

Under effective `AUTONOMY_MODE=autonomous` with the DeFi grant armed
(`DEFI_AGENT_AUTONOMY`) that set widens to the autonomous-mode toolset, which
can include `defi_trade`. That is deliberate, and what bounds it is not the
toolset: it is the per-transaction ceiling, the rolling daily cap, the pause
record and the owner approval queue above `DEFI_AUTONOMOUS_MAX_USD`. See
[payments.md](payments.md) and [security-model.md](security-model.md).

A goal's `payload.tools` is used **verbatim** when it is dispatched. Anything an
owner seat writes there — `polyrob goals create --tools`, `/trade` in chat, a
manifest entry — is a grant, with no filtering on the way through. Only a room
service run overrides it, down to the room's read-only toolset.

---

## 4. Fair dispatch across objectives

The dispatcher claims `ready` goals each tick, up to `GOAL_MAX_CONCURRENT`
(default 2).

`GOAL_FAIR_DISPATCH` (default **on**) round-robins the ready queue by
objective: it walks the same priority-ordered window but takes at most one goal
per objective per pass, until the tick's slots are full. Priority still decides
who is served *first* within a pass; it no longer decides who is served *at
all*, so one objective's backlog cannot starve its neighbours. Throughput is
unchanged when only one objective has ready work, and any error in the fair
path falls back to the global order rather than stalling the tick.

`GOAL_PER_OBJECTIVE_CAP` (default `0`, off) is an optional hard ceiling on
concurrent runs belonging to one objective. Leave it off: the round-robin
already delivers fairness, while a cap of `1` idles a slot whenever only one
objective happens to have work.

On the planner side, code picks *which* objective is served next — fewest
in-flight children first, then oldest activity — and the model still writes the
goals. Its three numeric limits are derived from the number of active
objectives rather than hardcoded, so a sixteen-objective board is allowed more
ready goals than a two-objective one. `GOAL_PLANNER_SCALING=false` is the single
revert for that whole half, the way `GOAL_FAIR_DISPATCH=false` is for dispatch.

### Knobs as the objective count grows

Three things widen together, or one becomes the new bottleneck:

- **`GOAL_MAX_CONCURRENT`** (default `2`) — global runs in flight. Fair dispatch
  spreads the slots that exist; it cannot create slots. This is an owner env
  edit and a restart; a preference can only lower it.
- **`GOAL_PLANNER_READY_CEILING`** (default `0` = derive `max(5, active
  objectives)`) — how stocked the planner keeps the ready queue. Pin a number
  only if you want a fixed ceiling regardless of objective count.
- **`GOAL_PLANNER_MAX_SOCIAL`** (default `1`) — how many goals in one planner
  run may carry the `twitter` tool. It deliberately does not scale: raising it
  raises posting volume, not coverage.

`GOAL_PLANNER_GOALS_PER_RUN` (default `3`) bounds spend per planner run, not
coverage, so it is not a "turn this up as you grow" knob.

### When the board goes quiet

If the planner leaves the ready queue empty for
`GOAL_EMPTY_PIPELINE_ESCALATE_AFTER` consecutive runs (default 2), the stall is
escalated to you once, as an ask. It is not treated as a stall while work is in
flight, while a stream is waiting out its cadence, or when every objective is
at budget with its ask already open — and the streak is durable, so a restart
does not re-arm the push. The planner also backs off after consecutive empty
runs instead of paying for the same verdict every tick.

---

## 5. Standing work

### On a clock — cron

```bash
polyrob cron schedule "Summarise the inbox and post the three items that need me" "every day 09:00"
polyrob cron list
polyrob cron show <id>
polyrob cron cancel <id>
```

A schedule is a duration (`30m`), a weekday time (`every monday 09:00`), a
five-field cron expression, or an ISO timestamp for a one-shot. `--max-duration`
sets the per-run hard cap in seconds (default 180). Jobs are tenant-scoped: you
can only cancel your own.

From Telegram or the console: `/cron add <schedule> | <task>`, `/cron list`,
`/cron cancel <id>`, or just ask in plain words ("check the site every morning at
nine"). The REPL's `/cron` is read-only — schedule and cancel there with
`polyrob cron`.

Cron needs `CRON_ENABLED`. Two behaviours are worth knowing:

- **Out-of-band delivery.** A job can deliver its result to Telegram, email or
  Twitter instead of leaving it in a session, behind `CRON_DELIVERY_ENABLED`.
- **Change-gated wakes.** At `AUTONOMY_POSTURE=full`, `WAKE_CHANGE_GATE` lets a
  review-shaped job skip its paid model call when nothing observable changed
  since the last tick — a $0 tick rather than a model paid to rediscover that.
  A delivery job is never gated.

### Your own stream manifest (optional)

A **stream** is one objective plus a recurring cycle of goals that serves it,
declared in a YAML file **you** write. Nothing ships one, and most people do not
need one: an objective plus the planner covers standing work well. Reach for a
manifest when you want the exact goal bodies, the exact toolset and the exact
cadence pinned in a reviewed file rather than written by a model.

The mechanism is `agents/task/goals/streams.py`. It resolves
`POLYROB_STREAMS_MANIFEST` first, then `<data home>/streams/streams.yaml`. It
refuses any path under a session workspace, and the file is hard-denied to every
agent-writable surface, so the agent can never aim it at something it wrote
itself.

Each entry names an objective and a non-empty `goals:` list; every goal needs a
title, a body and a `tools:` list, which is written to the board **verbatim**.
Two throttles gate a re-seed and both must pass: `max_live_goals` (anything not
finished, `blocked` included) and `cadence_hours` since the stream's newest goal.
Seeding itself is yours to drive — call `load_manifest`, `stream_is_due` and
`seed_stream` from a scheduled job you own, at whatever interval your shortest
cadence needs. The goal dispatcher reads the manifest too, but only to know when
a stream's next seed is due, so an idle stream is not mistaken for a stalled
board.

⚠️ A manifest is an operator grant, not a safety boundary. It is not the only
place a goal can receive a money verb — an owner seat writing `--tools` does the
same thing, and an armed autonomous instance widens the planner's own allowlist
(§3). What bounds spending is the money gates in
[payments.md](payments.md), not where the tool list was typed.

### A room that answers on its own

A group chat can run a recurring catch-up pass over its own ledger instead of
waiting for a mention. That is a cron job bound to the room, created with
`/groups service here every 30m`. See [groups.md](groups.md#the-service-job).

---

## 6. Where to look when nothing runs

| Symptom | Check |
|---|---|
| A goal sits in `ready` forever | `polyrob autonomy status` — master switch, posture, and whether a pause is active. |
| "the goal board is durable, but no dispatcher will pick goals up" | `AUTONOMY_ENABLED`/`GOALS_ENABLED` is off. `polyrob autonomy on`. |
| Goals run but you never hear about them | `goals.notify_on_done`, and `AUTONOMY_POSTURE` — `silent` means autonomous work is not reported. |
| One objective eats every slot | `polyrob doctor --flags --search GOAL_FAIR_DISPATCH`; raise `GOAL_MAX_CONCURRENT` if the slots are genuinely too few. |
| The agent keeps asking for the same thing | `polyrob owner asks`, then `polyrob owner fulfill <id>`. |
| Everything stopped at once | a pause is in force: `polyrob autonomy status`, then `polyrob autonomy resume`. |

Flag names and defaults: [`docs/CONFIGURATION.md`](../CONFIGURATION.md), or
`polyrob doctor --flags --group autonomy`. Commands: [cli.md](cli.md).
