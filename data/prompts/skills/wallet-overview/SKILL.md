---
name: wallet-overview
description: 'Report what the wallet holds and where money stands: portfolio one chain per read, UNKNOWN versus EMPTY, the fee reserve, the book versus the wallet, any other wallet or token (wallet_holdings, wallet_activity, token_origin, contract_read), NFTs, the x402 balance and the accounting ledger, never summed.'
license: MIT
metadata:
  polyrob-when: 'Reporting balances, holdings or treasury value'
  polyrob-priority: '3'
  polyrob-owner-only: 'true'
  polyrob-triggers: '{"tool_ids":["defi_data","x402_invoice"]}'
  polyrob-version: '1'
---
# Wallet overview — what is held, read from a verb

Every figure you report comes from a verb you called in this turn, never from
memory, a note or your own arithmetic. A figure you could not read is UNKNOWN.

## Your own wallet

- **`defi_data.portfolio(chain=…)`** reads ONE chain. Ask per chain, and name
  the chains a total covers ("Base and Solana; Ethereum not read"). Add
  `account=…` or `nft=…` to read the account of an NFT this wallet owns.
- The native row is a number, `UNKNOWN` (the read failed — not zero) or
  `EMPTY` (a real zero). The row names the chain's fee reserve: the native coin
  above it is money, the reserve pays for every exit (`stable-cash`).
- Tokens in the *unvalued* block have no confident price. Some were sent to you
  (dust or bait); some can be your own positions. Check each against `positions`
  before you call it not a holding ("Tokens you did not buy" in `trade-execution`).
- **The book is not the wallet.** `defi_data.positions(chain=…)` reads the
  position book (size, basis, P&L, computed by the verb); `portfolio` reads the
  chain. When they could disagree, `defi_data.reconcile(chain=…)` gives the
  verdict (`position-journal`).
- **NFTs:** `defi_data.nft_holdings(chain=…)`. "No enumeration provider" means
  "I could not look", never "you own nothing". One NFT:
  `defi_data.nft_info(chain=…, contract=…, token_id=…)`.

## Someone else's wallet or token

- **Holdings:** `defi_data.wallet_holdings(address=…, chain=…)` (`chain` is
  required for a 0x address; a base58 address reads Solana).
- **Recent activity:** `defi_data.wallet_activity(address=…, chain=…,
  limit=…)` — at most 25 transactions, newest first. Its net flow is over the
  rows read, not a P&L. For an address that is not ours it also lists OUR
  recorded payments to it: read it before you call a payment unverified.
- **Where a token came from:** `defi_data.token_origin(address=…, chain=…)` —
  the deployer, the creation transaction, a launchpad when the chain shows one,
  and the deployer's other contracts. It names addresses, never people.
- **A raw read:** `defi_data.contract_read(chain=…, address=…, signature=…,
  args=[])` returns raw hex; any decoding is unverified (`contract-call`).

## The other ledgers — never summed

- **x402 balance** (only when `x402_pay` is loaded):
  `x402_pay.x402_wallet_status()` shows the address, the Base USDC that pays
  x402 challenges, the gas and the caps. USDC on another chain
  cannot pay a Base challenge.
- **Accounting:** `x402_invoice.accounting(days=…)` — treasury (income, spend,
  net, pending: the agent's own money) and runtime cost (what the owner pays to
  run you). Report the two apart; never add them.
- Each chain, each wallet and each venue keeps its own rows. A treasury figure
  names what it includes and what it could not read.

## The owner's own reads

`/book` (the book against every money chain), `/wallet` (addresses and caps),
`/check <address|ticker>` (any wallet or token), `/invoices` (what is billed).
Name the verb instead of a long recap when the owner asks for the same view.

## If a tool is missing

Follow `harness-manual`. Without `defi_data` you cannot read a balance: say so,
and never state a balance you did not read.

## Related

`position-journal`, `stable-cash`, `send-funds`, `x402-pay`, `token-identity`.
