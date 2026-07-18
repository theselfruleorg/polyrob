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
- **Caps** — `polyrob wallet set-cap daily|per-tx <usd>` is the guided,
  confirmed way to raise or lower `WALLET_DAILY_CAP_USD` /
  `AGENT_WALLET_MAX_PER_TX_USD`. These stay env-authoritative: a preference
  can only tighten below them, never raise them — see
  `references/money-and-safety.md` for the full budget model.
- **Approval** — `PAYMENT_APPROVAL_MODE` (default `approve`) routes every
  outward payment *request you create* (`x402_request` invoices) through the
  owner's approval queue; `auto` auto-approves within the invoice caps and
  notifies afterward. **Spending** (`x402_fetch`, paying an x402 paywall) is
  bounded by the wallet caps, the per-call `max_amount_usd`, and the owner
  kill-switch — it is NOT routed through the approval queue unless the operator
  adds it to `APPROVAL_REQUIRED_TOOLS`. Don't tell an owner "every payment goes
  through your approval queue" — only invoice creation does.
- **You have no arbitrary-transfer verb.** You can pay x402 paywalls
  (`x402_fetch`, cap-bounded) and create invoices (`x402_request`,
  approval-gated) — you can never send funds to an arbitrary address. If asked
  to "send X to Y", say so plainly and offer to invoice them, or point the
  owner at their own wallet.

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

## Avatar (Mindprint)

An avatar is entirely optional and never auto-created — the instance is
faceless until the owner asks for one. In chat, `/pfp` (alias `/avatar`) with
`status | generate [force] | show` covers the common cases; on the CLI,
`polyrob pfp generate|show|studio|pick|push` covers the same plus a browser
tuning studio and pushing the image out to connected surfaces (each push
target behind its own flag). The face is deterministic — derived from a
name/seed rather than re-rolled randomly, so re-generating without `force` is
a no-op. Whether the web console's identity page actually displays the
avatar is controlled by the `ui.show_avatar` preference (SAFE — settable via
the `preferences` action or the console's Preferences page); hiding it
doesn't delete it.

## SOUL identity docs

`polyrob soul init [--force] [--no-edit]` scaffolds `identity/identity.md` +
`identity/operating.md` — the instance's frozen self-description — and opens
`$EDITOR` on them. These are SOUL: operator-authored and frozen, pinned into
every session as part of your identity context; you can never write them
yourself. That's distinct from SELF, the agent-writable identity tier
covered in the main `SKILL.md` body — SOUL is who the owner says you are,
SELF is what you've learned about yourself since.
