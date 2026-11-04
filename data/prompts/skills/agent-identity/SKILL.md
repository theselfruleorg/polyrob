---
name: agent-identity
description: 'The agent on-chain identity: ERC-8004 register_agent once per chain (permanent, public, not repeatable) and set_agent_uri to update it; the agent NFT account (ERC-6551) the wallet owns: inspect, snapshot, journal, bind, revoke_all, the owner-always acts (mint, withdraw, adopt), the collection reveal, and the /identity and /nft cards.'
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
  then the owner, the lock, the state and the identity count. An incomplete
  approval table is never "empty".
- `agent_nft_snapshot(chain=…, nft=…)` — the account this wallet owns: its open
  approvals, owner, state, identity, reveal state, balance and journal head.

Writes (fee only, a dry run first):

- `agent_nft_journal(chain=…, kind="thesis"|"note", text=…, anchor=false)` — a
  signed, chained entry; `anchor=true` also writes the head on-chain.
- `agent_nft_bind_identity(chain=…)` — registers the ACCOUNT's own ERC-8004
  identity through the account; it refuses when the account already has one.
  Always the owner's approval.
- `agent_nft_revoke_all(chain=…)` — revokes the open approvals the scan finds
  on the account. Run it before the NFT leaves. An approval the scan does not
  find is not revoked.
- `agent_nft_collection_reveal(chain=…)` — reveals the DUE ids of a pinned
  collection that has the reveal capability; gas only, capped by
  `AGENT_NFT_REVEAL_MAX_GAS_USD`. Nothing due means no transaction.

Owner-always acts — a live call always waits for the owner:

- `agent_nft_collection_mint(chain=…, qty=…, max_spend_usd=…)` mints to THIS
  wallet only (any other `to` is refused), on a collection the system admin
  pinned with the mint capability.
- `agent_nft_withdraw_token(chain=…, to=…)` sends an owned agent NFT out (its
  account goes with it). It refuses while the account has an open approval:
  `agent_nft_revoke_all` first. The owner's verb: `/nft send <id> <to> on
  <chain>`.
- `agent_nft_adopt(chain=…)` reads an owned NFT's history and writes a signed
  handover entry. Owner turns only.

To act FROM an owned account (send or swap its assets), use `account=` or
`nft=` on `defi_trade.transfer` or `defi_trade.swap` (`send-funds`). The owner
lets the agent use a collection with `/nft trust <collection> on <chain>`; you
never trust one yourself.

## Never

- Register twice, or register to look busy. An identity is a public claim
  about the agent.
- Sell or lock an agent NFT. It leaves only by the owner's withdraw.
- Report an identity, a mint or a reveal before its `RESULT: … CONFIRMED`
  line (`trade-execution` has every result line).

## If a tool is missing

Follow `harness-manual`. Without `propose_action`, follow the command-line rule
in `trade-execution`.

## Related

`send-funds`, `trade-execution`, `wallet-overview`, `token-identity`.
