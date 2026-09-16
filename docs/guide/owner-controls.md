# Owner controls: stop, pause, resume

POLYROB runs autonomous work on its own — goal dispatch, the planner, stream
seeding, cron ticks, self-wake re-entries, social posting. This page is how you
stop it, and how you know it stopped.

## Stop everything

Say it. In Telegram — typed or as a voice note — any of these pauses everything,
with no model call in the way:

```
stop
stop everything
Stop all ghosts today, please
autonomy off
halt
pause
```

The five stop words are `stop`, `halt`, `pause`, `freeze` and `standby`; a few
phrases (`stop everything`, `shut it down`, `stand down`, `autonomy off`, `kill
switch`) mean a full stop on their own. A negation or a question never counts
("why did you stop?", "don't stop the exit monitor").

You get the verified state back within a second or two:

```
⏸ Paused everything (since 14:12 UTC, by owner via telegram:voice).
Still on: this chat, crash/security/credit alerts.
Resume with /resume. /status shows this first.
```

The same words work in the `polyrob` REPL. The slash verb `/pause` (and its
alias `/halt`) does the same on Telegram, in the REPL and in the console; on the
box:

```bash
polyrob autonomy pause
```

What stops within seconds: goal dispatch and the planner, stream seeding, cron
ticks (the daily digest too), self-wake re-entries, correspondent replies
re-running a session, the curator and background review, the settlement watcher,
lifecycle pings and escalations. In-flight work is cancelled: a running goal
goes back to `ready` with no failure counted, a running cron job back to
`scheduled`, and the background delegations of autonomous sessions are stopped.

What keeps running: your chat with the agent, and crash, security and credit
alerts.

## Scoped and timed pauses

```
/pause trading                # no NEW positions; exits still run
/pause background             # no stream reseeds, no planner, no cron ticks
/pause deploying for 6h       # auto-resumes after six hours
/pause for 2d                 # everything, for two days
```

Five plain words cover most of it: `everything` (= `all`), `trading`,
`background` (streams + planner + cron), `messages` (lifecycle pings and
escalations) and `deploying` (the durable app service — no new deploys, and live
app containers are stopped until resume). A raw scope still works for anything a
plain word does not cover: `all`, `trading`, `streams`, `planner`, `cron`,
`social`, `oversight`, `pings`, `apps`. An unrecognized word refuses — it never
silently widens to "everything". Durations are `90m`, `6h`, `2d`. The CLI form
is `polyrob autonomy pause trading --for 6h`.

In prose, a scoped ask ("stop trading for 6 hours", "stop rendering videos")
goes to the agent, which narrows it through its owner-only `autonomy_control`
action and quotes the result. If the model is unavailable, or the session is too
busy to take your message, everything is paused instead — the safe default — and
the reply says so.

The preference `pause.phrases` adds your own object-free words: with
`['ghosts', 'bots']`, "stop the ghosts" still means stop everything.

### Narrow pauses with their own verbs

```bash
polyrob owner pause-entries    # refuse NEW treasury positions; exits still run
polyrob owner resume-entries
polyrob owner pause-streams    # refuse NEW stream-manifest reseeds
polyrob owner resume-streams
polyrob owner halt             # alias of `polyrob autonomy pause`
polyrob owner resume           # alias of `polyrob autonomy resume`
```

## Resume

```
/resume            # everything
/resume trading    # only that scope; the others stay paused
polyrob autonomy resume
```

The resume words are `resume`, `unpause` and `unfreeze`. ("continue" is the
everyday "keep going" word and lifts a pause only next to an autonomy word, as
in "continue autonomy".) Those words only act when something is paused;
otherwise they are just chat. The reply is the verified state:
`▶ Autonomy RESUMED.` or `▶ Resumed; still paused: streams.`

## Pause versus off

A pause is live and needs no restart. Turning autonomy **off** is a durable
configuration change that applies to the next process:

```bash
polyrob autonomy off
polyrob autonomy on [--mode supervised|autonomous] [--global]
polyrob autonomy status
```

