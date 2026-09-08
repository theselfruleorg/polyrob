# Payments, Wallet & Crypto

This guide is the single, complete reference for **every crypto- and money-related
capability** in POLYROB: the agent wallet, paying for resources (x402), getting paid
(invoicing / built-in ecommerce), on-chain settlement, watchtower subscriptions,
metering-to-invoice, the machine-payer HTTP surface, ERC-8004 reputation, platform
billing (credits), deposit addresses, on-chain token sight and guarded transfers,
the crypto trading tools, and the unified ledger.

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

**Crypto is the only rail today.** Payments are USDC on Base (via the
[x402 protocol](https://www.linuxfoundation.org/x402foundation), now a Linux
Foundation standard), with USDC-on-**Solana** receive as an opt-in second
settlement chain (`X402_SOLANA_SETTLE`). On-chain *trading* additionally spans
the chains the registry verifies (Base, Ethereum, Arbitrum, Polygon, Robinhood
Chain — §10) and Solana via Jupiter (§10.3). Fiat (Stripe) is a designed-for but
**deferred** extension. A **no-payments deployment degrades gracefully** — the
invoice tool is simply absent, nothing breaks.

**"OFF by default" is a promise about a fresh install, not a ceiling.** A fully
armed deployment (the reference bot runs one) turns on, step by step: the wallet
→ invoicing + on-chain detection → DeFi sight → EVM + Solana trading →
unattended goal-run trading → monitor-loop exits. Each step is its own flag and
its own widening; §13 and the table in §14 name what each one adds, and the caps
(not the flags) are the damage bound once you arm the unattended steps.

The five money organs and where they live:

| Organ | Package | What it is |
|---|---|---|
| **Agent treasury** (custody) | `core/wallet/` | The on-chain wallet the agent controls: keys, spend caps, audit |
| **Agent receivables** (invoicing) | `modules/x402/` | What the agent is owed: invoices, settlement, subscriptions |
| **Platform billing** (credits) | `modules/credits/` | What users owe the platform per LLM call; balances, deposits |
| **Metering** (measurement) | `usage_records` | Per-call cost measurement, tenant-scoped |
| **Accounting** (read-only join) | `unified_ledger.py` | Treasury (income / spend / pending / net) + Runtime compute cost, as two blocks that are never summed |

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
  `AGENT_WALLET_MAX_PER_TX_USD` (default `250`, a catastrophic loss-guard, *not* a
  budget — was `1000` before the 2026-08-22 hardening pass); `WALLET_DAILY_CAP_USD`
  (a 24h rolling cap, default `100` — the per-tx ceiling alone cannot stop a
  within-ceiling loop draining the treasury one ticket at a time; set
  `none`/`off`/`unlimited`/`disabled` to restore the old unbounded behaviour
  explicitly); per-venue 24h caps (`WALLET_VENUE_DAILY_CAP_<VENUE>_USD` — no
  cap unless set; the same `none`/`off`/`unlimited`/`disabled` sentinel is a
  no-op for a per-venue key, and a malformed or negative value RAISES naming
  the key, same as the two caps above). Owner
  **preferences** can only *tighten* these (`/config set budget.wallet_daily_usd …`
  min-merges with the env cap — never above the resolved default). An idempotency
  replay-guard prevents a retried step from double-paying.
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
| `x402_fetch` | **yes** | required | Fetch, auto-paying up to a caller-set `max_amount_usd` |
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

Both verbs handle POST-only paywalls (JSON-RPC, A2A `/v1`) via `method`/`body`,
and read a challenge from the **response body** as well as the
`PAYMENT-REQUIRED` header — the spec puts the requirements in the body, and
POLYROB's own middleware emits exactly that shape. All decoding delegates to the
one client-side parser (`RealX402Client._decode_challenge`), so the payer and the
prober can never drift apart.

Guardrails: bounded to 50 targets at 8 concurrent, and every agent-supplied URL
goes through the same SSRF validator `web_fetch` uses (cloud metadata and RFC1918
stay shut). A missing price is reported as unknown, never as `$0`.

The `x402_fetch` flow (`tools/x402/service.py`, `real_client.py`):

1. Owner pause check (`autonomy_halted()` — the `all` scope of the 031 pause record — refuses all spend, fail-closed).
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

## 10. On-chain token operations (`tools/defi/`, `core/wallet/tx_guard.py`)

Two tools give the agent eyes on-chain and, separately, a guarded way to move value.
They are split deliberately: **reading is a different risk class from spending**, so
they are different tools behind different flags, and enabling sight never implies
enabling spend.

### 10.1 `defi_data` — read-only token sight (`DEFI_DATA_ENABLED`, default OFF)

Nine read-only actions, **multichain**: the chain registry (`core/wallet/chains.py`)
is the one table of supported chains — Base, Ethereum, Arbitrum, Polygon, Robinhood
Chain, and **Solana** (reads; base58 addresses, no checksum). **No signer is
constructed and nothing is broadcast**, so this tool cannot move value:

| Action | What it does | Chains |
|---|---|---|
| `token_resolve` | Ranked *candidate* contract addresses for a ticker — a discovery aid, never a resolver | all, incl. Solana |
| `token_info` | On-chain identity + price + liquidity + safety screen for one contract address | all, incl. Solana |
| `price` | USD price for one contract address | all, incl. Solana |
| `swap_quote` | Prices a swap through the *same* route seam `defi_trade.swap` uses — see what a trade would do for $0 | EVM |
| `portfolio` | The agent's own holdings on one chain, USD-valued, with explicit coverage — includes the wallet's own address in the header | all, incl. Solana |
| `reconcile` | Diffs the position ledger's `## Open positions` table against actual on-chain balances — see §10.4 | EVM (Solana: explicit refusal, do it by hand) |
| `new_pools` | Newest-indexed pools (the fresh-launch frontier), unscreened | all, incl. Solana |
| `trending` | Pools an indexer ranks trending, unscreened | all, incl. Solana |
| `contract_read` | Raw `eth_call` (returns raw hex, gas- and size-capped) | EVM |

Two rules run through the whole tier:

**An address is the only identity.** `token_info` / `price` / `contract_read` reject a
ticker outright. `token_resolve` is the single verb that accepts a symbol, and it
returns *every* candidate and never picks — even when there is exactly one match today,
because one match is not a safety property. Binding a symbol to a contract is the
primary injection surface for an agent that reads web pages, and liquidity ranking is
purchasable, so a deeper pool does not mean genuine. **You choose the address.**

**Unknown is never zero.** An unreadable price renders `unknown` (never `$0.00`); an
unreachable safety screen renders `UNSCREENED` and never contains the word "safe"; a
balance that failed to read is listed separately as unknown rather than as a zero
holding. Only high-confidence prices enter a portfolio total — low-confidence and
unpriceable positions are listed under "unvalued — deliberately excluded" so an
attacker-seeded pool cannot inflate the headline figure you read.

Token metadata (`decimals` above all) is **pinned** from a canonical list for verified
tokens and **frozen on first sight** otherwise; a later differing read is surfaced as
`metadata_changed` rather than accepted. `decimals` denominates every valuation and, at
§10.2, the delta assertions the security model rests on — a token reporting 6 at
simulation and 18 at execution would break it.

Results are untrusted-wrapped: a token's `name`/`symbol` are chosen by whoever deployed
the contract, so DeFi reads are an injection inlet exactly like a fetched page.
`portfolio` alone is gated while a session is correspondent-tainted (own holdings are
pre-drain reconnaissance); the impersonal verbs stay available.

Without `ALCHEMY_API_KEY` the tool cannot *enumerate* holdings and reports
`coverage: partial`, naming the addresses it scanned and stating outright that a token
outside that set is invisible. The indexer is an upgrade path, never a requirement.

> ⚠️ `defi_data` is deliberately **not** in the `POLYROB_LOCAL` safe group, unlike the
> other interactive read tools. Production runs `POLYROB_LOCAL=1` beside a live mainnet
> wallet, so joining that group would switch this on for the live agent at the next
> deploy. Enabling it is an explicit operator decision.

### 10.2 `defi_trade` — value movement behind a transaction guard (`DEFI_TRADE_ENABLED`, default OFF)

**This one can move real funds.** Five verbs, every one defaulting `dry_run=true`
so moving funds requires saying so explicitly:

| Verb | What it does |
|---|---|
| `transfer` | Send tokens to an address, bounded by a declared `max_spend_usd` |
| `approve_token` | Grant an EXACT allowance to a named spender (unlimited approvals are refused outright) |
| `revoke_approval` | Set an allowance back to zero — retire the standing claim |
| `swap` | Swap through the route seam: Uniswap V3 from pinned addresses FIRST, an operator-named aggregator only as fallback |
| `solana_swap` | Swap SPL tokens via Jupiter on Solana — its own mirrored guard stack, §10.3 |

**The swap route is chosen conservatively.** Uniswap V3 calldata built locally
from pinned contract addresses is the strongest trust position, so it is tried
first wherever a chain has a verified deployment. A third-party aggregator
(currently Li.Fi) is consulted only when local construction finds no pool AND an
operator has named it (`DEFI_ROUTE_AGGREGATOR`, default off) — what makes opaque
aggregator calldata admissible is that the guard never reads calldata: it
simulates and asserts the observed deltas. Every swap additionally passes: a
quote-freshness bound, `amount_out_min > 0`, an allowance precheck, a slippage
bound (`slippage_bps`, default `DEFI_MAX_SLIPPAGE_BPS` = 100), and a
**route-sanity check** — the route's implied price is compared against an
independent source and a disagreement above `DEFI_ROUTE_DRIFT_MAX_PCT` (default
3%, hard ceiling 25%) REFUSES to execute rather than narrating.

**Exit untying (2026-08-26).** Two mechanisms keep fired stop rules executable
without weakening entries: a sell of a *held* token that no source can price is
valued at the simulation's **measured quote-asset inflow** (the receipt is
exact), and `DEFI_MONITOR_EXITS` (default OFF) lets a forged main-agent turn —
the self-wake monitor loop — run **EXIT-shaped operations only**: revoke,
exit-bounded approve, or a sell of a held token into the chain's quote asset
with the measured inflow asserted post-simulation. Entries, transfers and leaf
turns still refuse; all caps still apply. An approve/revoke records $0 against
the daily cap (the grant is still headroom-checked before landing; value is
recorded once, on the swap), and cap arithmetic runs in cents.

