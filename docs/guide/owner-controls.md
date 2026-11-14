# Owner controls: stop, pause, resume

POLYROB runs autonomous work on its own — goal dispatch, cron ticks, self-wake
re-entries, social posting. This page is how you
stop it, and how you know it stopped.

## Stop everything

Send `/pause` (or its alias `/halt`). It is a slash command on purpose: it runs
with no model call, no queue and no tool in the way, and it works the same on
Telegram, in the `polyrob` REPL and in the console. You get the verified state
back within a second or two:

```
⏸ Paused everything (since 14:12 UTC, by owner via telegram).
Still on: this chat, crash/security/credit alerts.
Resume with /resume. /status shows this first.
```

Plain words are never commands. "stop", "pause the swaps" or "resume where you
left off" — typed or spoken — is a message to the agent like any other. The agent
can pause and resume through its owner-only `autonomy_control` action, but that
path needs a working model; `/pause` does not. On the box:

```bash
polyrob autonomy pause
```

What stops within seconds: goal dispatch, cron
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
/pause background             # no cron ticks
/pause deploying for 6h       # auto-resumes after six hours
/pause for 2d                 # everything, for two days
```

Five plain words cover most of it: `everything` (= `all`), `trading`,
`background` (cron), `messages` (lifecycle pings and
escalations) and `deploying` (the durable app service — no new deploys, and live
app containers are stopped until resume). A raw scope still works for anything a
plain word does not cover: `all`, `trading`, `cron`,
`social`, `oversight`, `pings`, `apps`. An unrecognized word refuses — it never
silently widens to "everything". Durations are `90m`, `6h`, `2d`. The CLI form
is `polyrob autonomy pause trading --for 6h`.

A one-time job (a reminder, for example) that comes due while jobs are paused is not
lost: it keeps its slot and runs at the first tick after `/resume`. A job's own
pause window holds it the same way.

In prose, a scoped ask ("stop trading for 6 hours") goes to the agent, which
narrows it through its owner-only `autonomy_control` action and quotes the
result. Nothing acts on the sentence before the agent reads it.

### One run, this chat, or the trading mandate

```
/run                  # what runs in the background now, numbered
/run pause 2          # pause run 2 at its next step; /run resume 2 continues it
/run stop 2           # stop that run only
/cancel               # stop the task I am running for you in this chat
/trade pause          # no new trading entries; every exit job keeps running
/trade stop           # end the trading mandate (it asks whether to close every open position)
```

`/run` acts on one live run and leaves the job or goal behind it alone: a
stopped goal run is retried or not by the board's own rules. A paused goal or
job still runs inside its time limit, so a long pause can end the run. To pause
or stop the job or goal itself, use `/job pause|stop <id>` or `/goal pause|stop
<id>` ([streams.md](streams.md)).

The trading mandate is how you let the agent trade on its own.
`/trade start <budget> <chain,chain> [preset]` (for example
`/trade start 150 base majors`) shows one decision with every number;
Confirm creates the mandate and its jobs: a hunt, an exit job for each open
trade, and a daily review. `/trade status` shows it, `/trade resume` lifts its
pause, and `/trade release` clears a loss stop that a breaker latched. A pause
or a stop leaves every exit job running until its trade is closed.

### Narrow pauses with their own verbs

```bash
polyrob owner pause-entries    # refuse NEW trading entries; exits still run
polyrob owner resume-entries
polyrob owner halt             # alias of `polyrob autonomy pause`
polyrob owner resume           # alias of `polyrob autonomy resume`
```

## Resume

```
/resume            # everything
/resume trading    # only that scope; the others stay paused
polyrob autonomy resume
```

The reply is the verified state: `▶ Autonomy RESUMED.` or
`▶ Resumed; still paused: cron.` A sentence such as "resume the swaps" is
chat for the agent, never a switch.

## Pause versus off

A pause is live and needs no restart. Turning autonomy **off** is a durable
configuration change that applies to the next process:

```bash
polyrob autonomy off
polyrob autonomy on [--mode supervised|autonomous]
polyrob autonomy status
```

Both write to `~/.polyrob/.env`, the only file the CLI loads. `--global` is
still accepted so that old scripts keep working; it changes nothing.

`/mode` states what the agent may do without you and what it does when you are away;
`/mode ask-first`, `/mode standard` and `/mode on-my-own` set one level
([configuration.md](configuration.md#what-the-agent-may-do-on-one-screen)).

Use `pause` to stop what is happening now; use `off` when you do not want the
loops to start at all. What each mode moves:
[configuration.md §6](configuration.md#6-the-autonomy-dial).

## Where the state lives

One file: `<data>/AUTONOMY_PAUSE.json`, written atomically to every data base
the runtime probes. Every process — the Telegram daemon, the email daemon, the
console, the timers, the on-box scripts — reads it. A restart starts the loops
armed and idle, and a resume needs no restart. An unreadable record is treated
as a pause of everything (fail-closed). The older `touch <data>/AUTONOMY_HALT` and
`TREASURY_ENTRY_PAUSE` files still work as facets of
it, and `/resume` clears them. A pause set in the environment
(`AUTONOMY_HALT=true`) needs an env edit and a restart, and the reply tells you
so.

## Restart and update from chat

`/restart` restarts the background service once its current work is done
(`/restart now` cuts it); `/update` updates it to the latest release, rolls back
on any failure, and restarts. Both show a quote with a Confirm button, both are
owner-only and refused in a group room, and the service answers in the chat you
asked from when it is back: `✅ Back online on 1.7.0 (abc1234) — restart took 14 s.` A setting that
waits for a restart is listed in `/status` with `/restart` as its remedy. The
whole story, and the shell and root equivalents:
[operate-the-service.md](operate-the-service.md).

## How to verify

- `/status` (Telegram), `polyrob autonomy status`, `polyrob doctor` and the
  console all lead with the pause line: `⏸ PAUSED (everything) since 14:12 UTC
  by owner via telegram` or `▶ RUNNING — 0 goal run(s), 0 cron run(s), loops
  alive 3/3`.
- A loop is alive when its ticks complete. For any other loop, `/status` prints
  one word and the reason: `silent` (no heartbeat in 24 hours), `dead` (the
  heartbeat stopped), `failing` (the last ticks failed) or `stuck` (one tick
  does not end).
- The run lines count a 24-hour window. They print the runs that ended apart
  from the runs that started, and what a `done` run did: `effect`, `idle`,
  `missed` or `unverified`. A skipped slot is not a run.
- Times show UTC, and your own time beside it when you set `owner.timezone`
  (`polyrob config set owner.timezone Europe/Berlin`).
- A `pause_violation` CRITICAL health item names any autonomous activity
  recorded after the pause (goal runs, cron runs, self-wakes, wallet spend, and
  every outward write the pause covers — a post, a message, a publish, a money
  move — whichever tool made it). Treat it as an incident.
- Whatever tool it uses, an autonomous run is refused a post, a message, a
  publish or a money move while your pause covers it (`social`, `oversight`,
  `apps`, or everything); the refusal is recorded. Running code, editing its own
  skills and plain web requests are recorded but not stopped by a scoped pause.
  Your own requests are never blocked — including money: when YOU ask, in chat
  or with an owner verb (`/send`, `/bridge`, `/pay`), the pause and the
  autonomous ceiling do not apply. The per-transaction and daily caps always do.
- `/why [n]` lists the last refusals (default 5, at most 20): when, which
  tool, and the gate's reason word for word. It reads the recorded refusals, so
  the answer does not depend on the agent's own summary.
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
most of what this list shows. Both are still recorded in the telemetry
event log (`<data home>/telemetry_events.db`), which `polyrob doctor --full`
and `polyrob journey` read.

`/status` counts the missed messages since you last ran `/missed`. A message
that waits for a chat host which never comes back is given up after 7 days;
`/status` names it in its delivery lines as `dead`.

### If you are missing too much

Raise your own budget — the preference wins unless the deployment set an
explicit ceiling, in which case it may only tighten it:

```bash
polyrob config set delivery.daily_cap 60 --user <owner id>
polyrob config set delivery.rate_per_hour 20 --user <owner id>
```

These are per-user preferences, so the command line needs `--user`: the owner id
that `polyrob owner show` prints, or `local` when it says the owner is unbound. In the REPL,
`/config set delivery.daily_cap 60` needs no `--user`.

Safety-bearing messages never queue behind ordinary traffic and never spend
your budget: an approval you are blocked on, a blocked goal, a transaction that
has already moved money, a credit-death or security notice. Quiet hours
(`digest.quiet_hours`) defer rather than drop — held text is delivered when the
window ends.

The **daily digest** is the roll-up for everything above. Turning it on
(`OWNER_DIGEST_ENABLED=true`, `CRON_DELIVERY_ENABLED=true`) is not enough — a
job has to run it:

```bash
polyrob job digest "every day 08:00"     # schedule it (again = move it)
polyrob job digest                       # show what is scheduled
polyrob job digest --off                 # cancel it
```

It costs nothing to run: the message is composed from the ledger, the event log
and your open asks, with no model call. If `polyrob doctor` reports **"owner
digest is enabled but no digest job is scheduled"**, that is this gap.

## MCP servers (giving the agent new tools)

An MCP server is how a protocol hands an agent its own tools — Aave, GitHub, a
docs index. You add one from wherever you are; it is saved against your tenant
and the agent can use it **from the next turn** — no restart, no deploy.

```
/mcp                                 # each server: transport, tool count, last probe, last error
/mcp add aave https://mcp.aave.com   # add one (an api key may follow the URL)
/mcp remove <id>                     # drop it; the agent loses it from the next turn
/mcp test <id>                       # connect now and count the tools
```

Same verbs in the REPL. `/mcp add` finds the transport itself: it tries
Streamable HTTP, then plain HTTP, then SSE, and saves the first one that
answers. A refusal names the HTTP status and the server's first line (for
example `HTTP 401: missing api key`). **HTTPS URLs only.** A local command (`npx …`) is
refused: saving one would save arbitrary code execution on this box, which is
the whole threat model for installing an MCP server. If you genuinely need a
local server, the operator adds it to `config/mcp_config.json` on the box.

What an added server can and cannot do: its tools become callable, and
everything they return is framed as untrusted data rather than instructions. It
gets **no** access to the wallet — no MCP tool can reach the signer, and no money
verb accepts calldata from outside the process. Aave's server, for example,
prepares *unsigned* transactions, so installing it gives the agent Aave's
markets, positions and health factors to read, and no ability to spend.

## First contact on a chat surface

A chat surface starts locked. On Telegram:

- **No allowlist yet.** `polyrob telegram` prints `no allowlist set — message
  the bot once and it will reply with your id, then set ALLOWED_TELEGRAM_USER_IDS
  and restart.` Your first private message gets your numeric id and the exact
  `polyrob config set ALLOWED_TELEGRAM_USER_IDS <id>` line. The agent does not
  run on that message, and each sender gets that reply once per process.
- **Allowlist set.** A private message from an id that is not on the list gets
  no answer at all (pairing off). A single id on the list is your owner seat.
- **Pairing on** (`POLYROB_REQUIRE_PAIRING=true`). A private sender who is not
  the owner and not paired, on the list or not, gets
  `🔒 I'm not allowed to talk to you yet.` with a pairing code. A paired sender
  passes the allowlist. Send `/pair` to
  see the waiting codes and the paired users, and `/pair approve <surface> <code>`
  to approve one (a code without its chat app name is refused; on the box: `polyrob owner pair list|approve`). The code is sent at
  most once per 10 minutes; between, the bot stays silent. The first time a new
  person asks, you get one message, and the request waits in `/inbox` until you
  decide. Neither shows the code: ask the person for it. When you turn pairing
  off, every paired sender loses access on every chat app; the list is kept, so
  they are back when you turn it on again.

