---
name: position-journal
description: 'Keep the position book honest: the rail book reconcile reads, every fill recorded from its measured receipt, P&L read from defi_data.positions (average cost, computed by the verb, never by hand), unknown is never zero, and never summing books.'
license: MIT
metadata:
  polyrob-when: 'Recording fills and reading P&L of positions'
  polyrob-priority: '2'
  polyrob-auto-activate: 'true'
  polyrob-triggers: '{"tool_ids":["defi_trade","defi_data"]}'
  polyrob-version: '4'
---
# Position Journal — the book, and what is not tracked yet

The money rail keeps ONE position book (`open_positions`). You do not write
it: every confirmed swap whose receipt could be measured records its position
there. The chain is the truth; the book must match it.

## What exists today

- **The rail book.** A confirmed EVM `swap` records the size from the receipt
  and the cost basis in USD. Each sell books its realized P&L there. The cost
  method is AVERAGE COST: a partial sell reduces the basis in proportion to the
  size sold. `defi_data.positions(chain=…)` reads this book and does every sum
  for you: size (measured or quoted), basis, price, value, unrealized P&L in
  USD and %, realized P&L, and the high-water price. The owner sees the same
  basis as "Entry" in `/book`.
- **Reconcile.** `defi_data.reconcile(chain=…)` compares the book with on-chain
  balances and returns a VERDICT (`CLEAN`, `DISAGREEMENT`, `UNVERIFIED`). Its
  output is authoritative over your memory and over any earlier summary.
- **Basis unknown.** A position has NO basis when it was inherited, bought with
  `solana_swap` (which records no position yet), or bought by a swap the rail
  could not value. `positions` then says "basis unknown" and gives no P&L.
  Unknown is not zero: never write $0 as its cost, and never compute a P&L for
  it yourself. USDC and the wrapped native are working capital, not positions:
  a USDC ↔ WETH swap records no position.
- **What is NOT tracked yet:** fees per trade, and venue positions
  (Hyperliquid, Polymarket) in the same book.

## The procedure

1. **Verify every fill from its measured receipt** (see `post-trade-verify`):
   chain, address, side, measured quantity, gas, tx hash, and WHY you traded.
   The quantity is the measured chain delta — never the quoted output and never
   the "fill" a tool result reports. Put the WHY in your run report.
2. **Read P&L from `defi_data.positions`.** It computes unrealized and realized
   P&L by average cost — the same method the rail book uses. Repeat its figures
   as written. Do not keep lots by hand, do not use FIFO, and do not compute or
   convert a money figure yourself. If `positions` says `unknown`, say
   `unknown`.
3. **A position the rail book does not hold** (a Solana buy) has no
   verb-computed P&L. Report its value from `defi_data.portfolio` and its P&L
   as `unknown` — never an estimate.
4. **Reconcile before any claim** about the book, before a new entry on the
   same token, and after anything that looked wrong. On 2026-08-25 the record
   said "Open positions: NONE" while the chain held three — and that was
   published.
5. **Holdings the book does not explain** are either a position bought outside
   the money rail or something someone sent you (dust; never interact). Say
   which in your report. `reconcile` names an unpriced token the book does not
   hold as `unsolicited`: it is not an issue, and you never interact with it.
   A book row the chain does not back is closed by the owner with `/writeoff`.
6. **One symbol, two contracts:** if `reconcile` prints the collision warning,
   the book row's address decides which contract is yours — never the symbol.
7. **Never sum books.** Each chain, each wallet, and each venue keeps its own
   rows; a treasury figure names what it includes.

## If a tool is missing

These steps need `defi_data` (reads) and `defi_trade` (trades) in THIS session.
If `defi_trade` is not loaded, do the read steps, write the plan, and tell the
owner which grant is missing. If `defi_data` is not loaded either, you cannot
measure anything — say so and stop. Never work around a missing verb with a raw
`call`, a browser dapp, or another agent.

## Related

`post-trade-verify`, `exits`, `treasury-trading` (the reconcile section there).
To check SOMEONE ELSE's wallet, use
`defi_data.wallet_holdings(address=…, chain=…)` — `positions` and `portfolio`
read only this agent's own book.