Every call routes through `core/wallet/tx_guard.py`, the single choke point: **no
value-moving transaction is broadcast without `Decision(allowed=True)`.** The bound is
not "we only wrote safe verbs" — that stops being a security property the moment the
agent supplies its own calldata. It is: **simulate the transaction, measure its observed
asset and allowance deltas, assert them against a declared intent, and refuse on any
disagreement or any probe failure.** Effects are *measured*, never inferred from the
caller's own calldata — checking a declaration against itself is no check at all.

Nine ordered gates, every one fail-closed:

1. **Owner pause** (`polyrob autonomy pause` / `/pause`, the 031 record) — a probe failure counts as paused.
2. **Turn origin** — a forged, self-wake, delegation-result, delegated-leaf or
   autonomous turn is refused, and an *unprovable* origin refuses.
3. **Structural** — zero amount, zero/burn destination.
4. **RPC trust** — refuses to arm on the shared public endpoint. Simulation, deltas and
   caps all read from the RPC, so it cannot be the trust anchor for moving money; pin
   `DEFI_EVM_RPC_BASE`.
5. **Simulation trustworthiness** — a revert, an RPC error or an unreadable balance
   refuses. Simulation runs as one `eth_simulateV1` bundle (`[reads, tx, reads]`) so
   state carries between calls and the deltas are real; if the node does not support it,
   the guard refuses rather than falling back.
