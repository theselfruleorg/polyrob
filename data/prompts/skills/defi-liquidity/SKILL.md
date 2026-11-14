---
name: defi-liquidity
description: 'Uniswap v3 liquidity positions with the lp verbs: reading positions, pricing a deposit, adding, removing and collecting, the fee unit, and why a pool that a launchpad made is different.'
license: MIT
metadata:
  polyrob-when: 'Uniswap v3 liquidity: add, remove, collect fees'
  polyrob-priority: '2'
  polyrob-triggers: '{"tool_ids":["defi_trade","defi_data"]}'
  polyrob-version: '2'
---
# DeFi Liquidity — Uniswap v3 positions

## Liquidity

Use `defi_data.lp_positions`, `lp_pool_info`, and `lp_quote` to read Uniswap v3
positions and price a deposit. `defi_trade.lp_add` creates/initializes and mints,
or increases a held token_id; `lp_remove` decreases and collects (optional burn
at 100%); `lp_collect` collects both tokens without reducing liquidity
(`protocol='v4'` too; with `account=`/`nft=` for a v4 position an NFT's account
owns: the fees land in that account). Writes
require `DEFI_LIQUIDITY_ENABLED=true`, default to dry_run, and pass the shared
money guard. Run each exact token approval separately when the verb names it.
The owner's chat verb is `/lp`; `propose_action` cannot carry it (it is not a
card verb), so to put a liquidity move to the owner, dry-run it yourself and
report the numbers, and name `/lp` as the owner's verb (the command-line rule
is in `trade-execution`). Fee is in millionths:
3000 means 0.3%, not 3000 bps. Amounts are human token units; max_spend_usd bounds
the sum of deposit legs (or gas for remove/collect). Native is accepted on add;
withdrawals return wrapped native. A new pool needs an explicit initial_price
in token_b per token_a. A token still on a launchpad curve requires
fragment=true before creating a separate market.

A launchpad's own liquidity venue is different from a plain pool. When a token
graduates from the curve of the launchpad the `launchpad` tool supports, it gets
a v4 pool whose LP fee is zero. Its hook collects the swap tax and pays creator
escrow; the launch locker position cannot be withdrawn. Adding another position
there provides depth, not LP income: a full-range position sells your token to
buyers as they buy. `lp_pool_info`/`lp_quote` with `protocol='v4'` read that
pool (token_a='native', token_b=the launched token), and
`defi_data.pool_metrics` gives its depth, one-buy impact and code-hash check.
A v4 add is `lp_add(protocol='v4', range='full')` — a NEW position only, in
three steps: the owner's one ERC-20 `approve_token(token, spender=Permit2)`, then
your `approve_token(token, spender=<v4 PositionManager>, amount, via='permit2')`
(exact, expires in 15 min), then the add. `LP_ETH_CAP` (0 by default) must be
set by the owner first. `lp_remove(protocol='v4', token_id=…)` withdraws a
percentage and keeps the position NFT (no burn on v4); it returns NATIVE for a
native leg. Any other v4 pool reads too: `lp_positions(protocol='v4',
token_id=…)` reads one position from the chain (owner, PoolKey, ticks,
liquidity, what it holds now, the fees owed), without `token_id` it counts the
positions on chain, finds their ids and reads each one; a pool read adds the
fee and the last 24 hours of swaps, volume and LP fees; `lp_pool_info`/`lp_quote` take `token_id`,
or the full PoolKey (token_a, token_b, fee, tick_spacing, hooks when it has
one). A v4 pool has no address: its id is the hash of those five fields.

A NEW v4 pool, or a custom range on a pool that is not the launchpad's, is the
OWNER's declaration — never yours alone. Call `lp_add(protocol='v4', token_a,
token_b, amount_a, amount_b, fee, tick_spacing, range='full'|'ticks:lo,hi',
initial_price, hooks if any)`: it sends NOTHING, stores the declaration and
puts one card to the owner (pool id, range, price, both amounts, the value).
`slippage_bps` is the allowed price gap there (max 500). Show the plan with
`lp_add(protocol='v4', seed=<ref>, dry_run=true)`. After the owner confirms
(one use, one hour), make the two approvals the refusal names, then
`lp_add(protocol='v4', seed=<ref>, dry_run=false)`: the pool is created and
the position minted in ONE transaction (an empty pool has no safe price), or a
plain mint when the pool exists within the gap of the confirmed price. The
result reads the position and the pool back; say only what it verified. Never
promise that generic `call` can bypass a missing liquidity shape.

A separate pool for a curve-phase token fragments the market and creates an
arbitrage spread. When you are the sole LP, sellers trade against your capital.
Removing your own liquidity removes holder exit depth and can be perceived as a
rug; decide the intended horizon before depositing. Fee collections are not yet
booked as treasury income: their wallet_spend USD figure is gas. A submitted tx
with unverified receipt must be reconciled before any retry.

## If a tool is missing

Follow `harness-manual`; do the read steps you can, and never work around a
missing verb with a raw `call`, a browser dapp or another agent.

## Related

`token-launch`, `trade-execution`.
