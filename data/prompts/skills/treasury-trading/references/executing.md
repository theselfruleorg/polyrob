# Treasury trading — executing a trade

Part of the `treasury-trading` skill.

Always in this order. Never skip a dry run.

**Native in** (`token_in="native"`) needs no allowance:

1. `defi_trade.swap(..., dry_run=true)` — read the guard verdict, the simulated
   outflow, the route sanity line and the gas figure.
2. `defi_trade.swap(..., dry_run=false)` with the same parameters you simulated.

**ERC-20 in** needs an allowance first. `swap` checks the allowance BEFORE it
simulates — even with `dry_run=true` it refuses with "insufficient
allowance" and never reaches the guard. So:

1. `defi_data.swap_quote(...)` — it names the `spender:`.
2. `defi_trade.approve_token(token=…, spender=<that spender>, amount=<exact>,
   max_spend_usd=…, dry_run=true)`, then the same call with `dry_run=false`.
   Never unlimited.
3. `defi_trade.swap(..., dry_run=true)` — now the guard runs; read it.
4. `defi_trade.swap(..., dry_run=false)` with the same parameters.
5. `revoke_approval` immediately after. An allowance that outlives the trade is a
   standing claim on your wallet — and on a fresh memecoin contract, that claim
   is held by code nobody has audited.
Then check the book: `defi_data.positions(chain=…)` must show the entry with
   the MEASURED size (the chain delta, never the quote). Put the thesis in one
   sentence in your run report.

`RESULT: BROADCAST BUT NOT CONFIRMED` is not a failure — it may still land.
**Never retry it blindly**; check the chain first or you pay twice.
