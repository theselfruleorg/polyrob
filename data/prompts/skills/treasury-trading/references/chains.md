# Treasury trading — which chain to hunt on

Part of the `treasury-trading` skill.

`defi_data.portfolio`, `token_info`, `price`, `new_pools` and `trending` read
**every** chain below. `defi_trade` can move value on every chain marked *money*.
To check SOMEONE ELSE's wallet, use `defi_data.wallet_holdings(address=…,
chain=…)`; `portfolio` and `positions` read only this agent's own wallet.

| Chain | Money? | Gas | What it is for |
|---|---|---|---|
| **base** | yes | ETH, ~free | The x402 rail lives here, and the dust does. Do not plan a Base ticket off a remembered balance; read `portfolio` first. |
| **robinhood** | **yes** | ETH, ~free | **Your primary hunting ground.** Read the `robinhood-chain` skill — it is a different market from the others. |
| **solana** | **yes**, own rail | **SOL** | Fast fresh-launch flow. `defi_trade.solana_swap` is one verb (Jupiter routes it; no approve/revoke cycle, because there is no standing delegate to grant), armed by `SOLANA_TRADE_ENABLED`, same simulate-and-assert guard and caps as the EVM verbs. **SOL is both your gas and your buying power here**, so a Solana ticket spends the same asset that pays to exit it — leave a fee reserve. |
| **ethereum** | yes | ETH, **expensive** | Depth, and tokens that exist nowhere else. An approve+swap+revoke cycle can cost more than a small trade is worth. |
| **arbitrum** | yes | ETH, ~free | Thin fresh-launch flow. Not a hunting ground; fine if a specific token is there. |
| **polygon** | yes | **POL, not ether** | **Not a hunting ground.** Kept as an integration (Polymarket settles here), not as a place to look for launches. |
| **optimism** | **no — read only** | ETH | Prices, screens, balances and `wallet_holdings` work here. No swap, send or approve can be sent on it, so it is never an entry chain and never an exit chain. |

**Where to actually hunt.** Spend your scan budget on **base**, **solana** and
**robinhood** — that is where the minutes-old flow is. Do not burn steps
sweeping arbitrum and polygon for launches — their newest pools are hours old
while base and solana are minutes old. Look there only
when chasing a specific named token.

Solana has its own signing rail. Robinhood's stablecoin is **USDG**, not USDC,
and it routes; **native ETH routes straight into a memecoin** there, so ETH on
that chain is gas AND buying power at once. Re-check a route with `swap_quote`,
and report what the quote says rather than what you remember.

Two things about Solana addresses, because the Ethereum habit is wrong there:
they are base58 and **carry no checksum**, so a typo that still decodes is a
valid, different account and nothing will flag it; and they are case-SENSITIVE,
so never "tidy" one. `token_info` prints this on every lookup — read it.

Pick per opportunity, not per habit. The screens and floors in `memecoin-scouting` are identical
on every chain.
