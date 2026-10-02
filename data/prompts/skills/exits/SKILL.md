---
name: exits
description: 'Declare the exit at entry: stop loss, take profit, time limit and trailing stop, run by a scheduled check because the on-chain rail has no native stop, limit or trigger orders; the DEFI_MONITOR_EXITS lane and the exit clamp.'
license: MIT
metadata:
  polyrob-priority: '2'
  polyrob-auto-activate: 'true'
  polyrob-triggers: '{"action_names":[],"keywords":["stop loss","stop-loss","take profit","take-profit","trailing stop","trade exit plan","sell the position","exit the position"],"task_patterns":["(set|plan|define|add).*(stop.?loss|take.?profit|trailing stop)"],"tool_ids":["defi_trade","defi_data","cronjob"]}'
  polyrob-version: '4'
---
# Exits — decide the way out before the way in

Every entry declares its exit at the same moment: a stop, a target, and a time
limit. A position with no declared exit is not ready to be opened.

## What exists today, and what does not

There are NO native stop, limit or trigger orders on the on-chain rail: no
stop-loss order, no take-profit order, no limit order, no DCA or TWAP order on
`defi_trade`. An exit on a DEX is a `swap` that YOU make when a rule fires.
Hyperliquid has limit orders and `reduce_only`, but no trigger (TP/SL) order
verb either — and its venue gate refuses EVERY autonomous order, reduce-only
included, so a scheduled run cannot close a Hyperliquid position. There, a stop
is the owner's order (see `hyperliquid-trading`).

What does exist:

- **A scheduled check (on-chain rail only):** `cronjob_schedule(task=…,
  schedule=…, rig="money_rail", skills=["exits"])` — a recurring run that reads
  the price and sells when a rule fires. Put the address, the rule and the
  numbers in the task text. That run holds no `cronjob` tool, so it cannot
  cancel its own schedule (step 5).
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

| Barrier | Default for a major | Default for a long-tail token |
|---|---|---|
| Stop loss | −8 to −10% | −30 to −50% (or the whole ticket, by size) |
| Take profit | +15 to +25%, or scale out | sell the cost basis at 2–3x |
| Time limit | 7–30 days | 24–72 hours without the thesis confirming |

A trailing stop (for example 10–15% below the high, after +10%) replaces the
fixed stop once it is in profit. Write the barrier PERCENTAGES into the
ledger's exit column (see `position-journal`).

**Check a barrier with `defi_data.positions`, not with your own arithmetic.**
For each position it gives `unrealized_pct` (against the average-cost basis)
for the stop and the target, and `high_water_usd` with `from_high_pct` for the
trailing stop. Compare those figures with the barrier as written. Do not
compute a percentage, a high-water mark or a USD figure yourself. If the verb
says `unknown` (basis unknown, or no price), the barrier cannot fire on that
figure: say so, and use the time limit or the exit quote instead. The
high-water mark only counts prices a `positions` read saw with high
confidence, so read `positions` on every scheduled check.

## The procedure

1. **At entry:** quote the exit route (`pre-trade-check`, step 4). If there is
   no route back, do not enter.
2. **Schedule the check** with `cronjob_schedule`, pinned to this skill, with
   the address and the three barriers in the task text.
3. **When a barrier fires:** read `defi_data.positions` (or `defi_data.price`
   or `ohlcv` for the market), and confirm it with the exit quote itself
   (`swap_quote` for the sell) — what you would actually receive. `price` comes from the deepest priced pool and `ohlcv`
   from one named pool: both are pool data, not an independent oracle, and a
   seeded pool can move them. Then `trade-execution` for the sell, then
   `post-trade-verify`.
4. **Stops are market exits.** Do not wait for a better price after a stop fires.
5. **End the schedule when the position is closed.** The closing run reports
   "position closed — cancel job <id>" to the owner; you cancel it
   (`cronjob_cancel`) from a session that holds `cronjob`, never by leaving the
   job to fire on an empty position.

## Never

- Average down, or move a stop further away because you like the story.
- Leave a position without a schedule that can close it.

## If a tool is missing

These steps need `defi_data` (reads) and `defi_trade` (trades) in THIS session.
If `defi_trade` is not loaded, do the read steps, write the plan, and tell the
owner which grant is missing. If `defi_data` is not loaded either, you cannot
measure anything — say so and stop. Never work around a missing verb with a raw
`call`, a browser dapp, or another agent.

## Related

`pre-trade-check`, `trade-execution`, `position-journal`, `sizing-and-risk`.
