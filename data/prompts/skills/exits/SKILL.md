---
name: exits
description: 'The exit doctrine: the triple barrier (stop, target, time) and the trailing stop from the mandate preset, confirmed with the exit quote; unknown prices cannot fire a price barrier; typed close reasons; dust; the DEFI_MONITOR_EXITS lane and the exit clamp. Code arms and cancels each exit job.'
license: MIT
metadata:
  polyrob-when: 'Setting a stop loss, take profit or time exit'
  polyrob-priority: '2'
  polyrob-auto-activate: 'true'
  polyrob-triggers: '{"tool_ids":["defi_trade","defi_data"]}'
  polyrob-version: '7'
---
# Exits — decide the way out before the way in

Every entry declares its exit at the same moment: a stop, a target, and a time
limit. A position with no declared exit is not ready to be opened. Inside a
trading mandate the declaration is the preset's exit (`trading-mandate`) plus
the entry's `plan`; code arms one exit job per open position and cancels it
when the position closes.

## What exists today, and what does not

There are NO native stop, limit or trigger orders on the on-chain rail: no
stop-loss order, no take-profit order, no limit order, no DCA or TWAP order on
`defi_trade`. An exit on a DEX is a `swap` that YOU make when a rule fires.
Hyperliquid has limit orders and `reduce_only`, but no trigger (TP/SL) order
verb either — and its venue gate refuses EVERY autonomous order, reduce-only
included, so a scheduled run cannot close a Hyperliquid position. There, a stop
is the owner's order (see `hyperliquid-trading`).

What does exist:

- **The mandate's exit job (on-chain rail only):** code arms one recurring job
  per open position of a trading mandate. Its `$0` precheck reads
  `defi_data.position_mark` and wakes a model run only when a barrier is near
  or crossed, the time stop is due, or the price cannot be read. Code cancels
  the job when the position closes, is retired or turns to dust. Never schedule
  an exit job, a cancel job or a "check my positions" job yourself.
- **The exit lane:** with `DEFI_MONITOR_EXITS` on, a monitor run may execute an
  EXIT-shaped trade — selling a held token, within the held balance, into the
  chain's quote asset — that other autonomous turns may not. The quote asset is
  the chain's pinned USDC where the registry pins one (Base, Ethereum,
  Arbitrum, Polygon, and Solana — USDC only, not wSOL), and the wrapped native
  where it pins none (Robinhood Chain: WETH). A sell into any other token is
  not an exit on this lane. Optimism is READ-ONLY here: you can read prices and
  balances on it, but no swap can be sent there, so it is never an exit chain.
- **The exit clamp:** a sell of up to 1% more than the held balance is clamped to
  the balance, so a full exit from a rounded display figure still goes through.

## The triple barrier — set all three at entry

A stop loss, a take profit (or a scale-out), and a time limit — and, once in
profit, a trailing stop that replaces the fixed stop. The numbers live in the
mandate's preset (`memecoin` or `majors`), never in this skill or in a job's
text: `position_mark` computes the barrier from them. The preset's rules hold
at load time: a stop sits below entry, no tier takes profit at break-even (a
break-even lock still pays fees), and a trail arms only above entry. You may
TIGHTEN a stop; you never move it wider than at entry.

**Check a barrier with `defi_data.position_mark`, not with your own
arithmetic.** For one position it gives `barrier` (`none` · `near` · `crossed`
· `unknown`), `barrier_reason` (`stop_loss`, `take_profit`, `trailing`,
`time_stop`, or why it is unknown: `price_unreadable`, `basis_unknown`,
`<barrier>_unverified`), and `unrealized_pct` / `from_high_pct` on a
spend-grade price. `defi_data.positions` gives the same figures for the whole
book. Do not compute a percentage, a high-water mark or a USD figure yourself.
If the figure is `unknown` (basis unknown, or no price), the barrier cannot
fire on that figure: say so, and use the time limit or the exit quote instead.

LOW, disputed or unknown prices cannot fire a price barrier. `positions`
withholds stop/target percentages and distance from the high on those grades.
Obtain an executable sell quote from a separate venue or source and verify that
its minimum proceeds breach the declared barrier before selling for that reason.
A quote from the same disputed or thin pool is not independent confirmation.
If independent confirmation is unavailable, report the uncertainty; the explicit
time limit and an owner's direct sell instruction remain separate reasons to exit.

## The procedure

1. **At entry:** quote the exit route (`pre-trade-check`, step 4). If there is
   no route back, do not enter. Inside a mandate the buy carries its `plan`
   (thesis, confidence, invalidation, preset); the exit runs follow it.
2. **On an exit run** (the job's task names ONE position by chain, account
   and address): read `defi_data.position_mark` for it. Follow the stored
   plan: an invalidation that happened is a reason to exit before a barrier.
3. **When a barrier fires:** confirm it with the exit quote itself
   (`swap_quote` for the sell) — what you would actually receive. `price` comes
   from the deepest priced pool and `ohlcv` from one named pool: both are pool
   data, not an independent oracle, and a seeded pool can move them. Re-screen
   the token with `token_info` (dev wallets selling, a holder cluster
   splitting). Then `trade-execution` for the sell, then `post-trade-verify`.
4. **Say why it closes:** pass `close_reason` on the sell — `stop_loss`,
   `take_profit`, `trailing`, `time_stop`, `owner` (the owner asked), `manual`
   (your judgment on the plan), or `breaker:<name>`. Code records `retired`,
   `dust` and `orphan` itself. The reason reaches the realized row and
   `/trade status`.
5. **Stops are market exits.** Do not wait for a better price after a stop
   fires. Act in the SAME run you see it; a postponed exit is not a rule.
6. **The job ends by itself.** When the book row closes, code cancels the exit
   job. You never cancel it and never leave a note asking someone to.

## Dust — skip it, never escalate it

A position worth less than $1 is DUST: `defi_data.positions` marks it `DUST`
(and `dust: true` in its metadata). Gas to sell it costs more than it is worth.
On an exit check, skip every dust position: do not quote, approve or sell it,
and do not message the owner about it. If every open position is dust, the
check has nothing to do: close the run with `SKIPPED: only dust` and stop. A
position with an UNKNOWN value is not dust — treat it by the rules above.

## Never

- Average down, or move a stop further away because you like the story.
- Build an exit job by hand. If `/trade status` shows a position
  `UNPROTECTED`, the lifecycle pass arms it; tell the owner what you saw.

## If a tool is missing

These steps need `defi_data` (reads) and `defi_trade` (trades) in THIS session.
If `defi_trade` is not loaded, do the read steps, write the plan, and tell the
owner which grant is missing. If `defi_data` is not loaded either, you cannot
measure anything — say so and stop. Never work around a missing verb with a raw
`call`, a browser dapp, or another agent.

## Related

`trading-mandate`, `pre-trade-check`, `trade-execution`, `position-journal`,
`sizing-and-risk`.
