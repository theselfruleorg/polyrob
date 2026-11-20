---
name: defi-liquidity
description: 'Uniswap v3 and v4 liquidity positions with the lp verbs: reading positions and pools, pricing a deposit, adding, removing and collecting, declaring a v4 pool or range, and judging a position — the loss against holding, size against volume, when to shrink, how to move a range.'
license: MIT
metadata:
  polyrob-when: 'Uniswap v3/v4 liquidity: add, remove, judge, move a range'
  polyrob-priority: '2'
  polyrob-triggers: '{"tool_ids":["defi_trade","defi_data"]}'
  polyrob-version: '5'
---
# DeFi Liquidity — Uniswap v3 and v4 positions

## Judging a position

Before you size a deposit, answer whether a pool is too large or too small, report a
position's result, or propose to shrink or move one, read
`references/managing-a-position.md`: what a position does when the price moves, the result
against holding, the three size ratios, when to propose a smaller position, range choice
and the steps of a move. A position that is most of its pool pays for the net selling:
watch its native leg, not only its USD value. Watch it with a job that WAKES you
(a `precheck`, §7 of the reference), never an alert-only read job. You keep it in shape
yourself: collect, re-range to a band around the price, and put the plan on one card (§4b).

## Liquidity

Use `defi_data.lp_positions`, `lp_pool_info`, and `lp_quote` to read Uniswap v3
positions and price a deposit. `defi_trade.lp_add` creates/initializes and mints,
or increases a held token_id; `lp_remove` decreases and collects (optional burn
at 100%); `lp_collect` collects both tokens without reducing liquidity
(`protocol='v4'` too; with `account=`/`nft=` for a v4 position an NFT's account
owns: the fees land in that account). Writes
require `DEFI_LIQUIDITY_ENABLED=true`, default to dry_run, and pass the shared
money guard. Run each exact token approval separately when the verb names it.
The owner's chat verb is `/lp`. To put a withdrawal or a fee collection to the
owner, use `propose_action` with `/lp remove [v4] <id> pct <n>` or `/lp collect
[v4] <id>`: the owner sees the dry run and confirms on one card. A deposit, a new
pool or a move is never a `propose_action`: those are declarations (below). Fee is in millionths:
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
A v4 add is `lp_add(protocol='v4', range='full')` for a NEW position, or with
`token_id=<your position>` to add to a position you hold in its own range. Both
take three steps: the owner's one ERC-20 `approve_token(token, spender=Permit2)`, then
your `approve_token(token, spender=<v4 PositionManager>, amount, via='permit2')`
(exact, expires in 15 min), then the add. `LP_ETH_CAP` (0 by default) must be
set by the owner first; it is the net deposit, so a booked withdrawal makes room. `lp_remove(protocol='v4', token_id=…)` withdraws a
percentage and keeps the position NFT (no burn on v4); it returns NATIVE for a
native leg. Any other v4 pool reads too: `lp_positions(protocol='v4',
token_id=…)` reads one position from the chain (owner, PoolKey, ticks,
liquidity, what it holds now, the fees owed, what was deposited and withdrawn,
and the result against holding the deposit), without `token_id` it counts the
positions on chain, finds their ids and reads each one; a pool read adds the
fee, the depth (what moves the price 2% and 10%), the last 24 hours of swaps,
volume and LP fees, and the positions that hold its liquidity; `lp_pool_info`/`lp_quote` take `token_id`,
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

To MOVE a v4 position to a new range, use `lp_add(protocol='v4', move_from=<id>,
move_pct, range)`: the same kind of card, then `seed=<ref>` withdraws that part and
mints the new range from it (two transactions, nothing swapped). The steps and what
to do when the second one refuses are in `references/managing-a-position.md` §6.

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
