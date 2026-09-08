# Owner controls: stop, pause, resume

POLYROB runs autonomous work on its own (goal dispatch, the planner, stream seeding,
cron ticks, self-wake re-entries, social posting, the on-box dev/ops loops). This page
is how you stop it — and how you know it stopped.

## Stop everything

Say it. In Telegram — typed or as a voice note — any of these pauses everything, with no
model call in the way:

```
stop
stop everything
Stop all ghosts today, please
autonomy off
halt
pause
```

You get the verified state back within a second or two:

```
⏸ Paused everything (since 14:12 UTC, by owner via telegram:voice).
Still on: this chat, crash/security/credit alerts.
Resume with /resume. /status shows this first.
```

The same words work in the `rob` REPL. The slash verb `/pause` (and its alias `/halt`)
does the same on Telegram, in the REPL and on the web console; on the box:

```
polyrob autonomy pause
```

What stops within seconds: goal dispatch and the planner, stream seeding, cron ticks
(the daily digest too), self-wake re-entries, correspondent replies re-running a session,
the curator and background review, the settlement watcher, lifecycle pings and
escalations, and — on the box — the maint/intel loops (their watchdog neither nudges nor
relaunches, and `ops_alert.py` sends only `--critical` alerts). In-flight work is
cancelled: a running goal goes back to `ready` (no failure counted), a running cron job
back to `scheduled`, background delegations of autonomous sessions are cancelled.

What keeps running: your chat with the agent, and crash / security / credit alerts.

## Scoped and timed pauses

```
/pause trading                # no NEW positions; exits still run
/pause streams cron           # no stream reseeds, no cron ticks; goals still dispatch
/pause social for 6h          # auto-resumes after six hours
/pause for 2d                 # everything, for two days
```

Scopes: `all` (default), `trading`, `streams`, `planner`, `cron`, `social`, `oversight`
(the dev/ops loops and their alerts), `pings` (lifecycle pings + escalations), `apps` (the
durable app service: no new deploys, and live app containers are stopped until resume). Durations:
`90m`, `6h`, `2d`. The CLI form is `polyrob autonomy pause trading --for 6h`.

In prose, a scoped ask ("stop trading for 6 hours", "stop rendering videos") goes to the
agent, which narrows it through its owner-only `autonomy_control` action and quotes the
result. If the model is unavailable, or the session is too busy to take your message,
everything is paused instead — the safe default — and the reply says so.

The pref `pause.phrases` adds your own object-free words: with `['ghosts', 'bots']`,
"stop the ghosts" still means stop everything.

## Resume

```
/resume            # everything
/resume trading    # only that scope; the others stay paused
polyrob autonomy resume
```

"resume", "unpause" or "continue" in chat lifts a pause when one is on (when nothing is
paused those words are just chat). The reply is the verified state:
`▶ Autonomy RESUMED.` or `▶ Resumed; still paused: streams.`

## Where the state lives

One file: `<data>/AUTONOMY_PAUSE.json` (written atomically to every data base the
runtime probes). Every process — the Telegram daemon, the email daemon, the console, the
stream-seeder timer, the on-box scripts — reads it; a restart starts the loops armed and
idle, and a resume needs no restart. An unreadable record is treated as a pause of
everything (fail-closed). The older `touch <data>/AUTONOMY_HALT`,
`TREASURY_ENTRY_PAUSE` and `STREAM_SEEDING_PAUSE` files still work as facets of it, and
`/resume` clears them; a pause set in the environment (`AUTONOMY_HALT=true`) needs an
env edit + restart, and the reply tells you so.

## How to verify

- `/status` (Telegram), `polyrob autonomy status`, `polyrob doctor` and the console all
  lead with the pause line: `⏸ PAUSED (everything) since 14:12 UTC by owner via telegram`
  or `▶ RUNNING — 0 goal run(s), 0 cron run(s), loops alive 3/3`.
- A `pause_violation` CRITICAL health item names any autonomous activity recorded after
  the pause (goal runs, cron runs, self-wakes, social writes, wallet spend) — file it as
  an incident.
- `polyrob autonomy status --json` exposes the record under `pause`.

## The ops loops during a pause

While `all` or `oversight` is paused the maintenance and intel loops run read-only ticks
(health checks and the tick log only): no seeding, no dial changes, no restarts or
deploys, no re-alerts. Their watchdog does not nudge or relaunch them, `/dev` messages
are appended to the inbox without a commit, and `ops_alert.py` delivers only
`--critical` alerts (everything else is logged to `<data>/ops_alert_suppressed.log`).

## Apps (the durable app service, proposal 032)

A NEW app address is an owner decision; the agent's `app_deploy` records it as
**pending** and waits. Every seat renders the same text:

```
/apps                      # list: slug [status] URL (health)
/apps show <slug>          # the row in full
/apps approve <slug>       # the owner decision — the supervisor deploys within one tick
/apps reject <slug>        # a pending app never runs
/apps kill <slug>          # stop a running app (the address stays approved)
/apps logs <slug> [n]      # recent container log lines
```

CLI: `polyrob apps list|show|approve|reject|kill|logs`; console: the Apps page. A pending
app leads the health block on every status seat until you decide. `/pause apps` stops
deploys and live containers; `/resume apps` brings them back without a redeploy.
