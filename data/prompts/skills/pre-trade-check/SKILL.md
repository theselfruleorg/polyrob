---
name: pre-trade-check
description: 'Measure before you commit a trade: gas row, swap_quote lines (rate, spender, cross-check, value), price impact by a half-size quote, the exit quote, the token screen, and slippage by asset class. Lists what the swap verb already refuses.'
license: MIT
metadata:
  polyrob-priority: '2'
  polyrob-auto-activate: 'true'
  polyrob-triggers: '{"action_names":["defi_data_swap_quote"],"keywords":["pre-trade","pre trade","before a trade","before the swap","price impact","slippage","swap_quote","quote a swap","exit liquidity","exit route"],"task_patterns":["(check|quote|price|measure).*(before|prior to).*(buy|swap|trade)"],"tool_ids":["defi_trade","defi_data"]}'
  polyrob-version: '3'
---
# Pre-Trade Check — measure before you commit

Run this after `token-identity` and before any `defi_trade.swap` with
`dry_run=false`. Every figure below comes from a verb you can call now; write
each one down, because the post-trade check compares against them.

## What the code already enforces

- `swap` refuses a quote older than 30 s ("the quote is stale").
- `swap` refuses when the route price DISAGREES with an independent price by more
  than `DEFI_ROUTE_DRIFT_MAX_PCT` — a thin or seeded pool extracts value that way.
- `swap` bounds every fill by `slippage_bps` (default `DEFI_MAX_SLIPPAGE_BPS`) and
  the guard ASSERTS the simulated receipt of `token_out` is at least that floor.
- An unverified `token_out` above $5 with a route check that did not AGREE is
  refused, and so is any unpinned Solana buy above $5 (see `token-identity`).

What the code does NOT measure is price impact: there is no price-impact field
on `swap_quote` or `swap` yet. You measure it with a second quote (step 3).

## The procedure

1. **Balances first.** `defi_data.portfolio(chain=…)`. Read the `gas (…)` row:
   a number, `UNKNOWN` (the read failed — not zero), or `EMPTY` (a real zero —
   you cannot pay for the swap or for the exit). Reserve native gas for the
   WHOLE round trip before you size anything: an ERC-20 entry is approve + swap
   + revoke, and so is the exit — up to six transactions. A native-in entry
   saves the entry approve and revoke, never the exit's.
2. **Quote at your size.** `defi_data.swap_quote(chain=…, token_in=…,
   token_out=…, amount_in=…)`. Read four lines:
   - `rate:` and `route:`;
   - `spender:` — this is what `approve_token` must authorise, NOT the token;
   - `cross-check:` — `consistent`, `unavailable`, or `⚠ SUSPECT QUOTE` (then
     re-quote and never use that number as a floor);
   - `value:` — `unknown` means no indexed price with real liquidity. Do not
     substitute a remembered rate.
3. **Measure how fast the price worsens with size.** Quote again at HALF the
   size. The gap between the full-size and the half-size rate is the
   INCREMENTAL deterioration of the second half — a lower bound on your price
   impact, not the total. If even that gap is above your class limit, cut the
   size: stable↔stable 0.1%, majors 0.5%, long tail 2%. If it is close to the
   limit, assume the real impact is about double and cut anyway.
4. **Quote the exit now.** Quote the reverse direction (`token_out` back to the
   quote asset) at the size you would receive. If the exit has no route, or a
   far worse rate, you are buying something you may not be able to sell.
5. **Screen what you buy.** `defi_data.token_info` — honeypot and sell tax, and
   the check count. A `PARTIAL` or `UNSCREENED` screen is not clean. For a fresh
   token also read `token_holders` (concentration) and `ohlcv` (the shape of the
   last day, not a single 24h figure). Both `price` and `ohlcv` are POOL data —
   `price` comes from the deepest priced pool, `ohlcv` names the one pool it
   read. Neither is an independent oracle; a seeded pool can move both.
6. **Set slippage from the asset class**, never unbounded: stables 5–10 bps,
   majors 30–50 bps, long tail 100–300 bps. Pass it as `slippage_bps`.
7. **Decide the ticket** with `sizing-and-risk`, and the exit with `exits`,
   BEFORE you execute. An entry without a declared exit is not ready.

## Refuse the trade yourself when

- any figure you need came back unknown and you would be guessing to fill it;
- the cross-check says `SUSPECT QUOTE` twice;
- the exit quote has no route;
- the gas row is `EMPTY` or `UNKNOWN`.

## If a tool is missing

These steps need `defi_data` (reads) and `defi_trade` (trades) in THIS session.
If `defi_trade` is not loaded, do the read steps, write the plan, and tell the
owner which grant is missing. If `defi_data` is not loaded either, you cannot
measure anything — say so and stop. Never work around a missing verb with a raw
`call`, a browser dapp, or another agent.

## Related

`token-identity`, `trade-execution`, `sizing-and-risk`, `exits`.
