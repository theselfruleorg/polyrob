# Payments, Wallet & Crypto

This guide is the single, complete reference for **every crypto- and money-related
capability** in POLYROB: the agent wallet, paying for resources (x402), getting paid
(invoicing / built-in ecommerce), on-chain settlement, watchtower subscriptions,
metering-to-invoice, the machine-payer HTTP surface, ERC-8004 reputation, platform
billing (credits), deposit addresses, the crypto trading tools, and the unified ledger.

> ⚠️ **Unaudited — use at your own risk.** These features have had **no independent
> security audit**. They handle real value on mainnet and are provided as-is with no
> warranty (see [LICENSE](../../LICENSE) and
> [SECURITY.md](../../SECURITY.md#crypto--wallet--payment-features)). **Everything here is
> OFF by default.** Evaluate on testnets first. `modules/x402/README.md` is the deep
> module reference; `docs/CONFIGURATION.md` is the authoritative flag SSOT (this guide
> quotes defaults but the catalog wins).

---

## 1. The safety model (read first)

Three invariants hold across the whole surface:

1. **Default-OFF, fail-open.** No money feature runs unless you set its flag. A
   deployment that enables none of them is *functionally* byte-identical to a plain
   server — no money code executes and no value can move. It is not *literally*
   byte-identical on the wire: a few inert reads/tables are always present regardless
   of flag state (empty invoice/subscription tables, an always-mounted x402 info route,
   always-mounted 8004 reads, always-mounted payments endpoints that self-404/503 when
   off) — see `docs/CONFIGURATION.md`'s "byte-identical at defaults" note for the
   complete list; none of them can move value. Every money hook is wrapped so a failure
   degrades (logs + skips) rather than blocks a settlement, wake, or the request path.
2. **Agent finances ≠ platform billing.** The agent's own balance sheet (what it earns
   and spends on-chain) is a separate concern from `modules/credits` (what users owe
   the platform for LLM calls). The **unified ledger** only *joins* them read-only —
   receivables are never written into the wallet spend audit.
3. **Money can't be minted by a stranger.** Outward money is approval-gated; a
   correspondent's payment or message can never gain steering rights; a
   forged / self-wake / delegated-leaf turn can never reach a money-moving verb; caps
   apply everywhere.

**Crypto is the only rail today.** USDC on Base (via the [x402 protocol](https://www.linuxfoundation.org/x402foundation),
now a Linux Foundation standard). Fiat (Stripe) is a designed-for but **deferred**
extension. A **no-payments deployment degrades gracefully** — the invoice tool is simply
absent, nothing breaks.

The five money organs and where they live:

| Organ | Package | What it is |
|---|---|---|
| **Agent treasury** (custody) | `core/wallet/` | The on-chain wallet the agent controls: keys, spend caps, audit |
| **Agent receivables** (invoicing) | `modules/x402/` | What the agent is owed: invoices, settlement, subscriptions |
| **Platform billing** (credits) | `modules/credits/` | What users owe the platform per LLM call; balances, deposits |
| **Metering** (measurement) | `usage_records` | Per-call cost measurement, tenant-scoped |
| **Accounting** (read-only join) | `unified_ledger.py` | earned / pending / spent / net across the above |

---

## 2. The agent wallet (`core/wallet/`)

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
- **Network** — `AGENT_WALLET_NETWORK` (default `testnet`). On mainnet, `x402_wallet_status`
  surfaces the live on-chain USDC/gas balance via public read-only RPCs
  (`core/wallet/onchain.py`; canonical USDC = `0x8335…2913` Base mainnet, `0x036C…CF7e`
  Base Sepolia).
- **Spend caps — the PolicyGate** (`core/wallet/policy.py`): every spend passes through
  `policy.check()` before signing and `policy.record()` after. Ceilings:
  `AGENT_WALLET_MAX_PER_TX_USD` (default `1000`, a catastrophic loss-guard, *not* a
  budget); `WALLET_DAILY_CAP_USD` (a 24h rolling cap, unset = disabled); per-venue
  24h caps (`WALLET_VENUE_DAILY_CAP_<VENUE>_USD`). Owner **preferences** can only
  *tighten* these (`/config set budget.wallet_daily_usd …` min-merges with the env cap).
  An idempotency replay-guard prevents a retried step from double-paying.
- **Audit** — every spend appends a `wallet_spend` event: an append-only JSONL sink
  (`<data_dir>/wallet/audit.jsonl`) plus a telemetry event. This is what the unified
  ledger reads for the "spent" leg.

Enable a testnet wallet:

```bash
AGENT_WALLET_ENABLED=true
AGENT_WALLET_NETWORK=testnet
AGENT_WALLET_MASTER_SEED=<32+ char secret, kept out of tracked files>
```

### 2.1 Create the wallet in one command

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

### 2.2 Portability, backup & export

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

### 2.3 Migrating to a new install

Moving the wallet to a fresh machine or a re-installed instance:

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
> independent signing key for ERC-8004 feedback authorizations (§8) — it is not derived
> from `AGENT_WALLET_MASTER_SEED` and has no relationship to the wallet's derivation tree.
> None of the steps above carry it. If you use `EIP8004_PAYMENT_FEEDBACK`, copy
> `EIP8004_AGENT_PRIVATE_KEY` to the new install's env yourself, or the 8004 signing
> identity is silently left behind (subsequent feedback signing fails closed, but the key
> itself doesn't move with the wallet).

---

## 3. Paying for resources — x402 pay-side (`tools/x402/`)

Lets the agent **pay** for a paywalled HTTP resource during a job. Off by default
(`X402_CLIENT_ENABLED=false`; also needs `AGENT_WALLET_ENABLED`). Exposed as the
`x402_pay` tool with three actions: `x402_quote` (price a URL without paying),
`x402_fetch` (fetch, auto-paying via x402 up to a caller-set `max_amount_usd`), and
`x402_wallet_status` (address, on-chain balance, caps, audit).

The `x402_fetch` flow (`tools/x402/service.py`, `real_client.py`):

1. Owner kill-switch check (`autonomy_halted()` refuses all spend, fail-closed).
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

`x402_pay` is **leaf-delegation-blocked** (in `DELEGATE_BLOCKED_TOOLS`) and
**correspondent-taint-blocked** — a tainted or delegated-child turn can never spend.

---

## 4. Getting paid — invoicing / built-in ecommerce (`modules/x402/`)

The agent can quote, invoice (as text **and** a branded QR image), get paid (USDC,
auto-detected), deliver, and account for it. Master flag `X402_INVOICE_ENABLED`
(default OFF) + a treasury address (`X402_PAYMENT_RECIPIENT`).

### 4.1 Create an invoice

The `x402_invoice` tool (`tools/x402/invoice_tool.py`) exposes `x402_request`,
`x402_invoices`, and `accounting`. `x402_request` creates a *pending*
`x402_payment_requests` row (`modules/x402/invoicing.py`):

- Amount ceiling `X402_INVOICE_MAX_USD` (default `50`), per-tenant daily cap
  `X402_INVOICE_DAILY_MAX` (default `10`).
- The counterparty is carried two ways: a **free-form `payer_contact`** string
  ("Alice \<alice@example.com\>", rendered "billed to") and a **typed
  `correspondent_ref`** `{surface, address, thread_id}` used for delivery + routing the
  settlement wake back to the originating session.
- Tenant-scoped by `json_extract(metadata,'$.tenant_id')`.
- Emits a first-class `payment_requested` event.

`x402_request` is **approval-gated, leaf-blocked, and correspondent-taint-blocked.**

### 4.2 The invoice as a branded image

With `INVOICE_CARD_ENABLED` (default OFF, **ON under `POLYROB_LOCAL`**),
`modules/pfp/cards.py::render_invoice_card` composes a branded PNG in **pure Pillow**
(never a headless browser): the instance's Mindprint face, amount, purpose, request id,
expiry, "billed to", a QR block, and pay instructions, using a shipped OFL font under
`assets/fonts/`. The QR payload (`modules/x402/artifact.py`) is controlled by
`INVOICE_QR_STYLE` (`address` default = the bare treasury address; `eip681` = a prefilled
`ethereum:<usdc>@<chain>/transfer?…` URI, auto-preferred when on-chain detection is on).
Rendering **fails open to text-only.**

### 4.3 Delivering it

The outbound-media leg turns `OutboundMessage.media` into a real attachment:
**Telegram photo** (`send_photo`), **email attachment**, or the agent-callable
`message(media_paths=[…])` tool (paths are workspace-confined and symlink-guarded).
Text-only surfaces deliver the text and note the omission honestly.

### 4.4 Approval — `approve` vs `auto`

`PAYMENT_APPROVAL_MODE` (default `approve`) is the single owner-legible switch:

- **`approve`** — every outward payment request goes through the durable, remotely
  approvable `owner_queue` provider (`tools/controller/approval_queue.py`): it records a
  durable ask (on the goal-board asks store), pushes one owner notification, and waits
  up to `APPROVAL_TIMEOUT_SEC`. Approve from Telegram with `/approve tap-<id>` (or
  `polyrob owner pending`). A post-timeout approval becomes a one-shot grant
  (`APPROVAL_GRANT_TTL_HOURS`).
- **`auto`** — requests **within the caps** auto-approve and notify the owner after the
  fact. Auto mode never widens the caps.

### 4.5 Getting paid — three settlement paths

A pending invoice completes by any of:

1. **Owner attestation** — `polyrob owner settle <id> [--tx-hash]`.
2. **Payer-driven facilitator** — a machine payer hits
   `POST /api/x402/requests/{id}/pay` (the public endpoint runs the x402 facilitator
   verify+settle).
3. **On-chain USDC detection** — gated `X402_SETTLE_ONCHAIN_DETECT` (mainnet +
   treasury). ⚠️ **Silently inert unless `X402_INVOICE_ENABLED` is also on** — the
   settlement watcher only starts when the autonomy runtime sees `X402_INVOICE_ENABLED=true`
   (`core/autonomy_runtime.py`); `X402_SETTLE_ONCHAIN_DETECT=true` on its own starts no
   ticker and detects nothing. The settlement watcher (`modules/x402/settlement_watcher.py`,
   running on the autonomy-runtime ticker every `X402_SETTLEMENT_WATCH_INTERVAL_SEC`, default 60s)
   scans treasury USDC `Transfer` logs (`modules/x402/onchain_probe.py`), keeps a
   per-treasury `settlement_scan` block checkpoint (bounded by
   `X402_SETTLEMENT_SCAN_MAX_SPAN`, confirmations `X402_SETTLEMENT_CONFIRMATIONS`),
   matches an incoming transfer to a pending invoice by **exact atomic amount,
   oldest-first**, and settles it. **This makes the human-payer loop facilitator-free**
   — a payer who just sends USDC to the address on the invoice is detected. Safety:
   `transaction_hash` has a partial-unique index + a `claim_for_settlement` CAS
   (a tx settles at most one invoice, ever); a transfer matching nothing emits a
   `payment_unmatched` owner notice; and **amount-jitter** (`X402_INVOICE_AMOUNT_JITTER`,
   forced ON with detection) nudges same-amount invoices sub-cent apart, disclosed at
   full precision on every payer-facing surface so the payer sends an unambiguous amount.

On settlement the watcher re-enters the originating session via the self-wake rail
(a correspondent-linked invoice delivers the notice as DATA, never as a command). On
expiry it escalates to the session **and** a one-off owner notice. Events:
`payment_requested` / `payment_settled` / `payment_expired` / `payment_unmatched`.

> **Settlement is attested or detected, never blindly inferred.** The owner CLI and the
> facilitator `/pay` path are explicit; on-chain detection matches a real, confirmed,
> exact-amount transfer to the treasury.

> **Runbook — a `payment_unmatched` notice for an amount that already has a `completed`
> invoice.** The facilitator `/pay` path settles-then-responds; if the payer's HTTP client
> disconnects or times out in that gap *after* the facilitator already moved USDC on-chain,
> the invoice can end up `completed` (or transiently stuck `settling`) while that same
> on-chain transfer is later picked up by the on-chain-detection scan, finds no PENDING
> invoice left to match, and emits a `payment_unmatched` owner notice/event (`tx_hash`,
> `from`, `amount_usd`, `block`, `treasury` — `modules/x402/settlement_watcher.py::_notify_unmatched`).
> This is the expected, at-most-once-settlement shape of that race, not a stray extra
> payment: if the unmatched amount matches an already-`completed` invoice for the same
> payer/treasury, don't book it as new revenue — verify the on-chain transaction (`tx_hash`
> in the event) really is the SAME payment as the completed invoice, then **refund the
> duplicate** to the payer rather than double-counting it.

---

## 5. Watchtower subscriptions (`modules/x402/subscriptions.py`)

Paid recurring monitoring — the first revenue product. Model: **prepaid period + renewal
invoice**, driven entirely from the same settlement-watcher tick. Gated
`SUBSCRIPTIONS_ENABLED` (default OFF). Default price `WATCHTOWER_PRICE_USD` = `$10.00`/mo.

- A `subscriptions` row binds a correspondent + a cron watchtower job + an amount +
  `paid_through`. A settled invoice tagged with its `subscription_id` extends
  `paid_through` by one period via an **atomic** `apply_settlement` (a
  `subscription_applied_settlements` PK ledger makes it idempotent; a typed
  `SettlementResult` — APPLIED / ALREADY_APPLIED / REFUSED / UNKNOWN — and a
  CancelledError-safe transaction prevent double-extend or lost extension).
- Near `paid_through − SUBSCRIPTION_RENEWAL_LEAD_DAYS` (default 5) the watcher mints a
  renewal invoice (respecting `PAYMENT_APPROVAL_MODE`); a partial-unique index prevents
  duplicate open renewals. Past `paid_through` → grace; past
  `+ SUBSCRIPTION_GRACE_DAYS` (default 3) → suspended, with owner + correspondent
  notices.
- The cron watchtower job gates on subscription status: a suspended/canceled sub's job
  takes a $0 `subscription_lapsed` skip (the agent is never invoked).
- Admin: `polyrob owner sub list` / `polyrob owner sub cancel <id>`.

> **Note:** the subscription machinery is inert until a `create_subscription` caller is
> wired to a provisioning surface, and — like the rest of the settlement-watcher-driven
> money layer (invoice amount-jitter dedupe, on-chain detection) — it assumes
> `UVICORN_WORKERS=1`; the pending-renewal unique index is only a cross-process backstop,
> not a substitute for single-worker. See the canonical `workers>1` note in
> `docs/CONFIGURATION.md` for the full money-layer `workers=1` rationale (M5).

---

## 6. Metering → invoice bridge (`modules/credits/usage_rollup.py`)

Turn measured LLM usage into a *draft* invoice. Gated `USAGE_INVOICE_BRIDGE_ENABLED`
(default OFF). A tenant-scoped `usage_rollup(user_id, session_id?, since?)` sums
`api_cost_usd` from `usage_records`; the `usage_summary` read action returns it and can
propose an invoice payload (amount = cost × `USAGE_INVOICE_MARKUP`, default `1.0`).
**The bridge only suggests** — the agent must still fire the approval-gated
`x402_request`; the bridge never mints a payment request itself. `usage_summary` is
anon-refused and correspondent-taint-denied (cost data is not exposed to a tainted turn).

---

## 7. The machine-payer HTTP surface (x402 middleware)

Lets an external agent/service pay per-request over HTTP. Gated `X402_ENABLED` (default
OFF) + a treasury (`X402_PAYMENT_RECIPIENT`).

- The middleware (`modules/x402/middleware.py`) 402-gates exactly these **billed** routes
  (exact `(method, path)`, not prefix, so free reads/continuations aren't paywalled):
  `POST /a2a/rpc`, `POST /a2a/message/stream`, `POST /a2a/tasks`,
  `POST /v1/chat/completions` — i.e. the A2A and OpenAI-compatible surfaces.
- Price SSOT `get_x402_price_usd()`: explicit `X402_PRICE_USD` wins, else derived from
  `X402_MAX_TOKENS_PER_REQUEST` (200k) × model rate × `X402_PRICE_MARKUP` (2.0)
  (≈ $30 unset).
- Flow: anonymous request → `402` + a challenge (`build_x402_challenge`) → payer signs
  an EIP-3009 authorization and retries with an `X-PAYMENT` header → the facilitator
  verifies+settles → the request is served without charging credits. If the facilitator
  library is unavailable, a request that carries `X-PAYMENT` gets an honest `503` —
  **but only when the caller has no other auth** (an API-key/JWT caller with a stray
  `X-PAYMENT` header is served normally).
- **Facilitator:** testnet `base-sepolia` uses the free `x402.org` facilitator (no
  credentials); mainnet uses the Coinbase CDP-hosted facilitator (needs
  `CDP_API_KEY_ID`/`CDP_API_KEY_SECRET`). Facilitators are pluggable by the x402 spec.
- **Public invoice endpoints** (`api/x402_endpoints.py`): `GET /api/x402/requests/{id}`
  (a per-invoice 402 challenge) and `POST /api/x402/requests/{id}/pay`, rate-limited by
  an **un-spoofable** client key (`get_trusted_client_ip` — `X-Forwarded-For` is trusted
  only from a configured trusted proxy; default trusts loopback only, plus
  `X402_TRUSTED_PROXIES`). Limits: `X402_PUBLIC_RATE_PER_WINDOW` (20) /
  `X402_PUBLIC_RATE_WINDOW_SEC` (60).

---

## 8. Payment-backed reputation — ERC-8004 (`modules/eip8004/`)

ERC-8004 is the **trust/discovery** layer (who is this agent, can I trust it), *not* a
payment rail — it composes with x402 (how do I pay it). Gated `EIP8004_ENABLED`
(default OFF; discovery-only when off). The module implements the Trustless Agents
standard: an Identity Registry (a registration file linking the A2A card + wallet + x402
pricing endpoint), a Reputation Registry, and a Validation Registry, served at
`/eip8004/*`.

With `EIP8004_PAYMENT_FEEDBACK` (default OFF), a settled invoice becomes a
**verified-purchase** signal — but ⚠️ **only if `X402_INVOICE_ENABLED` is also on.** The
feedback offer is raised from inside the settlement watcher's settle path
(`SettlementWatcher._maybe_offer_payment_feedback`), and — same as
`X402_SETTLE_ONCHAIN_DETECT` above — the watcher itself only runs when
`X402_INVOICE_ENABLED=true`; `EIP8004_PAYMENT_FEEDBACK=true` alone offers nothing.
On settlement of a correspondent-linked invoice the agent
offers the payer a `ProofOfPayment`-backed feedback **authorization** (it never submits
feedback on the payer's behalf). `submit_feedback` verifies the proof against the ledger
— the invoice exists and is settled, the proof's `toAddress` equals the treasury, the
`txHash` hasn't already backed a feedback (replay guard), and the caller's `agent_id`
matches the EIP-712-signed authorization. This is the anti-sybil signal 8004 was designed
for.

> **Status:** the `ReputationManager` is currently a local simulation — nothing writes to
> a real on-chain registry yet. Keep the flag off outside evaluation.

---

## 9. Platform billing — credits (`modules/credits/`)

Separate from the agent's own money: this is what *users* owe *the platform* for LLM
calls. The real gate is `ENABLE_AUTH` (off = no billing service registered at all).

- **Metering:** each LLM call becomes a `usage_records` row (keyed by `user_id` +
  `session_id`), with cost from the model registry including cached-input and cache-write
  pricing. A stable `request_id` column dedupes a retried bill of the same completion.
  Credits are deducted fail-fast (`InsufficientCreditsError` halts on depletion) unless
  `CHAT_SKIP_CREDIT_CHECK` (ON) skips the chat path. `CREDIT_VALUE_USD` = `$0.01`,
  `WELCOME_BONUS` = `100`.
  - **Operational note:** `usage_records.user_id` has a foreign key to `user_profiles`.
    A headless single-owner deployment now **seeds an owner `user_profiles` row at
    startup** (`ensure_owner_profile`) so metering actually persists — without it, every
    metering write fails the FK and spend reads as a false `$0`.
- **Resilience:** `CREDIT_SENTINEL_ENABLED` (ON) latches on credit-death (402 / quota)
  and pauses dispatch with one notice; `BILLING_FAILOVER_ENABLED` (ON) tries provider
  fallback on a billing/quota error before halting.
- **Deposit addresses** (`api/payment_endpoints.py`, prefix `/payments`, strict JWT):
  `GET /payments/deposit-address` returns a per-user crypto deposit address (derived from
  `PAYMENT_MASTER_SEED`), `GET /payments/balance`, `/transactions`, `/deposits`, and a
  public `/payments/pricing`. On-chain deposits are watched by an out-of-band monitor
  (`DEPOSIT_MONITOR_ENABLED`, default OFF; ETH price via an oracle bounded by
  `ETH_PRICE_USD_MAX`); there is no in-band credit-purchase POST.
- **Treasury sweeper** (`modules/payments/treasury_sweeper.py`): sweeps per-user deposit
  balances into `TREASURY_ADDRESS` on an interval (`SWEEP_INTERVAL`, default 3600s). It
  **signs and broadcasts real fund-moving transactions**, so it is gated by its own
  dedicated switch, `TREASURY_SWEEPER_ENABLED` (default **false**) — before this flag
  existed the sweeper started on config presence alone (`TREASURY_ADDRESS` + a resolved
  master seed + `ENABLE_AUTH`), which meant a box that merely still *had* those three set
  in an old env file re-lit fund-moving on next boot with no dedicated kill-switch. Now
  `TREASURY_SWEEPER_ENABLED=true` must be set explicitly; with it off, config presence
  alone only logs that the sweeper is disabled and moves nothing. See
  `docs/CONFIGURATION.md` for `TREASURY_SWEEPER_ENABLED`/`TREASURY_ADDRESS`/`SWEEP_INTERVAL`.

---

## 10. Crypto trading tools (`tools/hyperliquid/`, `tools/polymarket/`)

Beyond payments, the agent can trade — **but live trading is dry-run by default and
double-gated.** Both are in `DELEGATE_BLOCKED_TOOLS` and the correspondent high-impact
set.

- **Hyperliquid** (perps, Arbitrum) and **Polymarket** (prediction markets, Polygon):
  read actions (orderbook, positions, market data — `polymarket_data` is fully read-only,
  no wallet) work whenever the tool is loaded. **Order placement is validated but
  NOT submitted** unless `tools/crypto_trade_gate.evaluate_live_trade` passes — which
  requires the master switch `CRYPTO_TRADE_LIVE_ENABLED` **and** the venue switch
  (`HYPERLIQUID_TRADING_ENABLED` / `POLYMARKET_TRADING_ENABLED`), all default OFF, and
  the order value within the per-venue cap (`HYPERLIQUID_TRADE_MAX_USD` /
  `POLYMARKET_TRADE_MAX_USD`, default `$5` each). A blocked order returns a `dry_run`
  result, never a silent submission.
- The `polymarket`/`hyperliquid` wallet venues never hold a spendable float in the
  hub-and-spoke model — funding those venues for live trading is a deliberate,
  separate operator step.

> Treat live trading as the least-exercised, highest-risk surface. Keep it off unless you
> are actively testing with funds you can lose.

> ⚠️ **The four order-placement verbs are also `PAYMENT_APPROVAL_TOOLS`, and approval
> fires BEFORE the dry-run decision.** The owner-approval pre-hook gates on ACTION NAME,
> not on whether the order will actually be live — so under the default
> `PAYMENT_APPROVAL_MODE=approve`, even a pure paper-trading posture
> (`CRYPTO_TRADE_LIVE_ENABLED` OFF) requires **one owner approval per dry-run order**. An
> autonomous/forged turn (goal, cron, self-wake) has no one to tap "approve" for it, so it
> **cannot paper-trade at all** — a goal-driven dry-run trading rig is retired by design;
> exercise dry-run trading interactively, or call the venue tool directly outside the
> approval-gated action. Cancel verbs (`cancel_order`/`cancel_all_orders`) are NOT in
> `PAYMENT_APPROVAL_TOOLS`, but the owner kill-switch (`AUTONOMY_HALT` /
> `polyrob owner halt`) freezes them too — during an incident, cancel open orders directly
> at the venue, not through the agent.

---

## 11. Accounting — the unified ledger (`modules/credits/unified_ledger.py`)

One read-only model joins three legs, tenant-scoped, each fail-open:

- **Costs** — `SUM(api_cost_usd)` from `usage_records`.
- **Spent** — `wallet_spend` events (the wallet audit).
- **Earned / pending** — settled and pending `x402_payment_requests`.

It reports `earned / pending / spent / net`. Owner-facing surfaces that read it:
the agent-callable `accounting` and `agent_status` actions, the CLI `/journey` ("Earned"
line) and `polyrob finance`, the webview `/finance` page, the owner digest's Money line,
and Telegram `/recap`.

---

## 12. Deployment recipes

**No payments (default).** Set nothing. The invoice tool is absent; `/finance` shows
zeros; nothing crypto runs.

**Invoice-only (get paid, no agent wallet), testnet.**
```bash
X402_INVOICE_ENABLED=true
X402_PAYMENT_RECIPIENT=0xYourTreasury…
X402_DEFAULT_CHAIN=base-sepolia         # testnet first
INVOICE_CARD_ENABLED=true               # branded QR cards
PAYMENT_APPROVAL_MODE=approve           # tap to approve outward money
```

**Add facilitator-free on-chain settlement (mainnet).**
```bash
AGENT_WALLET_NETWORK=mainnet
X402_SETTLE_ONCHAIN_DETECT=true         # requires mainnet + treasury
# X402_INVOICE_AMOUNT_JITTER is forced ON with detection
```

**Machine-payer HTTP surface (A2A / OpenAI-compat), mainnet.**
```bash
X402_ENABLED=true
X402_PAYMENT_RECIPIENT=0xYourTreasury…
CDP_API_KEY_ID=…                        # mainnet facilitator
CDP_API_KEY_SECRET=…
```

**Agent pays for resources.**
```bash
AGENT_WALLET_ENABLED=true
AGENT_WALLET_MASTER_SEED=<secret>
X402_CLIENT_ENABLED=true
WALLET_DAILY_CAP_USD=25                 # optional rolling budget on top of the per-tx guard
```

Turning subscriptions, the usage bridge, or 8004 feedback on additionally requires their
flags (`SUBSCRIPTIONS_ENABLED`, `USAGE_INVOICE_BRIDGE_ENABLED`,
`EIP8004_ENABLED`+`EIP8004_PAYMENT_FEEDBACK`) — see the caveats in their sections above.

---

## 13. Flag reference

`docs/CONFIGURATION.md` is the authoritative SSOT (each row has a code anchor); run
`polyrob doctor --flags` for the live runtime view. The complete money/crypto flag set,
with current defaults:

| Flag | Default | Purpose |
|---|---|---|
| `AGENT_WALLET_ENABLED` | OFF | Enable the agent wallet |
| `AGENT_WALLET_NETWORK` | `testnet` | Wallet network |
| `AGENT_WALLET_BACKEND` | `local_eoa` | Key backend |
| `AGENT_WALLET_MAX_PER_TX_USD` | `1000` | Per-tx loss guard (not a budget) |
| `AGENT_WALLET_OPERATIONAL_VENUE` | `treasury` | Venue same-chain spends sign with |
| `AGENT_WALLET_DERIVATION` | unset (`meta.json` wins; absent = legacy) | Recovery-hatch override for the key-derivation scheme (`legacy` \| `bip44`) |
| `WALLET_DAILY_CAP_USD` | unset | 24h rolling spend cap |
| `X402_CLIENT_ENABLED` | OFF | Agent pay-side (`x402_pay`) |
| `X402_ENABLED` | OFF | Machine-payer HTTP middleware |
| `X402_PAYMENT_RECIPIENT` | `''` | Treasury/recipient address |
| `X402_DEFAULT_CHAIN` | `base` | Default chain |
| `X402_FACILITATOR_URL` | `''` | Facilitator endpoint (receive-side) |
| `X402_PRICE_USD` / `X402_MAX_TOKENS_PER_REQUEST` / `X402_PRICE_MARKUP` | derived / `200000` / `2.0` | Machine-payer per-request price |
| `X402_INVOICE_ENABLED` | OFF | Agent invoicing |
| `X402_INVOICE_MAX_USD` / `X402_INVOICE_DAILY_MAX` | `50` / `10` | Invoice caps |
| `INVOICE_CARD_ENABLED` | OFF (ON local) | Branded QR invoice cards |
| `INVOICE_QR_STYLE` | `address` | `address` \| `eip681` |
| `PAYMENT_APPROVAL_MODE` | `approve` | `approve` (owner tap) \| `auto` (within-caps) |
| `APPROVAL_GRANT_TTL_HOURS` | `24` | One-shot post-timeout grant TTL |
| `X402_SETTLE_ONCHAIN_DETECT` | OFF | Facilitator-free on-chain USDC detection |
| `X402_INVOICE_AMOUNT_JITTER` | ON | Sub-cent uniqueness for detection |
| `X402_SETTLEMENT_WATCH_INTERVAL_SEC` | `60` | Settlement-watcher tick |
| `X402_SETTLEMENT_SCAN_MAX_SPAN` / `X402_SETTLEMENT_CONFIRMATIONS` | `5000` / `2` | On-chain scan bounds |
| `X402_PUBLIC_RATE_PER_WINDOW` / `X402_PUBLIC_RATE_WINDOW_SEC` / `X402_TRUSTED_PROXIES` | `20` / `60` / `''` | Public-endpoint rate limit |
| `SUBSCRIPTIONS_ENABLED` | OFF | Watchtower subscriptions |
| `WATCHTOWER_PRICE_USD` | `10.00` | Default monthly price |
| `SUBSCRIPTION_RENEWAL_LEAD_DAYS` / `SUBSCRIPTION_GRACE_DAYS` | `5` / `3` | Renewal + grace windows |
| `USAGE_INVOICE_BRIDGE_ENABLED` / `USAGE_INVOICE_MARKUP` | OFF / `1.0` | Usage→invoice draft |
| `EIP8004_ENABLED` / `EIP8004_PAYMENT_FEEDBACK` | OFF / OFF | ERC-8004 identity + payment-backed reputation (feedback also needs `X402_INVOICE_ENABLED`) |
| `EIP8004_ONCHAIN_ENABLED` | OFF | Flips the public `trustMode` claim to `onchain` (operator attestation only — no code registers on-chain) |
| `EIP8004_AGENT_PRIVATE_KEY` | unset — secret | 8004 feedback signing key; a SECOND key, not derived from the wallet seed, not carried by wallet migration |
| `ENABLE_AUTH` | OFF | Platform billing gate |
| `CREDIT_VALUE_USD` / `WELCOME_BONUS` | `0.01` / `100` | Credit economics |
| `CREDIT_SENTINEL_ENABLED` / `BILLING_FAILOVER_ENABLED` | ON / ON | Credit-death latch / provider failover |
| `DEPOSIT_MONITOR_ENABLED` / `PAYMENT_MASTER_SEED` | OFF / unset | Crypto deposit monitor + address derivation |
| `TREASURY_SWEEPER_ENABLED` / `TREASURY_ADDRESS` / `SWEEP_INTERVAL` | OFF / unset / `3600` | Sweeps deposit balances into the treasury (fund-moving; needs the flag AND the address) |
| `CRYPTO_TRADE_LIVE_ENABLED` | OFF | Master live-trade switch |
| `HYPERLIQUID_TRADING_ENABLED` / `POLYMARKET_TRADING_ENABLED` | OFF / OFF | Per-venue live trade |
| `HYPERLIQUID_TRADE_MAX_USD` / `POLYMARKET_TRADE_MAX_USD` | `5` / `5` | Per-venue trade caps |

---

## See also

- `docs/CONFIGURATION.md` — authoritative flag SSOT with code anchors.
- `modules/x402/README.md` — deep x402 module reference (protocol, tables, facilitator).
- `SECURITY.md` — the crypto/wallet/payment security posture.
- `docs/guide/deployment-postures.md` — how the money flags interact with deployment shapes.
