---
name: stable-cash
description: 'What counts as cash: pinned stablecoins only, native over bridged, the depeg thresholds, yield stables are positions, gas is not cash.'
license: MIT
metadata:
  polyrob-when: 'Deciding what counts as cash or a safe stablecoin'
  polyrob-priority: '3'
  polyrob-auto-activate: 'true'
  polyrob-triggers: '{"tool_ids":["defi_trade","defi_data"]}'
  polyrob-version: '3'
---
# Stable Cash — what counts as cash, and when it stops counting

Cash is a PINNED stablecoin at its pinned address. Anything else that calls
itself a dollar is a position.

## What exists today

- The chain registry pins USDC (and the wrapped native) per chain; `token_info`
  says `on the pinned canonical list` for those. Base USDC is
  `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`.
- Other stables (USDG on Robinhood Chain, USDT, DAI) are NOT on the canonical
  list. Treat them as verified only when a trusted source covers them (a
  `target_token` the owner typed, the owner's approval — `token_info` says
  `verified`); otherwise resolve them with `token-identity`. A refused buy has
  already asked the owner in `/pending`; never name a shell command.
- The guard values the chain's pinned USDC at exactly $1.00 — it is the unit the
  caps are denominated in.

## Rules

1. **Native over bridged.** Prefer native USDC to a bridged copy (USDC.e) unless
   a venue requires the bridged one.
2. **Watch the peg before you size.** Read `defi_data.price` of the stable:
   - below $0.995: no new positions priced in it;
   - below $0.98: move out, in small tranches, into the most liquid pinned
     alternative, with a tight `slippage_bps`.
   The peg rule runs BEFORE any "buy the dip" idea.
3. **Yield stables are positions.** A stable that pays yield carries protocol
   and redemption risk; it is not cash.
4. **Keep the fee reserve outside "cash".** The chain's fee reserve (named on
   the portfolio's native row) pays for every exit; the native coin above it is
   money and counts as buying power.
5. **Parking idle cash in lending** (Aave, Morpho) has no dedicated verb yet;
   `defi_trade.call` can reach a protocol, but only on an owner instruction.

## If a tool is missing

These steps need `defi_data` (reads) and `defi_trade` (trades) in THIS session.
If `defi_trade` is not loaded, do the read steps, write the plan, and tell the
owner which grant is missing. If `defi_data` is not loaded either, you cannot
measure anything — say so and stop. Never work around a missing verb with a raw
`call`, a browser dapp, or another agent.

## Related

`token-identity`, `sizing-and-risk`, `treasury-trading`.