6. **Delta assertion** — outflow ≤ declared, and a **measured-zero outflow on a declared
   send refuses** (zero is never a cheap transfer; it is an unmeasured one). An
   *undeclared* allowance grant refuses outright — a hidden `approve` is the one effect a
   USD cap cannot bound, because the drain happens in a later transaction the cap never
   sees.
7. **Pricing** — an unpriceable outflow or unknown decimals refuses. No cap can bound a
   number you do not have.
8. **Caps** — the per-tx ceiling, rolling daily cap and replay guard, held under a
   reservation across authorize → broadcast → record so two concurrent transfers cannot
   both clear a nearly-exhausted cap.
9. **Approval lane** — above `DEFI_AUTONOMOUS_MAX_USD` (default `$25`) the call returns
   `lane=owner_queue` with `allowed=False`. A queue lane is not an execute grant.

Result rendering is honest by construction: a refusal says **NOT SENT**; an
`owner_queue` lane says it did not execute; a reverted receipt says the transfer did
**not** happen but gas was spent (and the spend is still recorded — gas went and a nonce
was consumed); a receipt that never arrived says **BROADCAST BUT NOT CONFIRMED** and
warns against blind retry.

The signing perimeter is deliberately narrow. Transaction signing **refuses a
transaction with no `chainId`** (an unpinned chain is EIP-155 replay exposure across
every EVM chain the address exists on), chain identity is pinned from config and never
read from the RPC, and a preflight verifies the node actually serves the expected chain
before broadcasting. **Typed-data signing is kept off the money path entirely** — a
signed EIP-2612/Permit2 payload is not a transaction, so it would never reach the guard:
no simulation, no delta, no cap, no audit row, and the drain happens later in a
transaction the agent never sees.