⚠️ A paired sender may command the agent on that chat app with their own memory and work. They never reach your wallet, money, approvals, owner commands or your memory, and in local mode they run no code on your host. Approve only a code you expected. A code expires after 15 minutes and works
once; `/pair revoke <surface> <id>` removes a pairing. Only you can run `/pair`:
a paired sender gets "Owner only".

Email has no owner seat: a mail from an address the agent wrote to first is a
correspondent reply, delivered to that session as data, never as instructions.
Every other mail is refused. On every other chat app the owner is the id you
configure for it (`OWNER_SLACK_ID`, `OWNER_FEISHU_ID`, …;
[feishu-dingtalk.md](feishu-dingtalk.md)); group rooms have their own allowlist
([groups.md](groups.md)).

## Approving what the agent proposes

Your own turn and your scheduled jobs need no tap for anything that is not money, a secret
or the host. A queued spend approval, a new person, and whatever a chat that holds someone
else's message, or a group, writes take no effect until you decide. So does an identity note
or an owner rule that a job writes, or that a chat writes after it read a web page. One list holds everything that waits on you: `/inbox` reads these
approvals, open asks, decisions waiting for a tap, signer approvals, and pairing requests. `/pending`, `/asks` and `/cards` show parts of the same
list.

```
/inbox                     # everything waiting on you, blocking first
/pending                   # the approvals in it, each item with its own tap
/approve                   # one item waiting? decide it. several? pick from the list
/approve_all               # the whole queue at once
/reject_all
```

