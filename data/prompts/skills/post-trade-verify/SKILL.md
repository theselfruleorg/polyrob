---
name: post-trade-verify
description: 'Verify a trade after it confirms: receipt, the balance before and after (the only measurement), the bought token at the intended address, the measured amount beside the quote, leftover allowances, the simulated book size, and reconcile. A tx hash is not proof.'
license: MIT
metadata:
  polyrob-when: 'Checking a trade after it confirms'
  polyrob-priority: '2'
  polyrob-triggers: '{"tool_ids":["defi_trade","defi_data"]}'
  polyrob-version: '5'
---
# Post-Trade Verify — a tx hash is not proof

Run this after every `RESULT: CONFIRMED`. A trade is finished when you have
MEASURED what arrived, not when the transaction was sent.

## What the code gives you

**EVM `swap` and Solana `solana_swap`:** the result names the tx hash (EVM: the
block; Solana: `sig:`), the quoted output and the minimum. A CONFIRMED swap
writes the position book from the guard's SIMULATED balance change, never from
the token's own Transfer events: `positions` labels that size `SIMULATED balance
change, final fill not measured`. A swap between working-capital assets (USDC ↔
the wrapped native) writes no position. A reverted or unconfirmed swap writes
nothing. No tool reports a measured fill: the only measurement is the
`portfolio` balance before and after (step 2).

Never substitute the QUOTE for a measurement you do not have. Unmeasured is
unknown.

## The procedure

1. **Receipt.** `CONFIRMED` (a block number on EVM, a `sig:` on Solana).
   `BROADCAST BUT NOT CONFIRMED` is `pending`, never failed — check again before
   you conclude anything.
2. **Balance delta.** `defi_data.portfolio(chain=…)` before AND after the
   trade — on every chain this delta is the only measurement you have. The
   token you bought must appear at the ADDRESS you intended (read the address,
   not the symbol — see `token-identity`). If a collision warning appears, stop.
3. **Fill vs quote.** Put the measured amount that arrived beside the quoted
   output and the minimum, as the tools printed them, with the gas you paid. A
   measured amount below the minimum is a defect to report. Do not compute a
   fill price or a USD figure yourself: the rail book's basis for the position
   is in `defi_data.positions`, and that is the cost you report.
4. **Leftovers.** An allowance you granted for a one-off trade: revoke it.
   Dust left in `token_in`: record it; do not chase it.
5. **Check the book** (see `position-journal`): `defi_data.positions(chain=…)`
   shows the position at the address with its size and basis (or `unknown`).
   It also says where its size came from: `SIMULATED balance change, final
   fill not measured` (every swap now), `QUOTED size, not measured`, or `legacy
   Transfer events, unverified quantity` (older rows). None of these is a
   measurement. Report the tx
   hash, the MEASURED quantity (the chain delta — never the quote and never a
   "fill" a tool result reported), and the reason for the trade.
6. **Reconcile when anything looked off:** `defi_data.reconcile(chain=…)`. Its
   verdict is authoritative over your memory.

## Honest reporting

- Report what was measured. "Bought 0.5 ETH of X" without a measured receipt is
  a claim, not a result.
- A figure you could not read is UNKNOWN — say so; never report it as zero.
- Name the contract address in any public claim about a trade.

## If a tool is missing

Follow `harness-manual`; do the read steps you can, and never work around a
missing verb with a raw `call`, a browser dapp or another agent.

## Related

`trade-execution`, `position-journal`, `exits`.