`defi_trade` is classified `money` + `high_impact` + `delegate_blocked`, so it is
explicit-grant-only (the agent cannot self-serve it via `load_tool`), never reachable by
a delegated sub-agent, and never in the default toolset. All five verbs are in
`PAYMENT_APPROVAL_TOOLS` on the **spend** side — irreversible and self-custodial, with
no venue to dispute them, so never act-and-report. `DEFI_TIERED_SPEND_LANE` (default
OFF) can exempt a call whose *declared* ceiling sits within the autonomous limit from
the owner tap — dry runs and revokes are always exempt. All of this is ANDed with —
never a replacement for — `AGENT_WALLET_MAX_PER_TX_USD`, `WALLET_DAILY_CAP_USD`, the
owner pause and the `owner_queue` lane.

**What the mechanism cannot see**, stated rather than implied: the declaration and the
calldata can share an author (prompt injection authors both, and they will agree — the
turn-origin refusal and the caps are the defence there, not the delta assertion); a
permit signature never reaches the guard at all; and the RPC is the oracle for
everything the guard knows.

### 10.3 `solana_swap` — the Solana rail (`SOLANA_TRADE_ENABLED`, default OFF)

Solana has no allowances, so there is no approve/swap/revoke cycle — one verb,
routed through the **Jupiter** aggregator, with the EVM guard's step order
mirrored against `simulateTransaction` (there is no EVM transaction to hand to
`tx_guard`):

1. `SOLANA_TRADE_ENABLED` (default OFF; independent of `DEFI_TRADE_ENABLED`).
2. Owner pause (the `all` or `trading` scope), fail-closed.
3. Turn origin — forged/leaf refusal; `DEFI_AUTONOMOUS_TURN_TRADING` admits
   goal runs, `DEFI_MONITOR_EXITS` admits a sell of a held token into USDC.
