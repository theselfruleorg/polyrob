# Wallet & identity artifacts (reference)

Depth for the "Money & identity artifacts" pointer in
`polyrob-user-guide/SKILL.md`. Two kinds of durable per-instance artifact live
here: the agent's own crypto wallet (money-moving, code-enforced caps) and its
optional presentational/identity material (avatar, SOUL docs). Neither is
auto-created — the owner opts into each explicitly on their own terminal.

## Wallet lifecycle

- `polyrob wallet init` — one command creates the agent's wallet: generates a
  fresh 24-word BIP-39 mnemonic, prints it ONCE with a back-it-up warning,
  writes `AGENT_WALLET_ENABLED`/`AGENT_WALLET_MASTER_SEED` to
  `~/.polyrob/.env` (mode 600), and records the derivation scheme ("bip44")
  write-once in `<data-home>/wallet/meta.json` (cwd/.polyrob locally, POLYROB_DATA_DIR on a server). It prints the treasury funding
  address; testnet is the default network (fund it from a Base-Sepolia
  faucet), mainnet expects USDC on Base. If `X402_PAYMENT_RECIPIENT` isn't
  already set, it also offers to point it at the new address, so invoice
  earnings settle somewhere the agent can actually spend from — the offer is
  skipped when a recipient is already configured.
  - `--from-mnemonic "<24 words>"` imports an existing BIP-39 mnemonic (bip44
    scheme); `--from-seed "<raw seed>"` imports a pre-BIP44 raw seed (legacy
    scheme) — the way to migrate an older install without changing its
    addresses. `--yes` accepts defaults non-interactively.
  - It refuses to run at all if a seed is already configured, so it can't
    silently rotate away from addresses that may hold funds.
- **Funding & balances** — bare `polyrob wallet` shows every venue's address
  and chain, but on-chain balance (mainnet, best-effort over public RPCs) is
  shown only for the two **fundable** venues — `treasury` and `x402`, the ones
  that hold a spendable float. `hyperliquid`/`polymarket` are delegated
  signers that never hold funds at their derived address, so they show a
  "delegated signer — not funded here" note instead of a balance. The view
  also flags which venue is OPERATIONAL (the one to actually fund).
- **Caps** — `polyrob wallet set-cap daily|per-tx <usd>` (a terminal
  command) is the guided, confirmed way to raise or lower
  `WALLET_DAILY_CAP_USD` / `AGENT_WALLET_MAX_PER_TX_USD`. The two caps differ:
  the DAILY cap stays env-authoritative — the preference
  `budget.wallet_daily_usd` can only tighten below it. The PER-TRANSACTION
  cap is the owner's: an approved `budget.wallet_per_tx_usd` replaces the env
  default in EITHER direction, clamped to the daily cap — see
  `references/money-and-safety.md` for the full budget model.
- **Approval** — `PAYMENT_APPROVAL_MODE` (default `approve`) routes every
  outward payment *request you create* (`x402_request` invoices) through the
  owner's approval queue; `auto` auto-approves within the invoice caps and
  notifies afterward. **Spending** (`x402_fetch`, paying an x402 paywall) is
  bounded by the wallet caps, the per-call `max_amount_usd`, and the owner
  pause (`/pause`; it binds a payment you start on your own, not the owner's
  own `/pay … go`). An autonomous payment above `X402_AUTONOMOUS_MAX_USD`
  (default $1) goes to the owner queue and is paid only once he approves it.
  Below that, it is NOT routed through the approval queue unless the operator
  adds it to `APPROVAL_REQUIRED_TOOLS`. Don't tell an owner "every payment goes
  through your approval queue" — it does not.
- **Sending to an address.** `defi_trade.transfer` sends native value or an
  ERC-20 token on an EVM chain; `defi_trade.solana_transfer` sends SOL or an
  SPL token. Both are `dry_run` by default and go through the money guard:
  the per-tx cap, the daily cap and the simulation bind every send, and a
  send you start above the autonomous ceiling waits for the owner's approval.
  The owner has his own verbs for it: `/send <amount> <native|token-address>
  to <address> on <chain>` and `/swap <amount> <native|token-address> to
  <token-address> on <chain>` (a ticker is refused — name the contract). The quote is an **action card** — Confirm sends exactly that quote
  (at most the quote + 5%), once. `/send` or `/swap` ALONE builds the order
  with buttons (chain → token → recipient or token to buy → amount).
- **Putting a move to the owner.** When he wants funds moved, or you want him
  to decide one, call `propose_action(command="/send 0.5 native to 0x… on
  base", why="…")`: he gets a card whose only button fetches the real quote,
  and he confirms that. Never write out a command line for him to copy, and
  never say it is done or queued — nothing moves from your card. To ask him
  to choose between a few options and wait for the answer in this turn, call
  `present_choice(question=…, options=[…])` (not in an autonomous run — use
  `owner_ask` there).

