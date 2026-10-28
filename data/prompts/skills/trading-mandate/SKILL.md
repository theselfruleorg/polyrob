---
name: trading-mandate
description: 'Start, change or stop a standing trading programme: a short intake, ONE owner card (the mandate), and what code then runs by itself — the hunt, an exit job per position, the daily review, the breakers and the probe-small-normal ladder. What a hunt run and an exit run do, and how to read /trade status.'
license: MIT
metadata:
  polyrob-when: 'The owner wants to start, change or stop trading'
  polyrob-priority: '1'
  polyrob-auto-activate: 'true'
  polyrob-triggers: '{"tool_ids":["defi_trade","defi_data"]}'
  polyrob-version: '1'
---
# Trading mandate — the owner says it once, code runs it

When the owner says "I want to trade tokens", "start trading", "trade with
$150 on Solana" or "stop trading", he is asking for a **mandate**: one record,
confirmed on one card, that owns every job of the programme. You run the
intake and propose the card. Code does the rest.

## What code does by itself — never build it

Once the owner confirms the card, code creates and retires every job:

- **The hunt** — one recurring job per mandate. A `$0` precheck counts fresh
  candidates on the mandate chains and skips the tick when there is none, when
  every slot is in use, when the allocation is used, or when the mandate is not
  active. You run only when there is something to look at.
- **An exit job per open position** — armed by code the moment a buy opens a
  position, cancelled by code when the position closes, is retired or turns to
  dust. Its `$0` precheck marks the position against the preset's barriers and
  wakes you only when a barrier is near or crossed, the time stop is due, or
  the price cannot be read.
- **The daily review** — one run a day: P&L, what fired, breaker state, and the
  four questions of `treasury-trading` "Learning the meta".
- **Reports** — fills, exits and halts reach the owner as event pushes, and
  the daily digest has a Trading section. Nobody schedules a report job.
- **Breakers** — the money kernel refuses a new ENTRY when the mandate is
  paused, halted, stopped or cooling down, when the slots or the allocation are
  used, when the day's entry count is reached, or when the token was sold too
  recently. A loss breaker LATCHES: entries stay off until the owner releases
  it. Exits always run — a pause, a halt or a breaker never blocks a sell.
- **The ladder (micro-live)** — every mandate starts at stage `probe`: one
  probe-size round trip per chain (buy, exit armed, sell) proves the path. Then
  `small` tickets, then `normal` once realized P&L at `small` covers the added
  risk. A breaker trip drops one stage. Code moves the stage; the owner sees
  each step.

**Never create a trading goal, an exit job, a cancel job or a report job
yourself** — not with `goal_create`, not with `cronjob_schedule`. A
hand-built job duplicates the structure, has no precheck, and outlives its
position (one such job ran 59 times on dust). If a position looks unprotected,
`/trade status` says `UNPROTECTED` and the lifecycle pass arms it; tell the
owner what you saw.

## The intake — at most four questions, in ONE message

Ask only what the owner has not already said. Never re-ask an answer he gave.
State the default with each question, by preset NAME:

1. **Style** — a preset: `memecoin` (fresh pairs on the mandate chains, the
   `treasury-trading` doctrine) or `majors` (ETH, SOL, BTC wrappers against the
   pinned stable). "Tokens" with no detail means `memecoin`.
2. **Chains** — default: where the funds already sit. Check first with
   `defi_data.portfolio(chain=…)` on each chain (one chain per read) and name
   what you found ("USDC on Base, SOL on Solana"). Never guess a balance.
3. **Budget** — the allocation in USD for open positions at cost. There is NO
   default: if the owner did not give a number, ask for one.
4. **Ticket and slots** (optional) — default: the preset's own. Say "the
   memecoin preset's ticket and slots", not a number; the card shows the
   numbers.

Then echo the whole mandate in ONE line and propose the card:

```
trade_mandate_propose(preset="memecoin", chains=["base","solana"],
    budget_usd=150)            # ticket_usd=…, slots=… only if the owner said them
```