4. Mint decimals READ from the chain (`getTokenSupply`) — unreadable refuses;
   a guessed denomination once mispriced a swap 1000× (wSOL is 9, not 6).
5. Jupiter's own baked minimum output must CLEAR your `slippage_bps` bound — a
   looser route is refused, never rewritten.
6. Simulation with the **token accounts** (not just the owner) and the
   pre-state, so the deltas are real. A simulation that did not run is not a
   simulation that passed: an empty delta set refuses.
7. The **authority taxonomy** in place of the allowance check: any delegate,
   ownership change, close authority or freeze the simulation reveals refuses
   outright — a swap grants nothing.
8. Rent is classified, not licensed: native SOL outflow beyond plausible rent
   refuses. The simulated outflow of the sold token may not exceed the
   declared amount.
9. Valuation: pinned USDC = $1.00 → high-confidence price → exit-bounded
   fallback → the measured USDC receipt for an unpriceable exit; then the
   declared `max_spend_usd` (in cents), then the **same PolicyGate** — per-tx
   ceiling, rolling daily cap, replay guard — and the `DEFI_AUTONOMOUS_MAX_USD`
   owner-queue lane. Confirmed swaps land in the same spend audit.
10. Broadcast requires a pinned `DEFI_SOLANA_RPC` (dry runs work unpinned); the
    signer refuses a foreign fee payer.

⚠️ Solana addresses are **base58 and case-sensitive with no checksum** — a
mistyped address is a valid different account. The wallet derives one Solana
address from the same seed (`m/44'/501'/0'/0'`, Phantom-compatible); see it via
`polyrob wallet`, `x402_wallet_status`, or the `portfolio(chain=solana)` header,
and fund it with SOL for fees before trading.

### 10.4 `defi_data.reconcile` — the ledger⟷chain check

Born from a real incident: the agent kept a markdown position ledger, its table
and its run-log diverged, and it published a false "book flat" claim while three
positions sat open on-chain. `reconcile(chain, ledger_path)` is the mechanical
answer — a server-side comparison no step budget or partial read can dilute:

- parses the ledger's `## Open positions` **table** (state only, never the
  narrative), enumerates actual chain holdings through the same seams
  `portfolio` uses, and reports every disagreement in both directions — table
  rows the chain does not back, chain holdings the table does not explain,
  size mismatches;
- a failed read renders `UNVERIFIED`, never zero; quote asset and wrapped
  native are working capital; confidently-priced sub-$0.25 holdings classify
  as dust.

The treasury-trading posture runs reconcile as step 0 of every trading run and
again before any public claim. Solana is refused explicitly (compare the
`portfolio(chain=solana)` output by hand) until the rail-written position store
lands.

```bash
# Sight only — the safe posture to start from
DEFI_DATA_ENABLED=true
DEFI_EVM_RPC_BASE=https://base-mainnet.your-provider.example/v2/KEY
# ALCHEMY_API_KEY=…            # optional: complete portfolio enumeration

# Adding EVM spend — requires a pinned RPC; keep the autonomous ceiling low
DEFI_TRADE_ENABLED=true
DEFI_AUTONOMOUS_MAX_USD=5      # above this, every trade waits for an owner tap
WALLET_DAILY_CAP_USD=25
PAYMENT_APPROVAL_MODE=approve
# DEFI_ROUTE_AGGREGATOR=lifi   # optional: aggregator fallback for pools V3 can't reach

# Adding Solana spend — a separate arming decision
SOLANA_TRADE_ENABLED=true
DEFI_SOLANA_RPC=https://solana-mainnet.your-provider.example/v2/KEY

# Unattended (goal/cron) trading and monitor-loop exits — widenings, off by default
# DEFI_AUTONOMOUS_TURN_TRADING=true
# DEFI_MONITOR_EXITS=true
```

