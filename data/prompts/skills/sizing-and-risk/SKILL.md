---
name: sizing-and-risk
description: 'Size a trade from the risk you accept: risk per trade, caps by asset class, quarter Kelly for bets, loss breakers kept in the ledger (none exist in code yet), and the units check that stops a 1000x amount error.'
license: MIT
metadata:
  polyrob-priority: '2'
  polyrob-auto-activate: 'true'
  polyrob-triggers: '{"action_names":[],"keywords":["position size","position sizing","risk per trade","max drawdown","daily loss","kelly","bankroll","exposure cap"],"task_patterns":["\\b(size|sizing)\\b.*\\b(trade|bet|ticket)\\b","position siz(e|ing)"],"tool_ids":["defi_trade","defi_data"]}'
  polyrob-version: '4'
---
# Sizing and Risk — how much, and when to stop

Size every trade from the risk you accept, not from the amount you would like
to buy. The code bounds the worst case; you choose the size under it.

## What the code enforces

- `max_spend_usd` bounds the simulated outflow on the `defi_trade` verbs that
  take it (`swap`, `solana_swap`, `approve_token`, `transfer`, the LP and deploy
  verbs). Not every money verb has it: `bridge` is bounded by the amount you
  bridge, `x402_fetch` by `max_amount_usd`, venue orders by the venue live cap.
- The per-transaction ceiling and the rolling 24-hour cap apply to every lane.
  Above the autonomous ceiling a trade you start on your own needs the owner's
  tap: a LIVE call raises the card (a dry run does not), and an approved call is
  sent. A spend the owner asks for himself is not held by the ceiling.
- An unverified token above $5 without an agreeing route check is refused.
- Venue trades (Hyperliquid, Polymarket) have their own live cap
  (`HYPERLIQUID_TRADE_MAX_USD`, `POLYMARKET_TRADE_MAX_USD`, $5 by default) and
  are refused on every autonomous turn — the owner drives them.

What the on-chain rail does NOT have yet: a per-strategy or per-goal budget,
and a loss breaker (a daily-loss or drawdown halt). Those are yours to keep, in
the ledger. (Hyperliquid has its own daily-loss and total-exposure caps in the
venue code; they do not cover `defi_trade`.)

## Sizing rules

1. **Risk per trade 0.5–2% of the treasury.** Size = risk in USD ÷ stop
   distance. A $1,000 treasury, 1% risk, and a 25% stop gives a $40 ticket.
   Take the treasury value from `defi_data.portfolio` (one chain per read; name
   the chains a total covers), never from memory. The ticket is a decision you
   make, and `max_spend_usd` bounds it — but every figure you REPORT (value,
   basis, P&L) comes from a verb, never from your own arithmetic.
2. **Caps by class** (share of the treasury in one position): blue chip ≤10%,
   mid cap ≤5%, small cap ≤2%, micro cap or meme ≤0.5%. All memes together are
   ONE correlated bucket.
3. **The memecoin ladder** in `treasury-trading` (Tier A $5, B $2.50, C $1)
   and the percent caps above both apply. **Use the smaller of the two.** On a
   $1,000 treasury the meme cap (0.5%) is $5 and the ladder decides; on a $10
   book a $5 Tier A ticket would be half the treasury, so the 0.5% cap decides.
   If the smaller number is too small to trade after gas, do not trade — tell
   the owner the book is too small for the strategy.
4. **Bets (prediction markets):** a quarter of the Kelly fraction, computed
   AFTER fees and after walking the book, with a hard cap per bet.
5. **Unknown lowers the size.** A check that came back unknown moves you one
   class down, never up.

## Loss breakers — check before every new entry

Read the results from `defi_data.positions` (realized and unrealized P&L by
average cost, computed by the verb) and the treasury value from
`defi_data.portfolio`. Compare those figures with the limits below; do not
compute a P&L yourself. A figure the verb reports as `unknown` is not a zero —
treat the breaker as possibly tripped and enter nothing until it is known:

- daily loss beyond −3% of the treasury: no new entries today;
- three losing trades in a row: halve the size until a winner;
- drawdown beyond −15% from the treasury's high: stop new entries and tell the
  owner.

Exits and stops keep running while a breaker is on — a breaker blocks entries,
never exits.

**On the memecoin book** (`treasury-trading`) most tickets are expected to go
to zero, so a losing streak is the strategy, not a signal. There:

- the daily-loss and drawdown breakers still apply, measured on the WHOLE
  treasury — all memes are one bucket, and the bucket's loss is what counts;
- "three losing trades in a row" does not halve the ladder — judge the book
  over a run of tickets, never the single trade;
- a position's own −50% stop and 48-hour time stop (`exits`) are exits, not
  breakers, and they keep running.

## Units — the costliest mistake

Every amount has a token, a decimals value, and a USD value. Say all three
before a send ("50,000 EXMPL = $0.83 at $0.0000166"). An agent once meant to send
52,439 tokens and sent 52,439,283 — 5% of the supply — after a restart lost its
state. If the USD value surprises you, stop.

## If a tool is missing

These steps need `defi_data` (reads) and `defi_trade` (trades) in THIS session.
If `defi_trade` is not loaded, do the read steps, write the plan, and tell the
owner which grant is missing. If `defi_data` is not loaded either, you cannot
measure anything — say so and stop. Never work around a missing verb with a raw
`call`, a browser dapp, or another agent.

## Related

`pre-trade-check`, `exits`, `position-journal`, `treasury-trading`.
