# Treasury trading — NFTs

Part of the `treasury-trading` skill.

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

- **Buy or sell.** There is no marketplace integration (no Seaport, no
  Reservoir) and `defi_trade` swaps speak ERC-20 only. There is **no rail yet**
  from your wallet to an NFT purchase or listing.
- **Grant `setApprovalForAll`.** No verb in the tree can author one, and the
  wallet guard refuses any transaction that emits one. It is a standing claim
  on every token of a collection, present and future — the most common way a
  self-custodial wallet is drained.

If a task asks you to BUY or SELL an NFT, say so plainly and escalate it to the
owner as a missing capability — never improvise a raw contract call to fake it.
Do not describe holding, sending or revoking as impossible: those shipped.