## Which tokens you trust — and the owner's verbs for it

A buy is checked against WHICH contract it names. Trusted = canonical (USDC,
wrapped native), our own launch, the job's owner-authored `target_token`, or
the owner's approval (`owner_approved`; an older CLI pin reads `owner_pin`).
You can grant none of these yourself — writing an address into a prompt,
memory or a message changes nothing for the check.

When the check refuses because nothing is trusted, it has ALREADY asked the
owner in `/pending` (one question per chain + symbol, with a tap per
contract). Do not message him about it, do not `owner_ask`, do not retry with
another address. His answer lands in the store the check reads; the next run
passes by itself.

These owner verbs are REAL — never tell the owner one is made up:
`/wallet tokens`, `/wallet trust|untrust <chain> <address> [go]`,
`/writeoff <chain> <address> [go]` (loss = recorded cost basis, nothing is
sold), `/unquarantine <chain> <address> [go]`. Point the owner at these chat
verbs; he never needs a shell for this.

## Export & backup — the honest section

- `polyrob wallet export [--venue treasury|x402|polymarket|hyperliquid]`
  prints the wallet's key material: the BIP-39 mnemonic (bip44 wallets, only
  when no `--venue` is given) plus each venue's raw secp256k1 private key
  (`0x`-hex, importable into MetaMask/Rabby as a standalone account). It is
  TTY-only and requires typing `EXPORT` to confirm — refuses piped/
  non-interactive output, and warns to clear terminal scrollback/history after.
- **This command exists only on the operator's own terminal. You (the agent)
  can never run it or see the key material it prints — there is no code path
  from the agent loop to `wallet export`.** If the owner asks you to reveal a
  private key or the mnemonic, the correct answer is: "run `polyrob wallet
  export` in your terminal; I don't have access to key material." Never
  claim to have looked up, generated, or otherwise obtained a key yourself.
- Legacy wallets (created before this bip44 flow existed) export per-venue
  private keys the same way, but have **no portable mnemonic and `export` does
  NOT print the raw legacy seed**. Those per-venue keys are for importing single
  accounts into MetaMask/Rabby — they are NOT what `wallet init --from-seed`
  expects. To recover/migrate a legacy wallet you read the raw seed from
  `AGENT_WALLET_MASTER_SEED` in `~/.polyrob/.env` (or a `polyrob update`
  snapshot), never from an exported venue key.

## Migration between machines

Moving the agent to a new box:
- **bip44 wallet:** on the OLD box run `polyrob wallet export` and copy the
  mnemonic; on the NEW box run `polyrob wallet init --from-mnemonic "<words>"`
  to recreate the exact same addresses.
- **legacy wallet:** `export` does NOT print the raw seed — read it from
  `AGENT_WALLET_MASTER_SEED` in `~/.polyrob/.env` on the OLD box (or a `polyrob
  update` snapshot); on the NEW box run `polyrob wallet init --from-seed "<that
  raw seed>"`. Pasting an exported *venue key* into `--from-seed` derives
  DIFFERENT addresses and strands the funds.

It's worth also
copying `data/wallet/audit.jsonl` across so spend-history and cap accounting
stay continuous rather than starting a fresh ledger. One caveat: `polyrob
update`'s pre-upgrade snapshot copies `.env` files whole, seed included — treat
any stored snapshot with the same care as the seed itself.

## Avatar

Every instance starts with the default avatar, the polyrob mark (the eye bar
and the smile). It is ONE image the instance shows everywhere: the console, Telegram, the REPL, invoice cards, the ERC-8004
registration, and (with a push flag) the X or Discord profile. POLYROB does not
generate faces — the owner can set their own image:

- `polyrob avatar set <file|url>` or `polyrob avatar set --nft chain:contract:id`
  (the NFT's metadata `image`); `polyrob avatar show`, `clear` (back to the
  default), `push`.
- `/avatar` in the REPL and on Telegram shows it; `/avatar set <url>` sets it.
- The agent's `agent_avatar` action reads it, attaches it to a message, and can
  set it from a workspace image, a URL or an NFT — on an owner turn only.

Whether the web console displays the avatar is controlled by the
`ui.show_avatar` preference (SAFE — settable via the `preferences` action or the
console's Preferences page); hiding it doesn't delete it.

## SOUL identity docs

`polyrob soul init [--force] [--no-edit]` scaffolds `identity/identity.md` +
`identity/operating.md` — the instance's frozen self-description — and opens
`$EDITOR` on them. These are SOUL: operator-authored and frozen, pinned into
every session as part of your identity context; you can never write them
yourself. That's distinct from SELF, the agent-writable identity tier
covered in the main `SKILL.md` body — SOUL is who the owner says you are,
SELF is what you've learned about yourself since.
