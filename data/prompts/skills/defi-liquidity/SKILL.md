---
name: defi-liquidity
description: 'Uniswap v3 liquidity positions with the lp verbs: reading positions, pricing a deposit, adding, removing and collecting, the fee unit, and why a Pons graduation pool is different.'
license: MIT
metadata:
  polyrob-priority: '2'
  polyrob-auto-activate: 'true'
  polyrob-triggers: '{"action_names":["defi_trade_lp_add","defi_trade_lp_remove","defi_trade_lp_collect","defi_data_lp_positions","defi_data_lp_pool_info","defi_data_lp_quote"],"keywords":["liquidity pool","lp position","provide liquidity","add liquidity","remove liquidity","collect lp fees","uniswap v3 position"],"task_patterns":["\\b(add|remove|provide|withdraw)\\b.*\\bliquidity\\b"],"tool_ids":["defi_trade","defi_data"]}'
  polyrob-version: '2'
---
# DeFi Liquidity — Uniswap v3 positions

Part of the `treasury-trading` playbook, split out.

## Liquidity

Use `defi_data.lp_positions`, `lp_pool_info`, and `lp_quote` to read Uniswap v3
positions and price a deposit. `defi_trade.lp_add` creates/initializes and mints,
or increases a held token_id; `lp_remove` decreases and collects (optional burn
at 100%); `lp_collect` collects both tokens without reducing liquidity. Writes
require `DEFI_LIQUIDITY_ENABLED=true`, default to dry_run, and pass the shared
money guard. Run each exact token approval separately when the verb names it.
The owner's chat verb is `/lp`; `propose_action` cannot carry it (it is not a
card verb), so to put a liquidity move to the owner, dry-run it yourself and
report the numbers, and name `/lp` as his verb. Never name a `polyrob …` shell
command on a chat seat (in a terminal REPL, `polyrob wallet lp` is the same
rail — label it as a terminal command). Fee is in millionths:
3000 means 0.3%, not 3000 bps. Amounts are human token units; max_spend_usd bounds
the sum of deposit legs (or gas for remove/collect). Native is accepted on add;
withdrawals return wrapped native. A new pool needs an explicit initial_price
in token_b per token_a. A token still on a Pons curve requires fragment=true
before creating a separate market.

Pons graduation creates a v4 pool whose LP fee is zero. Its hook collects the
swap tax and pays creator escrow; the launch locker position cannot be withdrawn.
Adding another position there provides depth, not LP income: a full-range
position sells your token to buyers as they buy. `lp_pool_info`/`lp_quote` with
`protocol='v4'` read that pool (token_a='native', token_b=the Pons token), and
`defi_data.pool_metrics` gives its depth, one-buy impact and code-hash check.
A v4 add is `lp_add(protocol='v4', range='full')` — a NEW position only, in
three steps: the owner's one ERC-20 `approve_token(token, spender=Permit2)`, then
your `approve_token(token, spender=<v4 PositionManager>, amount, via='permit2')`
(exact, expires in 15 min), then the add. `LP_ETH_CAP` (0 by default) must be
set by the owner first. v4 remove and collect are not built. Never promise that
generic `call` can bypass a missing liquidity shape.

A separate pool for a curve-phase token fragments the market and creates an
arbitrage spread. When you are the sole LP, sellers trade against your capital.
Removing your own liquidity removes holder exit depth and can be perceived as a
rug; decide the intended horizon before depositing. Fee collections are not yet
booked as treasury income: their wallet_spend USD figure is gas. A submitted tx
with unverified receipt must be reconciled before any retry.

## If a tool is missing

The trading verbs need `defi_trade` (and `defi_data` for reads) in THIS session.
If they are not loaded, do the read steps you can, write the plan to the ledger or
watchlist, and tell the owner which grant is missing. Never work around a missing
verb with a raw `call`, a browser dapp, or another agent.

## Related

`treasury-trading`, `trade-execution`.
