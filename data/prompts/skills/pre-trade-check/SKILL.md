---
name: pre-trade-check
description: 'Measure before you commit a trade: gas row, swap_quote lines (rate, spender, cross-check, value), price impact by a half-size quote, the exit quote, the token screen, and slippage by asset class. Lists what the swap verb already refuses.'
license: MIT
metadata:
  polyrob-when: 'Measuring a trade before you send it'
  polyrob-priority: '2'
  polyrob-triggers: '{"tool_ids":["defi_trade","defi_data"]}'
  polyrob-requires: '["token-identity"]'
  polyrob-version: '4'
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

1. **Balances first.** `defi_data.portfolio(chain=…)`. Read the native-coin row (`ETH …`, `SOL …`):
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

What each chain can do (money or read-only, its gas asset): `references/chains.md`.

## Reading a route

Swaps try Uniswap V3 first where it exists, then a DEX aggregator that reaches
the other venues (Aerodrome, the V2 forks, V4 and the rest). A pool being off-V3
is not a reason a token is unbuyable: quote it. `swap_quote` asks the SAME
providers `swap` will, so a clean quote means a reachable trade.

- **A route refusal is still a stop.** Do not improvise a raw contract call.
- **The refusal names one of two facts.** "no route … asked: …" means nothing
  pools it — a real finding. "could not complete the route lookup …
  UNAVAILABLE" means a provider errored or rate-limited; the lookup did not
  finish. Never write "this token is unreachable" from the second one; retry it.
- **NO ROUTE on a held position is that token's fact, not a stop.** A rugged or
  illiquid token returns NO ROUTE on every venue. On a real re-quote, record its
  value as UNKNOWN with the date (not $0 — a failed lookup is not proof it is
  worthless), re-quote it at most once a day, stop escalating it, and keep taking
  new screened entries within the caps. Never pause every entry until each
  position has an exit route.

## Refuse the trade yourself when

- any figure you need came back unknown and you would be guessing to fill it;
- the cross-check says `SUSPECT QUOTE` twice;
- the exit quote has no route;
- the native-coin row is `EMPTY` or `UNKNOWN`.

## If a tool is missing

Follow `harness-manual`; do the read steps you can, and never work around a
missing verb with a raw `call`, a browser dapp or another agent.

## Related

`token-identity`, `trade-execution`, `sizing-and-risk`, `exits`.
