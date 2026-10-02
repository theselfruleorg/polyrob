---
name: position-journal
description: 'Keep the position ledger honest: the Open positions table reconcile reads, per-fill rows from measured receipts, P&L read from defi_data.positions (average cost, computed by the verb, never by hand), unknown is never zero, and never summing ledgers.'
license: MIT
metadata:
  polyrob-priority: '2'
  polyrob-auto-activate: 'true'
  polyrob-triggers: '{"action_names":["defi_data_reconcile","defi_data_positions"],"keywords":["position ledger","open positions table","cost basis","trade journal","realized pnl","unrealized pnl","trading pnl","position pnl","reconcile the position ledger","reconcile the trading ledger"],"task_patterns":["(update|write|record|fix).*(ledger|journal).*(trade|position|fill)"],"tool_ids":["defi_trade","defi_data"]}'
  polyrob-version: '3'
---
# Position Journal — the ledger, the book, and what is not tracked yet

You keep one position ledger (markdown, usually `kb-root-position-ledger.md`
in the project directory). Its `## Open positions` table is the state; the run
log under it is narrative. The chain is the truth; the ledger must match it.

## What exists today

- **The ledger table** you write. `defi_data.reconcile(chain=…, ledger_path=…)`
  compares it row by row with on-chain balances and returns a VERDICT
  (`CLEAN`, `DISAGREEMENT`, `UNVERIFIED`). Its output is authoritative over your
  memory and over any earlier summary.
- **The rail book.** A confirmed EVM `swap` whose receipt could be measured
  records the position in the rail store: the size from the receipt, and the
  cost basis in USD. Each sell books its realized P&L there. The cost method is
  AVERAGE COST: a partial sell reduces the basis in proportion to the size sold.
  `defi_data.positions(chain=…)` reads this book and does every sum for you:
  size (measured or quoted), basis, price, value, unrealized P&L in USD and %,
  realized P&L, and the high-water price. `reconcile` also compares this book
  with the chain. The owner sees the same basis as "Entry" in `/book`.
- **Ledger-only holdings.** A holding bought before the rail book existed has no
  row there. `positions` lists it under LEDGER-ONLY: the size from the ledger,
  basis unknown, no P&L and no high-water. The ledger records no chain, so pass
  `chain=…` to price it. If `positions` says the ledger "could not read", the
  list may be incomplete: check `reconcile` or `/book`.
- **Basis unknown.** A position has NO basis when it was bought before the rail
  book existed, inherited, bought with `solana_swap` (which records no position
  yet), or bought by a swap the rail could not value. `positions` then says
  "basis unknown" and gives no P&L. Unknown is not zero: never write $0 as its
  cost, and never compute a P&L for it yourself. USDC and the wrapped native
  are working capital, not positions: a USDC ↔ WETH swap records no position.
- **What is NOT tracked yet:** fees per trade, and venue positions
  (Hyperliquid, Polymarket) in the same book.

## The table — one row per open position

```
## Open positions
| Token | Address | Qty | Entry USD | Opened | Exit plan |
|---|---|---|---|---|---|
| EXMPL | 0x1111111111111111111111111111111111111111 | 2500.0 | 12.40 | 2026-09-26 | stop −40%, target 3x, 72 h |

Copy the Qty from the measured balance and the Entry USD from
`defi_data.positions` (or write `unknown`). Do not type a figure you computed.
```

`reconcile` reads the FIRST address in a row and the first number after it as
the quantity, so keep that order. A symbol alone is not a row — it must carry
the address.

## The procedure

1. **Record every fill from its measured receipt** (see `post-trade-verify`):
   date, chain, address, side, measured quantity, gas, tx hash, and WHY you
   traded. The quantity is the measured chain delta — never the quoted output
   and never the "fill" a tool result reports.
2. **Read P&L from `defi_data.positions`.** It computes unrealized and realized
   P&L by average cost — the same method the rail book uses. Copy its figures
   into the ledger as written. Do not keep lots by hand, do not use FIFO, and
   do not compute or convert a money figure yourself: the verb does the
   arithmetic. If `positions` says `unknown`, write `unknown`.
3. **A position the rail book does not hold** (a Solana buy, a pre-book buy)
   has no verb-computed P&L. Report its value from `defi_data.portfolio` and
   its P&L as `unknown` — never an estimate.
4. **Reconcile before any claim** about the book, before a new entry on the
   same token, and after anything that looked wrong. On 2026-08-25 the table said
   "Open positions: NONE" while the chain held three — and that was published.
5. **Holdings the ledger does not explain** are either a position you forgot to
   record (add the row) or something someone sent you (record it as dust; never
   interact). Decide and write it down in the same run. `reconcile` names an
   unpriced token that neither the ledger nor the rail book holds as
   `unsolicited`: it is not an issue, and you never interact with it. Each
   reconcile row ends with its source — `[ledger]` or `[rail store]`.
6. **One symbol, two contracts:** if `reconcile` prints the collision warning,
   the row's address decides which contract is yours — never the symbol.
7. **Never sum ledgers.** Each chain, each wallet, and each venue keeps its own
   rows; a treasury figure names what it includes.

## If a tool is missing

These steps need `defi_data` (reads) and `defi_trade` (trades) in THIS session.
If `defi_trade` is not loaded, do the read steps, write the plan, and tell the
owner which grant is missing. If `defi_data` is not loaded either, you cannot
measure anything — say so and stop. Never work around a missing verb with a raw
`call`, a browser dapp, or another agent.

## Related

`post-trade-verify`, `exits`, `treasury-trading` (the ledger and reconcile
section there). To check SOMEONE ELSE's wallet, use
`defi_data.wallet_holdings(address=…, chain=…)` — `positions` and `portfolio`
read only this agent's own book.
