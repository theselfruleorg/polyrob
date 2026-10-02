---
name: post-trade-verify
description: 'Verify a trade after it confirms: receipt, measured amount received, the bought token at the intended address, the measured amount beside the quote, leftover allowances, the ledger row, and reconcile. A tx hash is not proof.'
license: MIT
metadata:
  polyrob-priority: '2'
  polyrob-auto-activate: 'true'
  polyrob-triggers: '{"action_names":[],"keywords":["verify the trade","verify the fill","verify the swap","did the swap land","trade receipt","tx receipt","fill price","confirm the fill","realized slippage"],"task_patterns":["(verify|confirm|check).*(fill price|tx receipt|trade receipt|swap landed|trade landed)"],"tool_ids":["defi_trade","defi_data"]}'
  polyrob-version: '3'
---
# Post-Trade Verify — a tx hash is not proof

Run this after every `RESULT: CONFIRMED`. A trade is finished when you have
MEASURED what arrived, not when the transaction was sent.

## What the code gives you — it differs by chain

**EVM `swap`:**

- The result names the tx hash, the block, the quoted output and the minimum
  after slippage.
- When the receipt's Transfer logs can be read, the settled notice reports the
  MEASURED amount received, for example `measured +0.00098357 WETH`, and the
  tracked position is written with its cost basis — for a real position only: a
  swap between working-capital assets (USDC ↔ the wrapped native, e.g.
  USDC ↔ WETH) records NO tracked position on either side. When they cannot be read, the
  notice carries the quote only and NO position is written — the result still
  says CONFIRMED. A reverted or unconfirmed swap writes nothing.
- The spend audit row records `asset: <token_in>-><token_out>`.

**Solana `solana_swap`:**

- The result is `RESULT: CONFIRMED (<detail>)` with a `sig:` — no block, no
  measured output.
- No tracked position is written, and the audit row names the mint as its
  counterparty, not an asset pair.
- You measure the fill yourself: the balance before and after (step 2).

Never substitute the QUOTE for a measurement you do not have. Unmeasured is
unknown.

## The procedure

1. **Receipt.** `CONFIRMED` (a block number on EVM, a `sig:` on Solana).
   `BROADCAST BUT NOT CONFIRMED` is `pending`, never failed — check again before
   you conclude anything.
2. **Balance delta.** `defi_data.portfolio(chain=…)` before AND after the
   trade — on Solana, and on EVM when no measured amount was reported, this
   delta is the only measurement you have. The
   token you bought must appear at the ADDRESS you intended (read the address,
   not the symbol — see `token-identity`). If a collision warning appears, stop.
3. **Fill vs quote.** Put the measured amount that arrived beside the quoted
   output and the minimum, as the tools printed them, with the gas you paid. A
   measured amount below the minimum is a defect to report. Do not compute a
   fill price or a USD figure yourself: the rail book's basis for the position
   is in `defi_data.positions`, and that is the cost you report.
4. **Leftovers.** An allowance you granted for a one-off trade: revoke it.
   Dust left in `token_in`: record it; do not chase it.
5. **Write the ledger row** (see `position-journal`) with the tx hash, the
   address, the MEASURED quantity (the chain delta — never the quote and never
   a "fill" a tool result reported), the basis from `defi_data.positions` (or
   `unknown`), and the reason for the trade.
   `positions` also says where its size came from: `measured from the swap
   receipt`, or `QUOTED size, not measured` — a quoted size is not proof.
6. **Reconcile when anything looked off:** `defi_data.reconcile(chain=…,
   ledger_path=…)`. Its verdict is authoritative over your memory.

## Honest reporting

- Report what was measured. "Bought 0.5 ETH of X" without a measured receipt is
  a claim, not a result.
- A figure you could not read is UNKNOWN — say so; never report it as zero.
- Name the contract address in any public claim about a trade.

## If a tool is missing

These steps need `defi_data` (reads) and `defi_trade` (trades) in THIS session.
If `defi_trade` is not loaded, do the read steps, write the plan, and tell the
owner which grant is missing. If `defi_data` is not loaded either, you cannot
measure anything — say so and stop. Never work around a missing verb with a raw
`call`, a browser dapp, or another agent.

## Related

`trade-execution`, `position-journal`, `exits`.
