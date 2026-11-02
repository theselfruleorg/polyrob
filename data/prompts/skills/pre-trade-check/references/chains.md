# Which chain — what each one can do

Part of the `pre-trade-check` skill.

`defi_data.portfolio`, `token_info`, `price`, `new_pools` and `trending` read
**every** chain below. `defi_trade` can move value on every chain marked *money*.
To check SOMEONE ELSE's wallet, use `defi_data.wallet_holdings(address=…,
chain=…)`; `portfolio` and `positions` read only this agent's own wallet.

| Chain | Money? | Gas | Notes |
|---|---|---|---|
| **base** | yes | ETH, ~free | Read `portfolio` before you plan a ticket; never a remembered balance. |
| **robinhood** | **yes** | ETH, ~free | Its stablecoin is USDG, not USDC; native ETH routes straight into a token, so ETH is gas AND buying power. |
| **solana** | **yes**, own rail | **SOL** | `defi_trade.solana_swap` is one verb (Jupiter routes it; no approve/revoke cycle), armed by `SOLANA_TRADE_ENABLED`, same guard and caps as the EVM verbs. SOL is gas AND buying power — leave a fee reserve. |
| **ethereum** | yes | ETH, **expensive** | An approve+swap+revoke cycle can cost more than a small trade is worth. |
| **arbitrum** | yes | ETH, ~free | |
| **polygon** | yes | **POL, not ether** | |
| **optimism** | **no — read only** | ETH | Prices, screens, balances and `wallet_holdings` work here. No swap, send or approve can be sent on it, so it is never an entry chain and never an exit chain. |

Solana addresses are base58 and **carry no checksum**, so a typo that still
decodes is a valid, different account; and they are case-SENSITIVE, so never
"tidy" one. `token_info` prints this on every lookup — read it.

Re-check a route with `swap_quote` and report what the quote says, not what you
remember. Which chains a mandate trades on is the owner's choice at the intake.