> ⚠️ **The caps are global and chain-blind.** `AGENT_WALLET_MAX_PER_TX_USD`,
> `WALLET_DAILY_CAP_USD` and `DEFI_AUTONOMOUS_MAX_USD` are one number across
> every chain (EVM and Solana share the same rolling daily bucket under
> `venue="defi"`). There are no per-chain caps yet; if you arm a second chain
> with a different ticket size, size the global caps for the riskier one.

---

## 11. Crypto trading tools (`tools/hyperliquid/`, `tools/polymarket/`)

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
- ⚠️ **Hyperliquid live trading currently refuses to arm on the polyrob-wallet
  path** (H4, 2026-08 audit): on that path the derived venue key fully owns the
  account it trades, so the venue's approveAgent withdrawal firewall does not
  exist — with `HYPERLIQUID_TRADING_ENABLED` set, the client refuses with an
  error naming H4 rather than trading behind a firewall the code does not
  provide. Delegated DB credentials (via `approve_agent`) are unaffected.
  `agent_status` reports the actual signer.
- **Polymarket needs its SDK installed** (`py-clob-client`); without it every
  Polymarket verb errors on import. Install it deliberately or leave the tool
  unloaded — the tool_id is never in a default toolset either way.

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
> `PAYMENT_APPROVAL_TOOLS`, but the owner pause (`polyrob autonomy pause` / `/pause`;
> the legacy `AUTONOMY_HALT` facet) freezes them too — during an incident, cancel open orders directly
> at the venue, not through the agent.

---

## 12. Accounting — the unified ledger (`modules/credits/unified_ledger.py`)

One read-only model joins **two ledgers that are never summed**, tenant-scoped,
each fail-open:

- **Treasury** — the agent's *own* money (USDC): `income_usd` / `spend_usd` /
  `pending_usd` / `net_usd` (where `net = income − spend`), drawn from settled and
  pending `x402_payment_requests` and the wallet audit (`wallet_spend` events).
- **Runtime cost** — the *owner's* compute spend: LLM `api_cost_usd` from
  `usage_records`, with call counts. It has **no** `net` — there is nothing to net
  compute cost against.

The two blocks stay separate on purpose: the agent's earnings and the owner's
provider bill are different money, and the legacy top-level `earned_usd` / `net_usd` /
`total_spend_usd` fields were removed with no alias so a straggler consumer fails
loudly rather than silently reading a merged figure. Owner-facing surfaces that read
it: the agent-callable `accounting` and `agent_status` actions, the CLI `/journey`
("Income" line) and `polyrob finance`, the webview `/finance` page, the owner digest's
Money line, and Telegram `/recap`.

---

## 13. Deployment recipes

**No payments (default).** Set nothing. The invoice tool is absent; `/finance` shows
zeros; nothing crypto runs.

**Tier 1 — self-contained receive, zero infra (2026-08-21).** Getting paid needs
only a wallet address: mint an invoice, the payer sends USDC, on-chain detection
confirms it. No domain, no cert, no HTTP server — the settlement watcher runs in
the agent process. With the agent wallet enabled the treasury auto-fills
(`X402_TREASURY_FROM_WALLET`, default ON), so the whole rail is:
```bash
AGENT_WALLET_ENABLED=true
AGENT_WALLET_MASTER_SEED=<secret>       # or run `polyrob wallet init`
X402_INVOICE_ENABLED=true               # default ON under AUTONOMY_MODE=autonomous
X402_SETTLE_ONCHAIN_DETECT=true         # default ON under AUTONOMY_MODE=autonomous
X402_DEFAULT_CHAIN=base-sepolia         # testnet first; flip to base after the dry run
INVOICE_CARD_ENABLED=true               # branded QR cards
PAYMENT_APPROVAL_MODE=approve           # tap to approve outward money
```
Detection scans `base` (mainnet) and `base-sepolia` (testnet); `polyrob doctor`
shows the resolved treasury and its source. Validate the full loop on
base-sepolia before flipping the chain to mainnet.

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
X402_SETTLE_ONCHAIN_DETECT=true         # scannable chain (base/base-sepolia) + treasury
# X402_INVOICE_AMOUNT_JITTER is forced ON with detection
```

**Tier 2 — machine-callable HTTPS endpoint, one owner command.** A public URL
that returns a real 402 needs a domain + TLS + the api app — the one part an
agent can never self-provision. On the box, as root, from a full checkout:
```bash
X402_HOST=agent.example.com bash scripts/setup_x402_endpoint.sh
```
It checks auth secrets (fail-closed), DNS, installs a deny-by-default nginx
vhost (only `/a2a`, `/v1`, `/api`, `/.well-known`, `/eip8004` are proxied),
obtains the certificate, writes the endpoint block into
`/etc/polyrob/polyrob.env`, starts `polyrob-x402-api.service` (loopback :9000),
and verifies a live 402 + the agent card.

The equivalent manual configuration:

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
WALLET_DAILY_CAP_USD=25                 # tighten the $100 default rolling budget, if you want less
```