Use `pause` to stop what is happening now; use `off` when you do not want the
loops to start at all. What each mode moves:
[configuration.md §5](configuration.md#5-the-autonomy-dial).

## Where the state lives

One file: `<data>/AUTONOMY_PAUSE.json`, written atomically to every data base
the runtime probes. Every process — the Telegram daemon, the email daemon, the
console, the timers, the on-box scripts — reads it. A restart starts the loops
armed and idle, and a resume needs no restart. An unreadable record is treated
as a pause of everything (fail-closed). The older `touch <data>/AUTONOMY_HALT`,
`TREASURY_ENTRY_PAUSE` and `STREAM_SEEDING_PAUSE` files still work as facets of
it, and `/resume` clears them. A pause set in the environment
(`AUTONOMY_HALT=true`) needs an env edit and a restart, and the reply tells you
so.

## How to verify

- `/status` (Telegram), `polyrob autonomy status`, `polyrob doctor` and the
  console all lead with the pause line: `⏸ PAUSED (everything) since 14:12 UTC
  by owner via telegram` or `▶ RUNNING — 0 goal run(s), 0 cron run(s), loops
  alive 3/3`.
- A `pause_violation` CRITICAL health item names any autonomous activity
  recorded after the pause (goal runs, cron runs, self-wakes, social writes,
  wallet spend). Treat it as an incident.
- `polyrob autonomy status --json` exposes the record under `pause`.

Any maintenance or alerting loop you run on the box reads the same record: while
`all` or `oversight` is paused those loops tick read-only — health checks and
their own log, no seeding, no dial changes, no restarts, no deploys — and only
critical alerts are delivered.

## What did I miss?

Every message the agent sends you rides one delivery rail, and that rail is
bounded on purpose: a runaway loop must not be able to fill your chat. The
bounds are a daily cap (30), an hourly rate limit (10), a 24-hour content
dedup, and a smaller separate ceiling for framework status pings.

**A bounded message is never a lost message.** Anything the rail could not
deliver live is recorded durably and read back with:

```bash
polyrob owner missed          # or /missed in Telegram, the REPL and the console
```

Each entry says which bound held it:

| Kind | What happened |
|---|---|
| `capped` | the daily cap was already spent |
| `rate-limited` | the hourly limit was already reached |
| `paused` | you had paused the scope this message belongs to |
| `undelivered` | no live chat was reachable, or the send failed |
| `cooldown` | the same text had already been sent to you within `OWNER_MESSAGE_COOLDOWN_SEC` |

Two things deliberately do NOT appear there. A `deduped` message is text you
already received, so there is nothing to recover. And framework run pings
(`▶ goal started`) are ephemeral status, not content — they would otherwise be
most of what this list shows. Both are still in `polyrob telemetry`.

### If you are missing too much

Raise your own budget — the preference wins unless the deployment set an
explicit ceiling, in which case it may only tighten it:

```bash
polyrob config set delivery.daily_cap 60
polyrob config set delivery.rate_per_hour 20
```

Safety-bearing messages never queue behind ordinary traffic and never spend
your budget: an approval you are blocked on, a blocked goal, a transaction that
has already moved money, a credit-death or security notice. Quiet hours
(`digest.quiet_hours`) defer rather than drop — held text is delivered when the
window ends.

The **daily digest** is the roll-up for everything above. Turning it on
(`OWNER_DIGEST_ENABLED=true`, `CRON_DELIVERY_ENABLED=true`) is not enough — a
job has to run it:

```bash
polyrob cron digest "every day 08:00"    # schedule it (again = move it)
polyrob cron digest                      # show what is scheduled
polyrob cron digest --off                # cancel it
```

It costs nothing to run: the message is composed from the ledger, the event log
and your open asks, with no model call. If `polyrob doctor` reports **"owner
digest is enabled but no digest job is scheduled"**, that is this gap.

## MCP servers (giving the agent new tools)

An MCP server is how a protocol hands an agent its own tools — Aave, GitHub, a
docs index. You add one from wherever you are; it is saved against your tenant
and loads at the start of every new session.

```
/mcp                                 # the MCP servers I have, and their state
/mcp add aave https://mcp.aave.com   # add one (an api key may follow the URL)
/mcp remove <id>                     # drop it; running sessions keep it until they end
/mcp test <id>                       # connect now and count the tools
```

Same verbs in the REPL. **HTTPS URLs only.** A local command (`npx …`) is
refused: saving one would save arbitrary code execution on this box, which is
the whole threat model for installing an MCP server. If you genuinely need a
local server, put a reviewed entry in `MCP_INSTALL_CATALOG_FILE` on the box.

What an added server can and cannot do: its tools become callable, and
everything they return is framed as untrusted data rather than instructions. It
gets **no** access to the wallet — no MCP tool can reach the signer, and no money
verb accepts calldata from outside the process. Aave's server, for example,
prepares *unsigned* transactions, so installing it gives the agent Aave's
markets, positions and health factors to read, and no ability to spend.

## Approving what the agent proposes

The agent quarantines what it learns. A new skill, an identity note, an owner
rule, a queued spend approval, a new contact — none of it takes effect until you
decide. One queue holds all of them.

```
/pending                   # everything waiting, each item with its own tap
/approve                   # one item waiting? decide it. several? pick from the list
/approve_all               # the whole queue at once
/reject_all
```

Each item in `/pending` prints its own pair of one-tap tokens, like
`/approve_p_1245c6`. Tap one and you are done — there is no id to copy. The
token is an address, not a secret: it is resolved against the live queue every
time, so a stale one decides nothing.

You can also just reply **approve** or **reject** in words. That only counts
when something is actually waiting, and only when the whole message is the
decision — "I approve of that plan" is a sentence for the agent, not a vote.

On the command line the same queue is `polyrob owner pending`, and
`polyrob owner promote|reject <kind> <id>` (or `… promote all`) decides it.
In the REPL it is `/pending` and `/pending approve <kind> <id>`.

## Apps

A new app address is an owner decision; the agent's deploy records it as
**pending** and waits. Every seat renders the same text:

```
/apps                      # list: slug [status] URL (health)
/apps show <slug>          # the row in full
/apps approve <slug>       # the owner decision; the supervisor deploys within one tick
/apps reject <slug>        # a pending app never runs
/apps kill <slug>          # stop a running app (the address stays approved)
/apps logs <slug> [n]      # recent container log lines
```

CLI: `polyrob apps list|show|approve|reject|kill|logs`; the console offers the
same decisions ([console.md](console.md)).
A pending app leads the health block on every status seat until you decide.
`/pause deploying` stops deploys and live containers; `/resume deploying` brings
them back without a redeploy.

The supervisor process that actually runs the containers is owner-owned
(`polyrob apps supervise`, or the unit that calls it) and holds the Docker,
nginx and firewall privilege the agent never has. Standing it up, including the
vhost and certificate: [deployment-postures.md](deployment-postures.md).

## See also

- [configuration.md §5](configuration.md#5-the-autonomy-dial) — the four autonomy axes
- [cli.md](cli.md#polyrob-owner) — every `polyrob owner` verb
- [security-model.md](security-model.md) — what stops the agent when you are not watching
- [payments.md](payments.md) — money caps, approval lanes and the spend ledger
- [configuration.md §7](configuration.md#7-surfaces-and-who-may-talk-to-it) — the delivery-rail bounds and their flags