Each item in `/pending` prints its own pair of one-tap tokens, like
`/approve_p_1245c6`. Tap one and you are done — there is no id to copy. The
token is an address, not a secret: it is resolved against the live queue every
time, so a stale one decides nothing.

A plain "approve" or "reject" is a message to the agent, not a decision: only
the slash verbs and the tap tokens decide an item.

An outbound write that a background run queued for you — an X post, reply or
DM, or a mail — can be approved after that run ended. The approval sends exactly
the text you approved, once; it does not re-run the job or goal that wrote it.

On the command line the same queue is `polyrob owner pending`, and
`polyrob owner promote|reject <kind> <id>` (or `… promote all`) decides it.
In the REPL it is `/pending` and `/pending approve <kind> <id>`, and `/gates`
lists which actions stop and wait for your approval.

### Answering an ask in words

An **ask** is different from an approval: the agent is not proposing something,
it is stuck and needs a fact from you. `/asks` lists them.

```
/asks
/fulfill <id>                                   # unblock the goals behind it
/fulfill <id> use the treasury account, not the hot wallet
```

Everything you type after the id is passed to the run itself, and appears in
the retry prompt as *the owner answered: …*. Without it the run learns only
that it may proceed, not what you decided — which is how the same ask comes
back a second time. The answer is kept on the ask too, so you can see later
what you said.