Turning subscriptions, the usage bridge, or 8004 feedback on additionally requires their
flags (`SUBSCRIPTIONS_ENABLED`, `USAGE_INVOICE_BRIDGE_ENABLED`,
`EIP8004_ENABLED`+`EIP8004_PAYMENT_FEEDBACK`) — see the caveats in their sections above.

---

## 14. Flag reference

`docs/CONFIGURATION.md` is the authoritative SSOT (each row has a code anchor); run
`polyrob doctor --flags` for the live runtime view. The complete money/crypto flag set,
with current defaults:

| Flag | Default | Purpose |
|---|---|---|
| `AGENT_WALLET_ENABLED` | OFF | Enable the agent wallet |
| `AGENT_WALLET_NETWORK` | `testnet` | Wallet network |
| `AGENT_WALLET_BACKEND` | `local_eoa` | Key backend |
| `AGENT_WALLET_MAX_PER_TX_USD` | `250` | Per-tx loss guard (not a budget) |
| `AGENT_WALLET_OPERATIONAL_VENUE` | `treasury` | Venue same-chain spends sign with |
| `AGENT_WALLET_DERIVATION` | unset (`meta.json` wins; absent = legacy) | Recovery-hatch override for the key-derivation scheme (`legacy` \| `bip44`) |
| `WALLET_DAILY_CAP_USD` | `100` | 24h rolling spend cap (`none`/`off` disables it) |
| `X402_CLIENT_ENABLED` | OFF | Agent pay-side (`x402_pay`) |
| `X402_ENABLED` | OFF | Machine-payer HTTP middleware |
| `X402_PAYMENT_RECIPIENT` | `''` | Treasury/recipient address |
| `X402_TREASURY_FROM_WALLET` | **ON** | When `X402_PAYMENT_RECIPIENT` is empty and the agent wallet is on, the wallet's **treasury-venue** address becomes the x402 receive address. |
| `X402_SETTLEMENT_RPC` | `''` | Watcher-specific RPC pin for on-chain detection (any chain). The scan verifies the endpoint's `eth_chainId` matches the configured chain and refuses to scan on a mismatch. |
| `X402_DEFAULT_CHAIN` | `base` | Default chain |
| `X402_FACILITATOR_URL` | `''` | Facilitator endpoint (receive-side) |
| `X402_PRICE_USD` / `X402_MAX_TOKENS_PER_REQUEST` / `X402_PRICE_MARKUP` | derived / `200000` / `2.0` | Machine-payer per-request price |
| `X402_INVOICE_ENABLED` | OFF | Agent invoicing |
| `X402_INVOICE_MAX_USD` / `X402_INVOICE_DAILY_MAX` | `50` / `10` | Invoice caps |
| `INVOICE_CARD_ENABLED` | OFF (ON local) | Branded QR invoice cards |
| `INVOICE_QR_STYLE` | `address` | `address` \| `eip681` |
| `PAYMENT_APPROVAL_MODE` | `approve` | `approve` (owner tap) \| `auto` (within-caps) |
| `APPROVAL_GRANT_TTL_HOURS` | `24` | One-shot post-timeout grant TTL |
| `X402_SETTLE_ONCHAIN_DETECT` | OFF (ON under `AUTONOMY_MODE=autonomous`) | Facilitator-free on-chain USDC detection |
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
| `DEFI_DATA_ENABLED` | OFF | Read-only on-chain token sight (`defi_data`, 9 verbs incl. `reconcile`) — never in the local safe group |
| `DEFI_TRADE_ENABLED` | OFF | On-chain EVM money verbs (`defi_trade`: transfer/approve/revoke/swap) — can move real funds; needs a pinned RPC |
| `SOLANA_TRADE_ENABLED` | OFF | Arms `defi_trade.solana_swap` (Jupiter) — a separate decision from the EVM verbs |
| `DEFI_AUTONOMOUS_MAX_USD` | `25` | Per-tx ceiling below which a trade may execute autonomously; above it → `owner_queue` |
| `DEFI_AUTONOMOUS_TURN_TRADING` | OFF | Lets a goal/cron-dispatched run reach the money verbs (a real widening — size the daily cap as your loss bound) |
| `DEFI_MONITOR_EXITS` | OFF | Lets a forged main-agent (monitor-loop) turn run EXIT-shaped operations only |
| `DEFI_MAX_SLIPPAGE_BPS` | `100` | Default swap slippage bound (basis points) |
| `DEFI_ROUTE_DRIFT_MAX_PCT` | `3` (ceiling 25) | Route-vs-independent-price drift above this REFUSES the swap |
| `DEFI_ROUTE_AGGREGATOR` | unset | Names the aggregator fallback (`lifi`) for pools local V3 construction can't reach |
| `DEFI_TIERED_SPEND_LANE` | OFF | Exempts a within-ceiling declared spend from the owner tap (dry runs/revokes always exempt) |
| `DEFI_EVM_RPC_BASE` / `DEFI_EVM_RPC_<CHAIN>` | public default | Operator-pinned JSON-RPC per chain; `tx_guard` refuses to move funds on the shared public default |
| `DEFI_SOLANA_RPC` | public default | Pinned Solana RPC; `solana_swap` refuses to BROADCAST unpinned (dry runs/reads work) |
| `X402_SOLANA_SETTLE` | OFF | USDC-on-Solana invoice settlement (reference-key matching) in the settlement watcher |
| `X402_AUTONOMOUS_MAX_USD` | `1` | Per-payment ceiling below which `x402_fetch` runs act-and-report (deliberately far below the DeFi ceiling — x402 payments aren't simulated) |
| `ALCHEMY_API_KEY` | unset | Optional holdings indexer — upgrades EVM `portfolio` from an honest partial scan to complete (Solana enumerates completely without it) |
| `CRYPTO_TRADE_LIVE_ENABLED` | OFF | Master live-trade switch |
| `HYPERLIQUID_TRADING_ENABLED` / `POLYMARKET_TRADING_ENABLED` | OFF / OFF | Per-venue live trade |
| `HYPERLIQUID_TRADE_MAX_USD` / `POLYMARKET_TRADE_MAX_USD` | `5` / `5` | Per-venue trade caps |

---

## See also

- `docs/CONFIGURATION.md` — authoritative flag SSOT with code anchors.
- `modules/x402/README.md` — deep x402 module reference (protocol, tables, facilitator).
- `SECURITY.md` — the crypto/wallet/payment security posture.
- [security-model.md](security-model.md) — the layered trust model these gates sit in.
- `docs/guide/deployment-postures.md` — how the money flags interact with deployment shapes.
- `docs/examples.md` — a worked end-to-end money-loop example.
