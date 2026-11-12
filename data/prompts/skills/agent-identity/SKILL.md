---
name: agent-identity
description: 'The agent on-chain identity: ERC-8004 register_agent once per chain (permanent, public, not repeatable) and set_agent_uri to update it; the agent NFT account (ERC-6551) the wallet owns: inspect, snapshot, journal, bind, revoke_all, the owner-always acts (mint, withdraw, ready for sale, buy a pinned market listing), unready and cancel a market order, claim what the market owes, the collection reveal, and the /identity and /nft cards.'
license: MIT
metadata:
  polyrob-when: 'Registering the on-chain identity or an agent NFT'
  polyrob-priority: '3'
  polyrob-owner-only: 'true'
  polyrob-triggers: '{"tool_ids":["defi_trade","agent_nft"]}'
  polyrob-version: '1'
---
# Agent identity — one identity, one owned account

Both halves are permanent and public. Read first, write once, and put an act to
the owner as a card when it is the owner's call.

## ERC-8004 — the identity record

- **`defi_trade.register_agent(chain=…, dry_run=true)`** mints this agent's
  identity token on the chain's PINNED Identity Registry and publishes the
  registration file built from the instance (name, description, avatar, the
  services you offer). It costs only the fee (`max_spend_usd` bounds it).
  ⚠️ It is NOT idempotent: a second registration splits the identity. The verb
  reads the chain first and refuses when this wallet already holds its
  identity, or when a registration on that chain is already in flight. Treat
  that refusal as the answer; never look for another route. Register once per
  chain, and only on an owner instruction.
- **`defi_trade.set_agent_uri(chain=…, agent_id=…, dry_run=true)`** updates
  the published file when the endpoints, the description or the avatar change.
  It mints nothing.
- A confirmed registration writes the local identity record from the
  RECEIPT. If the result says `identity record NOT written`, the receipt and the
  simulation disagree: check the tx before you claim an identity.
- Both verbs are off until the operator sets `EIP8004_REGISTER_ENABLED`.
- **The owner's card:** `propose_action(command="/identity register on
  <chain>")` or `"/identity set-uri on <chain> id <agentId>"`.

## The agent NFT account (ERC-6551)

An NFT has its own wallet (a token-bound account), and whoever owns the NFT
controls it. This agent uses an account ONLY by owning the NFT on this wallet;
there are no grants, no bindings and no operator keys. The tool is `agent_nft`
(needs `AGENT_NFT_ENABLED`); its schema lists the chains it serves.

Reads (free):

- `agent_nft_inspect(target="<chain>:<collection>/<token_id>")` — ANY agent
  NFT before buying or taking it: open approvals FIRST with their coverage,
  then the owner, the lock or the sale readiness (by the account kind), the
  state and the identity count. An incomplete
  approval table is never "empty".
- `agent_nft_snapshot(chain=…, nft=…)` — the account this wallet owns: its open
  approvals, owner, state, identity, reveal state, balance and journal head.

Writes (fee only, a dry run first):

- `agent_nft_journal(chain=…, kind="thesis"|"note", text=…, anchor=false)` — a
  signed, chained entry; `anchor=true` also writes the head on-chain.
- `agent_nft_bind_identity(chain=…)` — publishes the agent's card on the
  ACCOUNT's own ERC-8004 identity through the account: `setAgentURI` on the
  identity the account owns (an ERC-6551P NFT has one from its mint), or
  `register()` when it has none. It never makes a second identity; the same
  card already published sends nothing. It is an act through the account, so
  it ends a ready NFT's sale readiness.
  Always the owner's approval.
- `agent_nft_revoke_all(chain=…)` — revokes the open approvals the scan finds
  on the account. Run it before the NFT leaves. An approval the scan does not
  find is not revoked.
- `agent_nft_collection_reveal(chain=…)` — reveals the DUE ids of a pinned
  collection that has the reveal capability; gas only, capped by
  `AGENT_NFT_REVEAL_MAX_GAS_USD`. Nothing due means no transaction.
- `agent_nft_sale_settle(chain=…)` — after the sale is settled (`agent_nft_snapshot`
  shows phase and settled), settles THIS wallet's own auction records: the sale
  returns what each paid above the clearing price. Gas only. No refund means no
  transaction.
- `agent_nft_sale_claim(chain=…)` — takes what the sale owes this wallet
  (`owed`, shown on the snapshot). Gas only. Nothing owed means no transaction;
  an unreadable `owed` is not "nothing owed".
- `agent_nft_sale_unready(chain=…, nft=…)` — ends the sale readiness
  (`cancelReady` from this wallet; the account is not used). Moves nothing. Not
  ready means no transaction.
- `agent_nft_sale_cancel_listing(chain=…, salt=…)` — withdraws this wallet's
  orders on the collection's pinned market: one order by its `salt`, or every
  order when `salt` is left out (`incrementNonce`); then it revokes the market's
  approval of each NFT whose open order it ended (or the named `nft=`).
