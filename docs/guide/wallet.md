# The agent wallet

This page covers the agent's own wallet: custody, the signer, the spend caps and the
audit, creating and moving the wallet, paying for resources with x402, and signing in
to a site with `wallet_sign`. Getting paid, credits, the ledger and the arming order are
in [payments.md](payments.md); on-chain trading and transfers are in
[onchain.md](onchain.md).

> ⚠️ **Unaudited — use at your own risk.** The wallet handles real value on mainnet and
> has had no independent security audit (see
> [SECURITY.md](../../SECURITY.md#crypto--wallet--payment-features)). It is OFF by
> default. Evaluate on a testnet first.

---

## The wallet (`core/wallet/`)

The wallet is the agent's on-chain identity and treasury. **Off by default**
(`AGENT_WALLET_ENABLED=false`).

- **Custody — hub-and-spoke** (`core/wallet/agent_wallet.py`): one master seed
  (`AGENT_WALLET_MASTER_SEED`, required ≥32 chars when enabled) derives a treasury key
  plus domain-separated per-venue keys via PBKDF2-HMAC-SHA256. Backend
  `AGENT_WALLET_BACKEND=local_eoa` (a local `eth_account` EOA; the raw key never crosses
  a tool boundary or a log). Venues: `{treasury, x402, polymarket, hyperliquid}`; only
  `{treasury, x402}` can hold a spendable float. The **operational venue**
  (`AGENT_WALLET_OPERATIONAL_VENUE`, default `treasury`) is the key that same-chain
  spends sign with, so the "fund me" address the owner sees equals the address that
  spends.
- **Seed split — public-only processes** (`core/wallet/public_identity.py`): keep
  `AGENT_WALLET_MASTER_SEED` in a `/etc/polyrob/wallet.env` that ONLY the agent unit loads.
  The seeded process publishes its addresses to `<data_home>/wallet/public_identity.json`
  (public, `0644`); a unit with `AGENT_WALLET_ENABLED=true` and no seed (console, email)
  reads it and gets a wallet that answers every address and refuses every signature with
  `WalletSigningUnavailable`. No flag — absence of the seed is the signal; with no record
  published, an unconfigured deploy fails fast.
- **A seed in a readable file is not isolated.** A local `polyrob wallet init` writes the
  seed to `~/.polyrob/.env` (0600, but the agent's own OS user can read it). While a seed
  came from an env file this user can read, the host refuses model-written code
  (`run_tests`, `run_code`, the host shell) and names the fix: run code in the Docker
  sandbox (`CODE_EXEC_BACKEND=docker`; `polyrob doctor` lists the setup steps), or take the
  seed out of that file (back it up with `polyrob wallet export`, delete the key lines, and
  give the key to the agent through a root-owned service env file or the `polyrob-signer`
  unit). A `systemctl --user` install has no env file out of its own user's reach, so the
  sandbox is its fix.
  Git, LSP, `rg` and `MCP stdio` children stay allowed: they do not run code the model
  wrote. A seed from the process env (the systemd `EnvironmentFile`) is no file gap.
- **Network** — `AGENT_WALLET_NETWORK` (default `testnet`). On mainnet, `x402_wallet_status`
  surfaces the live on-chain USDC/gas balance via public read-only RPCs
  (`core/wallet/onchain.py`; canonical USDC = `0x8335…2913` Base mainnet, `0x036C…CF7e`
  Base Sepolia).
- **Spend caps — the PolicyGate** (`core/wallet/policy.py`): every spend passes through
  `policy.check()` before signing and `policy.record()` after. Ceilings:
  `AGENT_WALLET_MAX_PER_TX_USD` (default `250`, a catastrophic loss-guard, *not* a
  budget); `WALLET_DAILY_CAP_USD`
  (a 24h rolling cap, default `100` — the per-tx ceiling alone cannot stop a
  within-ceiling loop draining the treasury one ticket at a time; set
  `none`/`off`/`unlimited`/`disabled` to turn the daily cap off explicitly); per-venue
  24h caps (`WALLET_VENUE_DAILY_CAP_<VENUE>_USD` — no cap unless set; the same
  sentinel is a no-op for a per-venue key, and a malformed or negative value RAISES
  naming the key, same as the two caps above). An idempotency replay-guard prevents a
  retried step from double-paying.
- **Changing a cap** — `polyrob wallet set-cap daily 20` (or `set-cap per-tx 10`)
  writes the owner **preference** (`budget.wallet_daily_usd` /
  `budget.wallet_per_tx_usd`, the same keys as `/config set`). The live gate re-reads it
  at once, and it can only *tighten*: it is min-merged with the env cap. To raise the
  envelope or turn the daily cap off, add `--env`: `polyrob wallet set-cap --env daily
  250` writes the operator env var to `~/.polyrob/.env` after a confirmation and needs a
  restart.
- **Audit** — every spend appends a `wallet_spend` event: an append-only JSONL sink
  (`<data_dir>/wallet/audit.jsonl`) plus a telemetry event. This is what the unified
  ledger reads for the "spent" leg. A process that holds the seed seals the file: the
  rows are chain-hashed and the head is MAC'd into `audit.jsonl.seal` with a key derived
  from the master seed, so an edited or removed row fails the load and spending is
  refused until the ledger is repaired. A seedless process neither seals nor verifies;
  under `WALLET_SIGNER=remote` the signer's own ledger is the authoritative one.
- **Address poisoning** — a transfer to an address that only looks like a payee of the
  last 30 days, one of `OWNER_WALLET_ADDRESSES` or the agent's own wallet (matching first
  and last characters, a different body) is refused, and unreadable payment history
  refuses the send. The same check runs on `nft_transfer` and on the payee of a generic
  call ([onchain.md](onchain.md#calling-any-contract)). Copy an address from its source, not from the wallet's history.
- **The declared bound** — `max_spend_usd` asserts what the transaction moves or grants;
  the network fee is charged to the per-tx and daily caps only. A deploy, claim or mint,
  whose fee is its value, keeps the fee in the declared check.
- **Transfers to yourself** — set `OWNER_WALLET_ADDRESSES` (file-only) to your own
  receive addresses. A genuine owner turn that has read no third-party content and
  transfers to one of them, or to the agent's own wallet, waits on no second tap; a turn
  that read a page, a post or a mail queues it for your tap like any other.

Enable a testnet wallet:

```bash
AGENT_WALLET_ENABLED=true
AGENT_WALLET_NETWORK=testnet
AGENT_WALLET_MASTER_SEED=<32+ char secret, kept out of tracked files>
```

## Where the signatures come from — the signer

`WALLET_SIGNER` (default `local`) decides where signatures come from. The seed can live
in a separate process, `polyrob-signer` (`core/signer/`): its own OS user, a Unix socket
that checks the caller's UID, its own spend ledger and hard caps the agent cannot change.
The agent sends an intent; the signer runs the same transaction guard again and signs.

| Mode | Who holds the seed | What happens on an EVM send |
|---|---|---|
| `local` | the agent process | The agent checks the send and signs it in process. The signer does not run. |
| `shadow` | the agent and the signer | The agent signs locally and also asks the signer for a verdict on the same intent. The result goes to `<data>/wallet/signer_shadow.jsonl` and the `custody` status section. It never blocks a send. |
| `remote` | the signer only | The agent holds no key; a seed that reaches it is dropped. The signer re-checks the intent against its own caps and ledger, then signs and broadcasts. x402, Hyperliquid orders, ERC-8004 feedback and the deposit sweep use typed signer endpoints; Solana signing is refused. Above the signer's hard cap, the owner approves on the box: `sudo polyrob owner promote signer_approval <id>`. |

With the signer installed (`shadow` or `remote`), its hard caps are the envelope: the agent enforces the
lower of its own per-tx/daily cap and the signer's. A raise from chat stops at the signer's cap; raise that
one as root in `/etc/polyrob/signer.toml` `[caps]`, then restart `polyrob-signer`. An x402 signature has
two limits of its own: a limit per payment (a fresh install writes `X402_AUTONOMOUS_MAX_USD`, else $5,
never the per-transaction limit) and an x402 daily limit. Under `remote`, an authorization above either
waits for the same on-box root approval as an EVM send: `x402_fetch` answers with the approval id and
pays nothing; after `sudo polyrob owner promote signer_approval <id>` (it prints the signer's review of
the request and asks `y`), a retry of that same payment (same payee, same amount) signs once. Under
`shadow` the agent signs locally, so it refuses a payment above either limit before it pays.

A send whose outcome the agent could not learn (a timeout after the broadcast) leaves its nonce
`unknown` in the signer, and the signer refuses every later EVM send on that network until it is
decided. Decide it from the network, as root on the box: `sudo polyrob wallet signer-reconcile <network>`
(`--json` for machine output). A nonce the network took is recorded `sent`; a nonce the network
still waits for is `released`, and the next send may use it.

An unknown value reads as `local`. On a server, install the `polyrob-signer` unit (it runs
from its own venv, never the agent's), set `shadow`, and change to `remote` only after a
clean week of shadow results. The installer checks that week in the signer's own decision
log, which the agent cannot rewrite: a refused verdict, no agreed verdict, fewer than 7 days
of data or an unreadable log stops the cut-over. The cut-over writes `WALLET_SIGNER=remote`
only into `/etc/polyrob/agent-signer.env`, which only the agent's own units load, so the x402-api
unit stays signer-free. The
`WALLET_SIGNER` row in the configuration reference has the full detail.

## Create the wallet in one command

`polyrob wallet init` is the guided path — no manual seed to generate or paste:

```bash
polyrob wallet init                       # generate a fresh wallet (default)
polyrob wallet init --from-mnemonic "..."  # import an existing BIP-39 mnemonic
polyrob wallet init --from-seed "..."      # import a legacy raw seed (pre-BIP44 install)
```

- **No args** generates a fresh 24-word BIP-39 mnemonic and prints it **once** — write it
  down; it is never shown again except via `polyrob wallet export`. This is the **`bip44`**
  derivation scheme, so the mnemonic imports cleanly into MetaMask/Rabby (account 0 there
  == the treasury venue here).
- **`--from-mnemonic`** imports an existing BIP-39 mnemonic (also `bip44`).
- **`--from-seed`** imports a legacy raw seed (≥32 chars) — this keeps the **`legacy`**
  PBKDF2 derivation scheme so an older install's addresses never change.
- The command writes `AGENT_WALLET_ENABLED`/`AGENT_WALLET_MASTER_SEED` to
  `~/.polyrob/.env` (chmod 600) and records the derivation scheme write-once in
  `<data-home>/wallet/meta.json` — refuses if a seed is already configured (use
  `polyrob wallet export` to see it, or remove the env var first, deliberately, to
  replace it).
- It then offers to point `X402_PAYMENT_RECIPIENT` at the new treasury address so
  invoices settle somewhere the agent can actually spend from — accept the prompt (or
  pass `--yes`) to link it automatically.
- On testnet (the default) it tells you to fund the printed address from a Base-Sepolia
  faucet; on mainnet it tells you to send real USDC on Base.

## Portability, backup and export

`polyrob wallet export [--venue <venue>]` reveals the private key material — the seed
that controls the funds. It is deliberately hostile to accidental exposure:

- **TTY-only** — refuses to run when stdin/stdout are piped (no accidental leak into a
  log file or script capture).
- Requires typing `EXPORT` at a confirmation prompt before printing anything.
- For a `bip44` wallet with no `--venue`, it prints the **mnemonic** first (importable
  into any standard wallet); `--venue <treasury|x402|polymarket|hyperliquid>` narrows to
  just that venue's raw `0x`-hex private key.
- For a `legacy` wallet there is no mnemonic — only per-venue hex keys.
- It warns you to clear your terminal scrollback/shell history afterward — the output is
  exactly as sensitive as the funds.
- **Never agent-callable.** This is an operator-only CLI command; no tool exposes it to
  the agent.

> ⚠️ **`polyrob update` snapshots copy your `.env` files — including the wallet seed —
> into the local snapshots directory; treat snapshot storage with the same care as the
> seed itself.**

## Migrating to a new install

Moving the wallet to a fresh machine or a new install:

1. On the old install:
   - **`bip44`:** `polyrob wallet export` and copy down the mnemonic.
   - **`legacy`:** `export` does **not** print the raw seed — read it from
     `AGENT_WALLET_MASTER_SEED` in `~/.polyrob/.env` (or a `polyrob update` snapshot).
     The per-venue keys `export` shows are for importing single accounts into
     MetaMask/Rabby; they are **not** what `--from-seed` expects.
2. On the new install: `polyrob wallet init --from-mnemonic "..."` (`bip44`) or
   `polyrob wallet init --from-seed "<raw seed>"` (`legacy` — the raw master seed from
   step 1, **never** an exported venue key; a venue key would derive different addresses
   and strand the funds). This reconstructs the identical addresses, since derivation is
   deterministic from the seed plus the recorded scheme.
3. Every wallet-affecting action (init/export/spend) also appends to the append-only
   `wallet_spend`/audit trail (`<data_dir>/wallet/audit.jsonl`) — the new install starts
   a fresh audit file, so reconcile old vs. new manually if you need continuous spend
   history across the move.

> ⚠️ **`EIP8004_AGENT_PRIVATE_KEY` is NOT part of this migration.** It's a second,
> independent signing key for ERC-8004 feedback authorizations ([payments.md](payments.md#62-payment-backed-reputation)) — it is not derived
> from `AGENT_WALLET_MASTER_SEED` and has no relationship to the wallet's derivation tree.
> None of the steps above carry it. If you use `EIP8004_PAYMENT_FEEDBACK`, copy
> `EIP8004_AGENT_PRIVATE_KEY` to the new install's env yourself, or the 8004 signing
> identity is silently left behind (subsequent feedback signing fails closed, but the key
> itself doesn't move with the wallet).

---

## Paying for resources — x402 pay side (`tools/x402/`)

Lets the agent **find** and **pay** for paywalled HTTP resources during a job. Off
by default (`X402_CLIENT_ENABLED=false`). The paying verbs additionally need
`AGENT_WALLET_ENABLED`; the read-only discovery verbs do **not** — pricing an
endpoint costs $0, so an invoice-only deployment can still map the market.
Exposed as the
`x402_pay` tool. Two of its actions **pay**, three are **read-only**:

| action | pays? | wallet? | what it does |
|---|---|---|---|
| `x402_probe` | no | not needed | Probe ONE endpoint: price, `accepts[]`, routing, payability score 0–5 |
| `x402_sweep` | no | not needed | Probe MANY endpoints → one scored ledger |
| `x402_quote` | no | not needed | Price a single URL (thin; `x402_probe` is the fuller read) |
| `x402_fetch` | **yes** | required | Fetch, paying up to `max_amount_usd` only to the approved `expected_pay_to` |
| `x402_wallet_status` | no | required | Address, on-chain balance, caps, audit |

### Discovery (`x402_probe` / `x402_sweep`) — read-only, $0

Before an agent can transact it has to answer three questions: does this endpoint
charge, how much, and *can I actually pay it?* Discovery answers all three without
a wallet and without ever sending a payment header.

The **payability score** is 0–5, and names what is missing when it falls short:
`+1` answered · `+1` HTTP 402 · `+1` parseable challenge body · `+1` price
disclosed · `+1` full routing (`asset` **and** `network` **and** `payTo`). Only a
5 means an agent could pay it today — a 402 that discloses no price or no routing
is a paywall in name only, and is reported as such rather than as "payable".

Both verbs accept GET, HEAD or OPTIONS without a body; POST-only discovery is unavailable,
and read a challenge from the **response body** as well as the
`PAYMENT-REQUIRED` header — the spec puts the requirements in the body, and
POLYROB's own middleware emits exactly that shape. All decoding delegates to the
one client-side parser (`RealX402Client._decode_challenge`), so the payer and the
prober can never drift apart.

Guardrails: bounded to 50 targets at 8 concurrent, and every agent-supplied URL
goes through the same SSRF validator `web_fetch` uses (cloud metadata and RFC1918
stay shut). A missing price is reported as unknown, never as `$0`.

The `x402_fetch` flow (`tools/x402/service.py`, `real_client.py`):

1. Owner pause check (`autonomy_halted()` — the `all` scope of the owner pause — refuses every spend the agent starts on its own, fail-closed; a genuine owner turn such as `/pay … go` passes it, and a call with no context is never the owner).
2. Advisory price probe (`quote()`); reject if the priced amount exceeds your
   `max_amount_usd`.
3. **PolicyGate `check()` runs unconditionally** — if the probe can't price it, the
   worst-case authorized ceiling is used (never skip the gate).
4. **Asset-pin + network binding** — the payment requirement's asset must be the
   canonical USDC for the configured network, and the challenge's network must match
   (testnet → `base-sepolia`/`eip155:84532`, mainnet → `base`/`eip155:8453`), enforced
   both at the probe and at the SDK signing hook. This defeats a malicious paywall that
   names a different token (a decimals-spoof that could inflate the cap) or a different
   chain.
5. Sign + pay on the wire via the `x402` SDK; record the **actual settled** amount
   (a `success=false` settlement is treated as unpaid).

**A paid fetch names its payee.** `x402_fetch` requires `expected_pay_to`, copied from
the `payTo` address that `x402_quote` or `x402_probe` shows. The approval displays this
recipient in full. A server that changes the recipient is refused before signing; only
EIP-3009 transfers are supported. This binds the approved address, not the server's
claimed identity. A fetch without a recipient can read a free response but cannot pay.

`x402_pay` is **leaf-delegation-blocked** (in `DELEGATE_BLOCKED_TOOLS`) and
**correspondent-taint-blocked** — a tainted or delegated-child turn can never spend.

### Who may pay

Three origins, and only three:

- **The owner, from chat** — `/pay <url> [max_usd] [go] [id=<name>]` on Telegram and in the
  REPL. Each `/pay` message is its own payment (its Telegram update id names it), and a
  re-delivered message maps to the same key, so it can never pay twice.
  Bare form prices the URL and pays nothing; `go` pays up to the `max_usd` you name
  (it refuses to pay without one). GET only, in USDC on Base. Refused from a group room.
- **An autonomous goal or cron run on the main agent** — once `DEFI_AGENT_AUTONOMY`
  is armed and the tool exists (`X402_CLIENT_ENABLED` + `AGENT_WALLET_ENABLED`),
  `x402_pay` is in the autonomous toolset. A payment whose `max_amount_usd` is at or
  below `X402_AUTONOMOUS_MAX_USD` (default $1) runs and reports; above it the durable
  owner queue holds it for your `/approve`. `WALLET_VENUE_DAILY_CAP_X402_USD` bounds the
  day. Once armed, a goal the agent writes for ITSELF can carry `x402_pay` too, exactly
  as it can carry `defi_trade` (`tools/goal_tools.allowed_self_goal_tools` folds in the
  whole autonomous grant); what bounds an injected goal is the lane and the caps above,
  not the toolset. Unarmed, a self-written goal cannot request it. A money goal or job
  that the model writes during YOUR turn is not written: it becomes a decision that
  you Confirm.
- Never a leaf/sub-agent, a self-wake, a delegation-result re-entry or a
  correspondent-tainted turn.

### Limits worth knowing

- **The SDK's own spend control.** The x402 SDK caps every payment at $1
  unless the client sets its own control. The client sets it to your `max_amount_usd`,
  so the only per-payment cap is the one you declared.
- **Which offer is paid.** A 402 may list several payment options. The client pays the
  first one it CAN pay — your configured network, canonical USDC — even when another
  network is listed first. With no such entry it refuses.
- **One payment per call, not per URL.** The replay guard keys a payment on the URL,
  the cap, the method and the body. To pay the same metered URL again on purpose, pass a
  new `request_id`; a retry of the same call reuses its id and can never pay twice.
  The key also rides the submission-journal row written before signing, so an operator
  `polyrob wallet release-submission` of a stuck payment books under it, and a retry of
  the same `request_id` after the release is still refused.
- **The authorization window.** The EIP-3009 authorization the server asks for must be
  valid for at most 600 s; a longer (or unset) window is refused.
- **The send lock.** Before the SDK signs, the payment is written to the wallet's
  submission journal. A payment that fails in a way that cannot prove it was unused (a
  second 402, a timeout, `success=false`) leaves that row unresolved, and an unresolved
  row blocks EVERY later payment and on-chain send until it is reconciled
  (`python -m core.wallet.submission_recovery` reports what is open). That is
  deliberate: an unknown outcome is not treated as "nothing was paid".

---

## Signing in to a site (`wallet_sign`)

`wallet_sign` is available when `DEFI_TRADE_ENABLED` is on.

Some services identify an agent by its wallet: Sign-In With Ethereum, Sign-In With
Solana, or a "sign this challenge to enroll your payout wallet" step. `wallet_sign`
signs those texts — and only those. Every request is classified first:

| Class | Example | Result |
|---|---|---|
| A — login / challenge bound to its site | SIWE for the site the agent is talking to; a challenge the agent fetched itself | signed |
| B — value-moving typed data | an order, a Permit2 transfer | refused (planned) |
| C — authority, or a text not bound to its site | an ERC-2612 permit; "prove you own this wallet" handed over from elsewhere | refused (an owner decision is planned) |
| D — blind | a hex hash, an encoded transaction, opaque bytes | refused, always |

`wallet_sign_challenge` is the normal path, in one call: it asks the site's HTTPS
challenge URL (POST or GET), signs the exact text the site returned, and POSTs the
signature to the site's `submit_url` — `{challenge.challengeId}`, `{signature}` and
`{address}` fill the body, and a login cookie the challenge set rides along. A
signature made without `submit_url` is sent nowhere; the agent is told so, and a second one
for the same site refuses. A key or login cookie the site returns is stored
encrypted and shown only as a fingerprint; `wallet_sign_api_call` uses it, and only on
that site. A site that returns only a nonce gets a Sign-In With Ethereum/Solana
message built from `message_template`. `wallet_sign_message` signs a text you hand over, on an owner turn
only, and the text must name the site. A goal or scheduled job may sign at most 10
class-A texts per site and 30 in all per day, must send the signature straight back
to the site, and never signs a challenge that does not name its site; a sign-in
nonce is never signed twice. A text that derives a key (an L2 or trading key), that
authorizes or spends, or that names another site is refused. The key is
the agent's operational wallet; under `WALLET_SIGNER=remote` the signer
re-classifies every request itself (`sign.classified`).

`/wallet signed [n]` lists what the wallet signed, newest first: the site, the kind,
the class and whether a goal or job signed it, then the keys sites gave back (only
their fingerprints). It reads the log and signs nothing.

---

## See also

- [payments.md](payments.md) — the safety model, getting paid, credits, the ledger and the arming order.
- [onchain.md](onchain.md) — on-chain sight, the transaction guard and value movement.
- [security-model.md](security-model.md) — where these gates sit in the layered trust model.
- [`docs/CONFIGURATION.md`](../CONFIGURATION.md) — every wallet, signer and x402 flag with its default.