You can also answer in plain words in the chat ("A", "use the second
account"). The agent records that answer on the ask, but only with words you typed in
that message: text from a page, a mail or a search result cannot decide an
ask.

An answer to an ask that a cron job raised re-runs that job now, not at its next
slot. Every seat's reply says where the answer went: the goals it unblocked, or
the job that will read it.

When the agent asks a question with lettered options (`A) … or B) …`), the
Telegram notice carries one answer button per option. A button sends
`/fulfill_<id>_<letter>`; that is the same as `/fulfill <id>` with the option's
own text as your answer. A button only answers the ask the notice is about.

## Apps

A new app address is an owner decision; the agent's deploy records it as
**pending** and waits. Every seat renders the same text:

```
/apps                      # list: slug [status] URL (health)
/apps show <slug>          # the row in full
/apps approve <slug>       # the owner decision; the supervisor deploys within one tick
/apps reject <slug>        # a pending app never runs
/apps kill <slug>          # stop a running app (the address stays approved)
/apps start <slug>         # run a stopped app again on its approved address
/apps remove <slug>        # retire an app: container, files, record and address go
/apps logs <slug> [n]      # recent container log lines
```

CLI: `polyrob apps list|show|approve|reject|kill|start|remove|logs`; the console offers the
same decisions ([console.md](console.md)).
A pending app leads the health block on every status seat until you decide.
`/pause deploying` stops deploys and live containers; `/resume deploying` brings
them back without a redeploy.

The supervisor process that actually runs the containers is owner-owned
(`polyrob apps supervise`, or the unit that calls it) and holds the Docker,
nginx and firewall privilege the agent never has. Standing it up, including the
vhost and certificate: [deployment-postures.md](deployment-postures.md).

## The money verbs you can reach from chat

Every money verb has the same shape as `/bridge`: a **quote by default**, and
an explicit `go` to execute. The full list, with `/pay`, `/lp`, `/launch`,
`/deploy`, `/book` and `/invoices`, is in [chat-commands.md](chat-commands.md),
and `/help` lists the verbs you can run where you are. Typing `go` is YOUR
act: you typed the amount and the address, so there is no second tap, and the
pause and the autonomous ceiling (which bound what I do on my own) do not
apply. The per-transaction cap, the daily cap and the simulation always do, and
the quote shows them before you type `go`.

```
/send <amount>|$<usd> <native|token-address> to <address> on <chain> [max <usd>] [go]   # send ($22 = dollars)
/swap <amount> <native|token> to <token> on <chain> [slippage <bps>] [max <usd>] [go]
/cards                              # open action cards: confirm, refresh, cancel, answer
/claim <token> [go]                 # collect the creator fees a launchpad owes me
/check <address|ticker> [chain]     # look at any wallet or token, read-only
/nft                                # the agent NFTs I own, their accounts and holdings
/nft list|info|account|transfer|revoke … on <chain> [go]   # what I hold, an NFT account, send one, retire an approval
/nft trust|untrust|send … on <chain> [go]          # use, stop using, or give away an agent NFT
/dapp list|revoke <id>              # web pages my wallet is armed for
/identity register|set-uri on <chain> [go]   # my own ERC-8004 registration
/contacts [<surface> <address>]     # third parties I have written to, and one transcript
/thread [n | <hours>h]              # OUR conversation, every session and rail (you are not a contact)
/wallet tokens                      # which tokens I trust and why; what is quarantined
/wallet trust|untrust <chain> <address> [go]   # your word on one token
/writeoff <chain> <address> [go]    # write a holding off: the loss is its recorded cost
/unquarantine <chain> <address> [go]           # undo a look-alike's quarantine
```

**Build it with buttons.** Send `/send` or `/swap` with nothing after it: I answer with
buttons — the chain, then the token, then the recipient (from sends you confirmed
before) or the token to buy, then the amount (a share of the balance). The last tap
gets the real quote. Nothing moves until you tap Confirm on that quote.

**Action cards.** A quote is a
card with **Confirm**, **Refresh** and **Cancel** buttons (Telegram), the same
buttons in the console chat and Inbox, and the typed tokens in the terminal
(`/card_<id>_ok`). Confirm runs the exact line the quote showed — the stored
address, never a re-typed one — once, and for `/send` and `/swap` at most the
quote + 5% (a `/send` of a token count: + 25%, because the count is fixed): if the
price moved further, nothing moves and the card says so. A
decided card loses its buttons. In the console, Money › Moves › *Make a move* builds a
send or swap from pickers (chain, a token I trust, a recent recipient) and answers with
the same card. The agent uses cards too: `propose_action` puts a
money verb to you whose only button gets the real quote (the agent never writes a
confirm), and `owner_ask` asks you to pick one of a few options and waits for
the tap. When the agent lacks something you can fix, `propose_setting` puts the fix
behind a Confirm button — `/mcp add`, a safe `/config set`, or a grant without
money — and Confirm runs that line as if you typed it; a change only the operator can
make is filed as an ask in `/asks`. `/cards` lists what is open. When you have made every decision the agent
raised in a chat, the agent continues by itself: it gets each outcome (sent, failed,
dismissed, expired) and reports back, without you telling it. It still cannot
spend on that turn — a next payment comes to you as a new decision.

A token-identity question ("which XYZ is real?") arrives in `/pending` with a
*trust it* and a *not trusted* tap per contract — see
[onchain.md](onchain.md#which-tokens-the-agent-trusts).

There is no default network. Name it with `on <chain>`: `/nft`, `/identity`
and `/deploy` refuse a line without it and list the networks you can use.

Three things worth knowing before you use them:

- **`/nft transfer` always comes to you for approval.** A collectible has no
  reliable price, so the spend caps cannot bound it and nothing exempts it.
  There is deliberately no verb that GRANTS an approval over a collection —
  only one that retires it.
- **`/identity register` is permanent and happens once.** A second registration
  would mint a second token and leave two identities with no authority between
  them, so the verb refuses when a registration already exists, and refuses
  just as firmly when it cannot READ whether one exists.
- **`/dapp revoke` binds my durable record immediately**, and a bridge already
  armed inside a running session holds its own copy of the authorization. To be
  certain right now, `/pause everything`.

Every one of these is refused from inside a group chat and answered in your
private chat, like the rest of the money and control verbs.

## See also

- [operate-the-service.md](operate-the-service.md) — restart and update, from chat or a shell
- [chat-commands.md](chat-commands.md) — every slash command, per seat
- [rails-verification.md](rails-verification.md) — what counts as proof that a send arrived
- [configuration.md §1](configuration.md#1-the-six-axes) — the six axes, and §6 the autonomy dial
- [cli.md](cli.md#polyrob-owner) — every `polyrob owner` verb
- [security-model.md](security-model.md) — what stops the agent when you are not watching
- [payments.md](payments.md) — money caps, approval lanes and the spend ledger
- [configuration.md §8](configuration.md#8-surfaces-and-who-may-talk-to-it) — the delivery-rail bounds and their flags
