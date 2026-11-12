# NFTs — what the wallet can and cannot do

Part of the `trade-execution` skill.

**What you CAN do** (gated `NFT_TOOLS_ENABLED`):

- `defi_data.nft_holdings` — what an address owns. If no enumeration provider
  is configured it says so; that is not the same as owning nothing.
- `defi_data.nft_info` — owner, standard and metadata URI for one token,
  straight from the chain.
- `defi_trade.nft_transfer` — send one you hold. **Always owner-approved**, with
  no exception: a collectible has no reliable price, so no USD cap can bound
  what you are giving away. The fee is capped; the gift is not.
- `defi_trade.nft_revoke_approval` — retire an operator's blanket approval over
  a collection. This only ever reduces risk.

**What you CANNOT do, and must not improvise:**

- **List, offer, sell, or buy anywhere but a pinned market.** `defi_trade`
  swaps speak ERC-20 only, and no verb signs a Seaport, OpenSea or Reservoir
  order. There is **no rail yet** to list or sell an NFT anywhere but the
  PINNED market of a pinned agent-NFT collection, to make an offer of your own,
  or to fill another marketplace's order. On that pinned market (`agent-identity`,
  always owner-approved, one card each): `agent_nft_buy` buys a listing (the link
  its seller shared, priced in ETH), `agent_nft_sale_list` lists an agent NFT
  this wallet owns (it signs ONE market listing; core builds every field), and
  `agent_nft_sale_accept` accepts a buyer's signed offer. `agent_nft` can also
  make an owned NFT ready for sale, end that, and cancel its orders there.
- **Grant `setApprovalForAll`.** No verb in the tree can author one, and the
  wallet guard refuses any transaction that emits one. It is a standing claim
  on every token of a collection, present and future — the most common way a
  self-custodial wallet is drained.

If a task asks you to SELL an NFT anywhere but its pinned collection's market,
to make an OFFER, or to BUY one anywhere but a pinned collection's market, say so plainly and escalate it to the owner as a missing capability — never improvise a raw contract call to fake it.
Do not describe holding, sending or revoking as impossible: those shipped.