The card shows the full mandate — ticket, slots, exit preset, breakers, the
expected cost of one stop-out — with **Confirm / Change / Cancel**. Quote a
number to the owner only as the card shows it. An imperative message ("start
trading with $150 on Solana") still gets the card: money on intent is one tap.
If the tool refuses because a mandate is already live, report its line and
offer `/trade pause` or `/trade stop`; do not work around it. The agent may
only TIGHTEN a preset number; any other change is the owner's, on the card.

If `trade_mandate_propose` is not loaded in this session, do the intake, echo
the mandate in one line, and tell the owner to start it with `/trade start`.

## On a hunt run (a job of the mandate)

The run's task names the mandate. Before any buy:

1. **Reconcile** — `defi_data.reconcile(chain=…)` for each mandate chain
   (`treasury-trading`, the reconcile step).
2. **Candidates** — the discovery reads on the mandate chains
   (`memecoin-scouting`). An UNAVAILABLE read is not an empty one: say which
   chain could not be read.
3. **Screen every candidate** with `defi_data.token_info(chain=…, address=…)`.
   Read its `survivability:` line: `REJECT` means skip, always. Otherwise it
   gives a **tier ceiling** (A, B or C) — you may size at that tier or lower,
   never higher. An unknown gate already lowered the ceiling one tier; unknown
   is never safe. A `PARTIAL` screen says which checks did not run.
4. **Measure** — `pre-trade-check`, including the exit quote. No route back
   means no entry.
5. **Size** — the smaller of the tier's rung on the preset ladder and the
   STAGE ticket (probe, small or normal); the run's task states both. Set
   `max_spend_usd` to it. If the task names no stage ticket, do not guess a
   size: enter nothing and say so in the report.
6. **Every entry carries a `plan`** — `defi_trade.swap(chain=…, …, plan={"thesis": …,
   "confidence": 0.0–1.0, "invalidation": …, "preset": "memecoin"})` (the same
   field on `solana_swap`). A buy inside a mandate run is refused without it.
   The thesis says why this token, now; the invalidation says what proves it
   wrong. Be honest with the confidence.
7. Then `trade-execution` and `post-trade-verify`. You do not arm the exit:
   the book row opens the exit job.

A refusal from the mandate step ("slots in use", "allocation is used",
"PAUSED", "HALTED", "token cooldown") is the structure working. Report it in
one line and end the run; never retry around it.

## On an exit run (an exit job of the mandate)

The run's task names ONE position (chain, account, address) and pins `exits`,
which holds the procedure. In short: read `defi_data.position_mark` for the
barrier, follow the stored plan (stops only TIGHTEN), confirm with the exit
quote, sell with `close_reason` set, then `post-trade-verify`. You never cancel
the exit job: code cancels it when the book row closes.

## Reading `/trade status`

`/trade status` is the one renderer; the digest and the status snapshot read
the same lines.

- `mandate <id>: ACTIVE · preset … · stage … · chains … · allocation $X of $Y
  at cost · slots N/M`. `PAUSED` = entries off, exits on.
- `breaker: LATCHED — <reason> …; /trade release` — entries stay off until the
  owner releases it. `cooling down until …` expires by itself.
- `mandate positions:` one line per position with cost, value and
  `PROTECTED` (a live exit job) or `UNPROTECTED`. `exit UNKNOWN` means the job
  store could not be read — not the same as unprotected.
- `value at least $X (N of M priced)` — a partial read; say "at least".
- `realized P&L: … gross` — gas is not recorded, so net of gas is unknown.
- `LLM cost of this mandate:` — what the watching cost, beside the P&L.
- `UNKNOWN — … could not be read` is never "none". Say UNKNOWN.

## The owner's verbs

`/trade start` (the intake card) · `/trade status` · `/trade pause` (entries
off, exits on) · `/trade resume` · `/trade stop` (the hunt and review end;
exits finish the open positions, or the owner chooses "close all") ·
`/trade release` (a latched breaker). Only the owner resumes, stops or
releases: a breaker is his call. When the owner says "stop trading" in plain
words, name the verb or offer the card; chat text is never a command.

## If a tool is missing

- No `defi_data`: you cannot check holdings, screen or mark. Say so; do not
  propose a mandate from remembered balances.
- No `defi_trade` on a hunt or exit run: do the read steps, report what you
  would do, and name the missing grant once.
- Never work around a missing verb with a raw `call`, a browser dapp, a
  hand-built job or another agent.

## Related

`treasury-trading` (the memecoin doctrine), `exits`, `sizing-and-risk`,
`position-journal`, `pre-trade-check`, `trade-execution`, `post-trade-verify`.
