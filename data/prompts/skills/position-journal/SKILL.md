---
name: position-journal
description: 'The position book is the journal: every confirmed swap writes it, an entry carries its plan, a sell its close reason; P&L read from defi_data.positions and /trade status (average cost, computed by the verb, never by hand), reconcile, retire, unknown is never zero, and never summing books.'
license: MIT
metadata:
  polyrob-when: 'Recording fills and reading P&L of positions'
  polyrob-priority: '2'
  polyrob-auto-activate: 'true'
  polyrob-triggers: '{"tool_ids":["defi_trade","defi_data"]}'
  polyrob-version: '5'
---
# Position Journal — the book is the journal

The money rail keeps ONE position book (`open_positions`). You do not write
it: every confirmed swap whose receipt could be measured records its position
there. The chain is the truth; the book must match it. Every figure the owner
or the public sees is rendered from this book. Do not keep a second record — no
markdown ledger, no trade table in the knowledge base, no running total in a
note. A second record is what disagreed with the chain before.

## What the book holds

- **Positions.** A confirmed `swap` (EVM) or `solana_swap` records the size
  from the receipt or the vetted simulation and the cost basis in USD. Each
  sell books its realized P&L. The cost method is AVERAGE COST: a partial sell
  reduces the basis in proportion to the size sold. The native coin or the
  wrapped native bought WITH A STABLE is a position too; the same coin received
  from an exit is not. `defi_data.positions(chain=…)` reads the book and does every sum for
  you: size, basis, price, value, unrealized P&L in USD and %, realized P&L,
  and the high-water price. The owner sees the same basis as "Entry" in
  `/book`.
- **The plan.** Inside a trading mandate every entry carries its `plan`
  (thesis, confidence, invalidation, preset); it is stored beside the book row
  and the exit runs follow it. Write the WHY there, not in a note.
- **The close reason.** Every sell says why it closes with `close_reason`
  (`exits` lists the values). It reaches the realized row, and `/trade status`
  shows the reasons beside the realized P&L.
- **Reconcile.** `defi_data.reconcile(chain=…)` compares the book with on-chain
  balances and returns a VERDICT (`CLEAN`, `DISAGREEMENT`, `UNVERIFIED`). Its
  output is authoritative over your memory and over any earlier summary.
- **Basis unknown.** A position has NO basis when it was inherited or bought by
  a swap the rail could not value. `positions` then says "basis unknown" and
  gives no P&L. Unknown is not zero: never write $0 as its cost, and never
  compute a P&L for it yourself.
- **What the book does not hold:** fees per trade (so P&L is gross of gas),
  and venue positions (Hyperliquid, Polymarket).

## The procedure

1. **Verify every fill from its measured receipt** (see `post-trade-verify`):
   chain, address, side, measured quantity, gas, tx hash. The quantity is the
   measured chain delta — never the quoted output and never the "fill" a tool
   result reports.
2. **Read P&L from the verbs.** `defi_data.positions` per chain; `/trade
   status` for a mandate (allocation used, PROTECTED/UNPROTECTED, realized
   gross, the mandate's LLM cost). Repeat their figures as written. Do not keep
   lots by hand, do not use FIFO, and do not compute or convert a money figure
   yourself. If a verb says `unknown` or "at least", say the same.
3. **Reconcile before any claim** about the book, before a new entry on the
   same token, and after anything that looked wrong. On 2026-08-25 the record
   said "Open positions: NONE" while the chain held three — and that was
   published.
4. **Holdings the book does not explain** are either a position bought outside
   the money rail or something someone sent you (dust; never interact). Say
   which in your report. `reconcile` names an unpriced token the book does not
   hold as `unsolicited`: it is not an issue, and you never interact with it.
5. **A book row the chain does not back:** if the measured balance is zero or
   dust, retire it with `defi_data.retire_row` (it refuses unless the chain
   proves it). A loss to close is the owner's `/writeoff`.
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

`post-trade-verify`, `exits`, `trading-mandate`, `treasury-trading` (the
reconcile section there). To check SOMEONE ELSE's wallet, use
`defi_data.wallet_holdings(address=…, chain=…)` — `positions` and `portfolio`
read only this agent's own book.
