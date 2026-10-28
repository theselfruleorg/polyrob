---
name: sizing-and-risk
description: 'Size a trade from the risk you accept: risk per trade, caps by asset class, the mandate preset ladder and stage ticket, quarter Kelly for bets, where the loss breakers live (the trading mandate), and the units check that stops a 1000x amount error.'
license: MIT
metadata:
  polyrob-when: 'Deciding how much to put into a trade'
  polyrob-priority: '2'
  polyrob-auto-activate: 'true'
  polyrob-triggers: '{"tool_ids":["defi_trade","defi_data"]}'
  polyrob-version: '5'
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
- Inside a trading mandate (`trading-mandate`), the money kernel also refuses
  an ENTRY when the mandate's allocation at cost or its slots are used, the
  day's entry count is reached, the token is in its re-entry cooldown, or the
  mandate is paused, halted or cooling down. Its loss breakers (daily loss,
  drawdown, consecutive losers, LLM cost over allocation) are the preset's,
  in code, and a tripped one LATCHES until the owner releases it.
  (Hyperliquid has its own daily-loss and total-exposure caps in the venue
  code; they do not cover `defi_trade`.)

## Sizing rules

1. **Risk per trade 0.5–2% of the treasury.** Size = risk in USD ÷ stop
   distance. A $1,000 treasury, 1% risk, and a 25% stop gives a $40 ticket.
   Take the treasury value from `defi_data.portfolio` (one chain per read; name
   the chains a total covers) and a position's size and basis from
   `defi_data.positions`, never from memory. The ticket is a decision you
   make, and `max_spend_usd` bounds it — but every figure you REPORT (value,
   basis, P&L) comes from a verb, never from your own arithmetic.
2. **Caps by class** (share of the treasury in one position): blue chip ≤10%,
   mid cap ≤5%, small cap ≤2%, micro cap or meme ≤0.5%. All memes together are
   ONE correlated bucket.
3. **Inside a mandate, the preset sizes the ticket:** the tier's rung on the
   preset ladder (`treasury-trading`; the tier ceiling comes from
   `token_info`'s survivability line), capped by the mandate's stage ticket
   (probe, small or normal). The mandate's allocation already is the bucket,
   so the percent caps above are for a trade outside a mandate. If the ticket
   is too small to trade after gas, do not trade — tell the owner the mandate
   is too small for the strategy.
4. **Bets (prediction markets):** a quarter of the Kelly fraction, computed
   AFTER fees and after walking the book, with a hard cap per bet.
5. **Unknown lowers the size.** A check that came back unknown moves you one
   class down, never up.

## Loss breakers — code keeps them

On a mandate the breakers are code, not your bookkeeping: the lifecycle pass
measures the realized P&L from the book and latches the mandate `halted` when
one trips. You do not compute a P&L to check one, and you never release one —
`/trade status` shows the state (`breaker: LATCHED …` or `cooling down until
…`), and `/trade release` is the owner's. Exits and stops keep running while a
breaker is on — a breaker blocks entries, never exits.

On the memecoin preset most tickets are expected to go to zero, so a losing
streak is the strategy, not a signal: judge the book over a run of tickets,
never the single trade. A position's own stop and time stop (`exits`) are
exits, not breakers, and they keep running.

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

`trading-mandate`, `pre-trade-check`, `exits`, `position-journal`,
`treasury-trading`.