- `agent_nft_market_claim(chain=…)` — takes what the collection's pinned market
  owes this wallet (an ETH payment it could not make at a fill). Gas only.
  Nothing owed means no transaction.
- You cannot make an offer of your own, list on Seaport or OpenSea, or buy a
  Seaport listing.

Owner-always acts — a live call always waits for the owner:

- `agent_nft_collection_mint(chain=…, qty=…, max_spend_usd=…)` mints to THIS
  wallet only (any other `to` is refused), on a collection the system admin
  pinned with the mint capability and its sale. The quote names the price now.
  In the whitelist phase add `allowance=…` and `proof=[…]` from the published
  whitelist; in the auction the sale returns the excess at its settlement.
- `agent_nft_withdraw_token(chain=…, to=…)` sends an owned agent NFT out (its
  account goes with it). It refuses while the account has an open approval:
  `agent_nft_revoke_all` first. The owner's verb: `/nft send <id> <to> on
  <chain>`.
- `agent_nft_sale_ready(chain=…, nft=…)` makes an ERC-6551P agent NFT READY FOR
  SALE: its own account calls the collection's `readyToSell` (with the journal
  entry, one transaction). From the next block a plain marketplace fill may move
  the NFT and everything its account holds. While it is ready, the guard refuses
  every act through the account that the owner did not ask for, because any act
  through it ends the readiness. An act the owner asked for passes and ends it.
  The owner's verbs: `/nft ready <id> on <chain>` and `/nft unready <id> on
  <chain>`; put it to the owner as a card (`propose_action(command="/nft ready
  <id> on <chain>")`).
- `agent_nft_buy(chain=…, listing=…, max_spend_usd=…)` buys an agent NFT its
  seller listed on the collection's PINNED market (the listing link the seller
  shared; an ETH price only). First quote it (`dry_run=true`): the quote lists
  what the NFT's account holds, what the market checks after the transfer, and
  what it cannot check (worth 0). Then buy with `conditions_hash=…` from the
  quote; it refuses if the account changed since. A seller who empties the
  account before the fill gets nothing and the wallet pays nothing.
- `agent_nft_sale_list(chain=…, nft=…, price=…, currency=…, expiry_days=…)`
  lists an owned agent NFT on its collection's PINNED market: the wallet signs
  ONE market listing (core builds every field and the seller's promise and checks
  them on the chain first); the market first gets the approval of that one NFT.
  Quote it first (`dry_run=true`): the quote shows the price, the expiry and
  what the account holds. The result is the share link. A listing does not stop
  your work, but spending a promised holding below its minimum makes the sale
  fail; `tolerance_bps=50` allows 0.5 %.
- `agent_nft_sale_accept(chain=…, offer=…)` accepts a buyer's signed offer (the
  link the buyer shared): the NFT and everything its account holds leave for the
  offer's price less the royalty.
- There is no "adopt" step. An NFT of a pinned collection that arrives on the wallet
  is yours to act from at once; the tokens its account already holds are recorded
  as inherited positions (no cost basis) by the holdings watch.

To act FROM an owned account, use `account=` or `nft=` on `defi_trade.transfer`,
`defi_trade.swap`, `defi_trade.lp_collect` (v4) or `defi_trade.call` (`send-funds`). A
call from an account grants no allowance and does not address the account's own
collection, sale or sale rails. Every other money verb refuses `account=` with
"this verb does not act from an NFT account". The owner lets the agent use a
collection with `/nft trust <collection> on <chain>`; you never trust one yourself.

The account kind decides what the account can do. The snapshot names the kind:

- Tokenbound AccountV3: the NFT's owner drives it with CALL only (no DELEGATECALL, no admin call); it has a lock and an ERC-2771 forwarder, may hold standing approvals, and accepts the owner's signature as its own (ERC-1271).
- ERC-6551P account: the NFT's owner drives it with CALL only (no DELEGATECALL, no admin call); it has no lock and no forwarder, signs nothing, refuses a standing approval (an ERC-20 spend approves, spends and resets in one batch), and its NFT cannot be sold while an ERC-6551 account owns it.

## Never

- Register twice, or register to look busy. An identity is a public claim
  about the agent.
- Make an agent NFT ready for sale, or act through a ready account, unless the
  owner asked. It leaves by the owner's withdraw, or by a marketplace fill after
  the owner made it ready. Never lock it.
- Report an identity, a mint or a reveal before its `RESULT: … CONFIRMED`
  line (`trade-execution` has every result line).

## If a tool is missing

Follow `harness-manual`. Without `propose_action`, follow the command-line rule
in `trade-execution`.

## Related

`send-funds`, `trade-execution`, `wallet-overview`, `token-identity`.
