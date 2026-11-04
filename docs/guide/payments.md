# Payments

This page is the hub for money in POLYROB: the safety model, getting paid
(invoicing and settlement), the assets an invoice can ask for, the metering bridge,
the machine-payer HTTP surface, ERC-8004 identity and reputation, platform billing
(credits), the ledger, and the order in which you arm each part. Two pages carry the
rest:

- [wallet.md](wallet.md) — the agent wallet, the signer, the spend caps and the audit,
  paying for resources with x402, and signing in to a site (`wallet_sign`).
- [onchain.md](onchain.md) — on-chain sight and value movement (`defi_data`,
  `defi_trade`, Solana, bridges, NFTs, deploys, contract calls, launchpads, dapps,
  liquidity, agent NFT accounts), the `/trade` mandate and the `markets` pack.

The HTTP routes are listed in [api.md](api.md#money-and-pricing).

> ⚠️ **Unaudited — use at your own risk.** These features have had **no independent
> security audit**. They handle real value on mainnet and are provided as-is with no
> warranty (see [LICENSE](../../LICENSE) and
> [SECURITY.md](../../SECURITY.md#crypto--wallet--payment-features)). **Everything here that
> can move value is OFF by default** (only the read-only `defi_data` sight is on).
> Evaluate on testnets first. `modules/x402/README.md` is the deep module reference;
> [`docs/CONFIGURATION.md`](../CONFIGURATION.md) owns every flag default.

---

## 1. The safety model (read first)

Four invariants hold across the whole surface:

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
3. **Money can't be minted by a stranger.** Outward money is approval-gated (under
   effective `AUTONOMY_MODE=autonomous` the receive side act-and-reports instead —
   [Approval](#24-approval--approve-vs-auto)); a correspondent's payment or message can
   never gain steering rights; a forged / self-wake / delegated-leaf turn can never reach
   a money-moving verb; caps apply everywhere.
4. **A payout needs the owner.** A spend whose shape can pay an address that is not the
   wallet, a pinned venue or a pinned own account goes to the owner's tap on every lane,
   whatever verb expresses it (a plain send, a generic contract call that declares
   nothing back, an endowed deploy, a page-authored native send). A number from a seller,
   an API or a bridge (a settle header, a hash list, a token's `symbol()`) is evidence the
   code checks, never a value it records.

**Crypto is the only rail today.** Payments are USDC on Base (via the
[x402 protocol](https://www.linuxfoundation.org/x402foundation), now a Linux
Foundation standard), with USDC-on-**Solana** receive as an opt-in second
settlement chain (`X402_SOLANA_SETTLE`). On-chain *trading* additionally spans
the chains the registry verifies (Base, Ethereum, Arbitrum, Polygon, Robinhood
Chain — see [onchain.md](onchain.md)) and Solana via Jupiter. Fiat (Stripe) is not
built. A **no-payments deployment degrades gracefully** — the invoice tool is simply
absent, nothing breaks.

**"OFF by default" is a promise about a fresh install, not a ceiling.** A fully
armed deployment turns on, step by step: the wallet → invoicing + on-chain detection
→ DeFi sight → EVM + Solana trading → unattended goal-run trading → monitor-loop
exits. Each step is its own flag and its own widening;
[Arming it, one step at a time](#9-arming-it-one-step-at-a-time) names what each one
adds, and the caps (not the flags) are the damage bound once you arm the unattended
steps.

The five money organs and where they live:

| Organ | Package | What it is |
|---|---|---|
| **Agent treasury** (custody) | `core/wallet/` | The on-chain wallet the agent controls: keys, spend caps, audit |
| **Agent receivables** (invoicing) | `modules/x402/` | What the agent is owed: invoices and settlement |
| **Platform billing** (credits) | `modules/credits/` | What users owe the platform per LLM call; balances, deposits |
| **Metering** (measurement) | `usage_records` | Per-call cost measurement, tenant-scoped |
| **Accounting** (read-only join) | `unified_ledger.py` | Treasury (income / spend / pending / net) + Runtime compute cost, as two blocks that are never summed |

---

## 2. Getting paid — invoicing and settlement (`modules/x402/`)

The agent can quote, invoice (as text **and** a branded QR image), get paid (USDC,
auto-detected), deliver, and account for it. Master flag `X402_INVOICE_ENABLED`
(default OFF) + a treasury address (`X402_PAYMENT_RECIPIENT`).

### 2.1 Create an invoice

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
- **No test-network invoice on a production rail.** When `X402_DEFAULT_CHAIN` is a
  production chain, an invoice on a test network or a test asset (`*-devnet`,
  `*-testnet`, `*-sepolia`) is refused: faucet tokens are not income. A devnet payment
  is never counted as income, and listings name the chain. Set `X402_DEFAULT_CHAIN` to a
  test network for a dev run.

`x402_request` is **approval-gated, leaf-blocked, and correspondent-taint-blocked.**

### 2.2 The invoice as a branded image

With `INVOICE_CARD_ENABLED` (default OFF, **ON under `POLYROB_LOCAL`**),
`modules/cards/cards.py::render_invoice_card` composes a branded PNG in **pure Pillow**
(never a headless browser): the agent's avatar (else the brand mark), amount, purpose, request id,
expiry, "billed to", a QR block, and pay instructions, using a shipped OFL font under
`assets/fonts/`. The QR payload (`modules/x402/artifact.py`) is controlled by
`INVOICE_QR_STYLE` (`address` default = the bare treasury address; `eip681` = a prefilled
`ethereum:<usdc>@<chain>/transfer?…` URI, auto-preferred when on-chain detection is on).
Rendering **fails open to text-only.**

### 2.3 Delivering it

The outbound-media leg turns `OutboundMessage.media` into a real attachment:
**Telegram photo** (`send_photo`), **email attachment**, or the agent-callable
`message(media_paths=[…])` tool (paths are workspace-confined and symlink-guarded).
Text-only surfaces deliver the text and note the omission honestly.

### 2.4 Approval — `approve` vs `auto`

`PAYMENT_APPROVAL_MODE` is the single owner-legible switch. It is `approve` by default,
and `auto` under effective `AUTONOMY_MODE=autonomous`; an explicit value wins in both
modes.

- **`approve`** — every payment request (receive and spend) goes through the durable,
  remotely approvable `owner_queue` provider (`tools/controller/approval_queue.py`): it
  records a durable ask, pushes one owner notification, and waits for the payment
  approval timeout. Approve from Telegram with `/approve tap-<id>` (or
  `polyrob owner pending`). A post-timeout approval becomes a one-shot grant
  (`APPROVAL_GRANT_TTL_HOURS`).
- **`auto`** — an invoice (`x402_request`, the receive side) runs at once within the
  invoice caps and notifies the owner after the fact. The spend side does not change:
  a venue order still waits for the owner's tap. Auto mode never widens a cap.

### 2.5 Three settlement paths

A pending invoice completes by any of:

1. **Owner attestation** — `polyrob owner settle <id> [--tx-hash]`.
2. **Payer-driven facilitator** — a machine payer hits
   `POST /api/x402/requests/{id}/pay` (the public endpoint runs the x402 facilitator
   verify+settle).
3. **On-chain detection** — gated `X402_SETTLE_ONCHAIN_DETECT` (a treasury
   address is required). ⚠️ **Silently inert unless `X402_INVOICE_ENABLED` is also on** — the
   settlement watcher only starts when the autonomy runtime sees `X402_INVOICE_ENABLED` on
   (`polyrob/app/autonomy_runtime.py`); `X402_SETTLE_ONCHAIN_DETECT` on its own starts no
   ticker and detects nothing. The settlement watcher (`modules/x402/settlement_watcher.py`,
   running on the autonomy-runtime ticker every `X402_SETTLEMENT_WATCH_INTERVAL_SEC`, default 60s)
   scans the treasury's incoming `Transfer` logs (`modules/x402/onchain_probe.py`) on
   each chain an invoice is owed on ([Payment assets](#3-payment-assets--what-the-treasury-can-be-paid-in)), keeps a
   per-treasury `settlement_scan` block checkpoint (bounded by
   `X402_SETTLEMENT_SCAN_MAX_SPAN`, confirmations `X402_SETTLEMENT_CONFIRMATIONS`),
   matches an incoming transfer to a pending invoice by **exact atomic amount,
   oldest-first**, and settles it. **This makes the human-payer loop facilitator-free**
   — a payer who just sends the invoiced token to the address on the invoice is
   detected. Safety:
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
> the invoice can end up `completed` (or held in `settling` — a submitted claim is never released
> on age; `polyrob owner invoices --stuck` lists it) while that same
> on-chain transfer is later picked up by the on-chain-detection scan, finds no PENDING
> invoice left to match, and emits a `payment_unmatched` owner notice/event (`tx_hash`,
> `from`, `amount_usd`, `block`, `treasury` — `modules/x402/settlement_scan.py::_notify_unmatched`).
> This is the expected, at-most-once-settlement shape of that race, not a stray extra
> payment: if the unmatched amount matches an already-`completed` invoice for the same
> payer/treasury, don't book it as new revenue — verify the on-chain transaction (`tx_hash`
> in the event) really is the SAME payment as the completed invoice, then **refund the
> duplicate** to the payer rather than double-counting it.

**Watchtower subscriptions** (`modules/x402/subscriptions.py`, `SUBSCRIPTIONS_ENABLED`):
no surface in this release creates a subscription; the CLI lists, cancels and
reactivates them (`polyrob owner sub list`, `polyrob owner sub cancel <id>`,
`polyrob owner sub reactivate <id>`). `reactivate` moves a `grace` or `suspended`
subscription back to `active` for one period from now, by your word: no payment is
taken or recorded. A `canceled` subscription stays canceled.

### 2.6 Selling — offers and orders

Say "sell …" in chat. The agent drafts an **offer** (what is sold, the price, what
the buyer gives, what is delivered, where it is listed), writes the steps it
follows for each order, and asks you for two decisions:

1. **Setup** (only when the offer needs a contract, files or an account):
   `/offer setup <id> <usd>` gives it one setup budget (a limit you state that the
   wallet does not check: your normal spend limits still apply).
2. **Go live**: `/offer live <id> <hash>` — the decision shows the price, where it
   is sold, the abilities each order uses, what one order may spend and the contract
   functions it may call. Confirm, and buyers can order.

Buyers order in the chats you list (`/offers`, `/buy <offer> field=value`,
`/order <id>`, `/order <id> cancel`, `/order <id> dispute <reason>` — answered by
code, never by the AI), over HTTP (`GET /api/offers`, `POST /api/offers/<id>/orders`,
then `GET /api/orders/<id>?token=…` for the status), as other agents and apps, or on
a marketplace the operator configures in `<data_home>/commerce_markets.json`.
**Email is never a way to buy.** Each order gets its own invoice; when the payment
arrives on the network, one goal run delivers the order inside its own budget and
only to the buyer's address. Deliver by a contract call (for example a mint): a
direct token or NFT send always waits for your approval, order by order.

Your verbs: `/offer` (list), `/offer show <id>`, `/offer orders`,
`/offer quote <order> <usd>` (a quote-priced order), `/offer resolve <order>
accept|refund` (a dispute), `/offer pause|resume|retire <id>`. Refunds are always
yours: refund the order's invoice to the buyer's refund address — never to the
address the payment came from.

---

## 3. Payment assets — what the treasury can be paid in

`core/payments/assets.py` is the one registry of payment assets. The invoice row, the
on-chain probe, the settlement scan, the public challenge and the EIP-681 URI all read
it. One row per asset, no default row: an unknown asset id resolves to `None` and the
caller refuses with the known ids echoed back.

**Pin an asset** (operator only — there is deliberately no agent action and no
chat verb that writes one):

```bash
polyrob wallet asset add --id demo --chain base \
  --address 0xYourToken --verify --min-amount 1000000000000000000
polyrob wallet asset list
polyrob wallet asset verify demo
```

`--verify` reads `decimals()` and `symbol()` on-chain **once** and freezes them.
A stablecoin symbol does not make an asset a dollar: only the pinned USDC and
stables of each network are priced at $1. `--verify` warns when the contract
calls itself `USDC` (or another stable) at an address that is not pinned; such an
asset needs a live quote, `--min-amount` and `--liquidity-floor`.
A later differing read reports `metadata_changed` and leaves the pin alone:
decimals denominate every amount comparison the settlement scan makes, so a
token that reports 6 at mint and 18 at settlement must not be able to move the
value the money math reads.

### Two rails, and why the difference matters

| rail | who settles it | which assets |
|---|---|---|
| `facilitator` | the x402 facilitator, via `POST /api/x402/requests/{id}/pay` | only what `fastapi_x402` knows — in practice USDC on Base |
| `onchain_scan` | the settlement watcher, from a plain transfer into the treasury | anything you pin |
| `svm_reference` | the Solana pass, matching a unique Solana Pay reference | the Solana USDC rows |

⚠️ An `onchain_scan` invoice serves a **direct-transfer** challenge — the token,
the chain id, the raw amount, the treasury and an EIP-681 URI — and carries **no
`accepts` block**. An `accepts` block asks the payer to sign a payment
authorization, and for an asset no facilitator knows nobody would ever verify
that signature, while the payer would reasonably believe they had paid. `/pay`
refuses such an invoice with the transfer instructions instead.

### What settlement matches on

`(recipient, asset_address, amount_raw)` — exact integers, never a USD float. A $1
payment in another token can never settle a $1 USDC invoice.

⚠️ **The uniqueness that match depends on is the producer's job.** For an agent
invoice it comes from the amount jitter (a sub-cent USD nudge). For a producer
that PINS its raw amount — a paid room action — the jitter cannot help: the
pinned integer is written verbatim, so the dollars moved and the payable integer
did not. Such a producer is separated by a **raw-unit** nudge instead, spanning
every payable kind, because the matcher itself is kind-blind. Two offers at the
same price in one room therefore never share an amount.

The configured chain's own asset is scanned on **every** tick, owed or not. Every
other asset is scanned only while something is owed in it. The difference is
deliberate: scanning only what is owed would silence `payment_unmatched`, and "no
unmatched payments" must never come to mean "I was not looking".

⚠️ Pin an asset **before** inviting payment in it. The first tick that sees a new
asset seeds its checkpoint near the chain head and scans nothing — a transfer
that already landed is not enumerated. The watcher logs this the one time it
happens.

### Paid room actions

A paid room action (a `room_action` invoice) is minted through the same
`create_payment_request` an agent invoice uses, so every filter that decides which rows
settle, notify or get scanned spans both kinds (`invoicing.PAYABLE_KINDS`). It differs in
two ways: it is exempt from `X402_INVOICE_DAILY_MAX` (the room has its own per-payer and
per-target caps), and its settlement does not wake a session — it applies the effect the
payer bought and tells the room. It needs `X402_SETTLE_ONCHAIN_DETECT` as well as
`X402_INVOICE_ENABLED`, because on-chain detection is the only way a room payment is
noticed; an offer refuses before the mint when either is off. The full rail is in
[groups.md](groups.md).

---

## 4. Metering → invoice bridge (`modules/credits/usage_rollup.py`)

Turn measured LLM usage into a *draft* invoice. Gated `USAGE_INVOICE_BRIDGE_ENABLED`
(default OFF). A tenant-scoped `usage_rollup(user_id, session_id?, since?)` sums
`api_cost_usd` from `usage_records`; the `usage_summary` read action returns it and can
propose an invoice payload (amount = cost × `USAGE_INVOICE_MARKUP`, default `1.0`).
**The bridge only suggests** — the agent must still fire the approval-gated
`x402_request`; the bridge never mints a payment request itself. `usage_summary` is
anon-refused and correspondent-taint-denied (cost data is not exposed to a tainted turn).

---

## 5. The machine-payer HTTP surface (x402 middleware)

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

## 6. On-chain identity and payment-backed reputation — ERC-8004 (`modules/eip8004/`)

ERC-8004 is the **trust/discovery** layer (who is this agent, can I trust it), *not* a
payment rail — it composes with x402 (how do I pay it). Gated `EIP8004_ENABLED`
(default OFF; discovery-only when off). The module implements the Trustless Agents
standard: an Identity Registry (a registration file linking the A2A card + wallet + x402
pricing endpoint), a Reputation Registry, and a Validation Registry, served at
`/eip8004/*`.

### 6.1 Registering the identity on-chain

Gated `EIP8004_REGISTER_ENABLED` (default OFF). The agent mints its **own** identity.
`defi_trade.register_agent` calls a pinned
Identity Registry — an ERC-721 collection, so the returned `agentId` is a token your
wallet owns — and `defi_trade.set_agent_uri` republishes the file afterwards without
minting anything. Both are money verbs on the capped lane
([`defi_trade`](onchain.md#defi_trade--value-movement-behind-a-transaction-guard)): the guard prices the
fee, holds it to the declared `max_spend_usd`, and sends anything above
`DEFI_AUTONOMOUS_MAX_USD` to the owner queue. The owner runs them with `/identity register on
<chain> [go]` and `/identity set-uri on <chain> [id <agentId>] [go]` in chat, and
`polyrob identity register --chain <chain>` / `polyrob identity set-uri <agentId> --chain
<chain>` in the terminal (both simulate by default; `--execute` broadcasts).

`EIP8004_AGENT_URI_MODE` decides where the registration file lives. `auto` (the default)
publishes a hosted URL when `A2A_BASE_URL` is a public host and otherwise inlines the
whole file as a base64 `data:` URI — **so registering needs no hosting, no domain and no
IPFS account**. `data` always inlines; `hosted` refuses unless a public URL exists. An
oversized document is refused, never truncated.

> ⚠️ **Registration is permanent, public, and not idempotent.** A second call mints a
> second token and splits the identity between two `agentId`s, neither authoritative.
> The verb therefore reads the chain first and refuses if this wallet already holds one;
> a failed read also refuses rather than reading as "not registered". Once a
> registration is confirmed, the served file carries it as
> `attestation: "verified"` — evidence from a transaction this code signed, not a claim.

### 6.2 Payment-backed reputation

With `EIP8004_PAYMENT_FEEDBACK` (default OFF), a settled invoice becomes a
**verified-purchase** signal — but ⚠️ **only if `X402_INVOICE_ENABLED` is also on.** The
feedback offer is raised from inside the settlement watcher's settle path
(`SettlementWatcher._maybe_offer_payment_feedback`), and — same as
`X402_SETTLE_ONCHAIN_DETECT` above — the watcher itself only runs when
`X402_INVOICE_ENABLED` is on; `EIP8004_PAYMENT_FEEDBACK=true` alone offers nothing.
On settlement of a correspondent-linked invoice the agent
offers the payer a `ProofOfPayment`-backed feedback **authorization** (it never submits
feedback on the payer's behalf). `submit_feedback` verifies the proof against the ledger
— the invoice exists and is settled, the proof's `toAddress` equals the treasury, the
`txHash` hasn't already backed a feedback (replay guard), and the caller's `agent_id`
matches the EIP-712-signed authorization. This is the anti-sybil signal 8004 was designed
for.

> **Status:** the **Reputation** side is a local simulation — the `ReputationManager`
> stores feedback locally and writes to no on-chain Reputation Registry. Keep
> `EIP8004_PAYMENT_FEEDBACK` off outside evaluation. The **Identity** side (6.1 above)
> is real: it signs and broadcasts.

---

## 7. Platform billing — credits (`modules/credits/`)

Separate from the agent's own money: this is what *users* owe *the platform* for LLM
calls. The real gate is `ENABLE_AUTH` (off = no billing service registered at all).

Access comes from the account tier: `free_access` (an administrator grant), `x402`
(pay per request) or `admin`. A `free` account has none. No tier is granted by token
or NFT ownership.

- **Metering:** each LLM call becomes a `usage_records` row (keyed by `user_id` +
  `session_id`), with cost from the model registry including cached-input and cache-write
  pricing. A stable `request_id` column dedupes a retried bill of the same completion.
  Credits are deducted fail-fast (`InsufficientCreditsError` halts on depletion) unless
  `CHAT_SKIP_CREDIT_CHECK` (ON) skips the chat path. `CREDIT_VALUE_USD` = `$0.01`,
  `WELCOME_BONUS` = `0` (automatic grants are opt-in; new wallets are not unique people).
  - **Operational note:** `usage_records.user_id` has a foreign key to `user_profiles`.
    A headless single-owner deployment **seeds an owner `user_profiles` row at
    startup** (`ensure_owner_profile`) so metering actually persists — without it, every
    metering write fails the FK and spend reads as a false `$0`.
- **Resilience:** `CREDIT_SENTINEL_ENABLED` (ON) latches on credit-death (402 / quota)
  and pauses dispatch with one notice; `BILLING_FAILOVER_ENABLED` (ON) tries provider
  fallback on a billing/quota error before halting.
- **Deposit addresses** (`api/payment_endpoints.py`, strict JWT): each user gets a
  crypto deposit address derived from `PAYMENT_MASTER_SEED`, plus balance, transaction
  and deposit reads; the routes are listed in [api.md](api.md#money-and-pricing).
  On-chain deposits are watched by an out-of-band monitor
  (`DEPOSIT_MONITOR_ENABLED`, default OFF; ETH price via an oracle bounded by
  `ETH_PRICE_USD_MAX`); there is no in-band credit-purchase POST.
- **Treasury sweeper** (`modules/payments/treasury_sweeper.py`): sweeps per-user deposit
  balances into `TREASURY_ADDRESS` on an interval (`SWEEP_INTERVAL`, default 3600s). It
  **signs and broadcasts real fund-moving transactions**, so it has its own dedicated
  switch, `TREASURY_SWEEPER_ENABLED` (default **false**). Config presence alone
  (`TREASURY_ADDRESS` + a master seed + `ENABLE_AUTH`) never starts it: with the flag off
  it logs that it is disabled and moves nothing.

---

## 8. Accounting — the unified ledger (`modules/credits/unified_ledger.py`)

One read-only model joins **two ledgers that are never summed**, tenant-scoped,
each fail-open:

- **Treasury** — the agent's *own* money (USDC): `income_usd` / `spend_usd` /
  `pending_usd` / `net_usd` (where `net = income − spend`), drawn from settled and
  pending `x402_payment_requests` and the wallet audit (`wallet_spend` events).
- **Runtime cost** — the *owner's* compute spend: LLM `api_cost_usd` from
  `usage_records`, with call counts. It has **no** `net` — there is nothing to net
  compute cost against.

The two blocks stay separate on purpose: the agent's income and the owner's provider
bill are different money, so the ledger has no merged total. Owner-facing surfaces
that read it: the agent-callable `accounting` and `agent_status` actions,
`polyrob journey` / `/journey` ("Income" line) and `polyrob finance`, the console's
Money page, the owner digest's Money line, and Telegram `/recap`.

---

## 9. Arming it, one step at a time

Each step below is its own decision and its own widening. Stop at the one you need;
nothing later is implied by anything earlier. Every step is additive to the one above
it, and the caps — not the flags — are the damage bound once you reach step 5.

**Step 0 — no payments (the default).** Set nothing. The invoice tool is absent, the
money surfaces show zeros, and nothing that can move value runs. Only the read-only
`defi_data` sight is on.

**Step 1 — receive, with no infrastructure at all.** Getting paid needs only a wallet
address: mint an invoice, the payer sends USDC, on-chain detection confirms it. No
domain, no certificate, no HTTP server — the settlement watcher runs inside the agent
process. With the wallet on, the treasury auto-fills (`X402_TREASURY_FROM_WALLET`).

```bash
AGENT_WALLET_ENABLED=true
AGENT_WALLET_MASTER_SEED=<secret>       # or run `polyrob wallet init`
# posture members ride ONE line; X402_SETTLE_ONCHAIN_DETECT forces X402_INVOICE_AMOUNT_JITTER on,
# INVOICE_CARD_ENABLED attaches branded QR cards
POLYROB_OVERRIDES=X402_INVOICE_ENABLED=true,X402_SETTLE_ONCHAIN_DETECT=true,INVOICE_CARD_ENABLED=true
X402_DEFAULT_CHAIN=base-sepolia         # testnet first; flip to base after the dry run
PAYMENT_APPROVAL_MODE=approve           # tap to approve outward money
```

Detection scans every EVM chain an invoice is owed on (the chain registry, plus
base-sepolia), and Solana under `X402_SOLANA_SETTLE`; `polyrob doctor` shows the
resolved treasury and where it came from. Validate the whole loop on base-sepolia, then set
`AGENT_WALLET_NETWORK=mainnet` and `X402_DEFAULT_CHAIN=base`. To invoice with **no agent
wallet**, drop the two wallet lines and set `X402_PAYMENT_RECIPIENT=0xYourTreasury…`
instead.

**Step 2 — a machine-callable HTTPS endpoint.** A public URL that returns a real 402
needs a domain, TLS and the api app — the one part an agent can never self-provision.
POLYROB's side of it is three flags:

```bash
X402_ENABLED=true
X402_PAYMENT_RECIPIENT=0xYourTreasury…
CDP_API_KEY_ID=…                        # mainnet facilitator credentials
CDP_API_KEY_SECRET=…
```

The serving side is yours to stand up once, as root on the box: point DNS at the host,
run `polyrob serve` on loopback under a service manager, obtain a certificate, and put
a **deny-by-default** reverse proxy in front of it that forwards only `/a2a`, `/v1`,
`/api`, `/.well-known` and `/eip8004`. Then verify it end to end — a `curl` of
`POST /a2a/rpc` with no payment must return a real `402` carrying a parseable
challenge, and `/.well-known/agent.json` must answer. `x402_probe` scores your own
endpoint out of 5 and names what is missing; see [self-hosting.md](self-hosting.md)
for the deployment shapes.

**Step 3 — let the agent pay for resources.**

```bash
AGENT_WALLET_ENABLED=true
AGENT_WALLET_MASTER_SEED=<secret>
X402_CLIENT_ENABLED=true
WALLET_DAILY_CAP_USD=25                 # tighten the $100 default rolling budget
```

**Step 4 — on-chain sight.** Read-only, no signer, nothing broadcast. `defi_data` is
already on; pin your own RPC so its reads do not depend on a shared public endpoint.
This is the safe place to sit while you learn what the agent does with the data
([onchain.md](onchain.md#defi_data--read-only-token-sight)).

```bash
DEFI_EVM_RPC_BASE=https://base-mainnet.your-provider.example/v2/KEY
# ALCHEMY_API_KEY=…                     # optional: complete portfolio enumeration
```

**Step 5 — spend on-chain.** Requires a pinned RPC; `tx_guard` refuses to move funds on
the shared public endpoint. Keep the autonomous ceiling low to start
([onchain.md](onchain.md#defi_trade--value-movement-behind-a-transaction-guard)).

```bash
DEFI_TRADE_ENABLED=true
DEFI_AUTONOMOUS_MAX_USD=5               # above this, a trade the agent starts waits for an owner tap
WALLET_DAILY_CAP_USD=25
PAYMENT_APPROVAL_MODE=approve
# DEFI_ROUTE_AGGREGATOR=lifi            # optional aggregator fallback

# Solana is a separate arming decision
SOLANA_TRADE_ENABLED=true
DEFI_SOLANA_RPC=https://solana-mainnet.your-provider.example/v2/KEY
```

Each further capability is its own flag on top of this:
`DEFI_BRIDGE_ENABLED` ([bridges](onchain.md#bridging-between-chains)),
`NFT_TOOLS_ENABLED` ([NFTs](onchain.md#nfts)),
`DEFI_DEPLOY_ENABLED` ([deploys](onchain.md#deploying-a-contract)),
`DEFI_CALL_ENABLED` ([contract calls](onchain.md#calling-any-contract)),
`LAUNCHPAD_ENABLED` ([launchpads](onchain.md#launchpads)),
`DAPP_BROWSER_ENABLED` ([dapps](onchain.md#using-a-web-dapp)),
`DEFI_LIQUIDITY_ENABLED` ([liquidity](onchain.md#pool-deposits--liquidity)),
`AGENT_NFT_ENABLED` ([agent NFT accounts](onchain.md#agent-nft-accounts)).

**Step 6 — unattended.** These let a run you are not watching reach the money verbs.
Size `WALLET_DAILY_CAP_USD` as your loss bound before setting any of them.

```bash
# DEFI_AGENT_AUTONOMY=true              # put the defi rail in the autonomous grant
# DEFI_AUTONOMOUS_TURN_TRADING=true     # a goal/cron run may reach the money verbs
# DEFI_TIERED_SPEND_LANE=true           # a within-ceiling spend runs and reports, no tap
# DEFI_MONITOR_EXITS=true               # a monitor-loop turn may run EXIT-shaped ops only
```

These keys add up to the **money regime** (`core/config_policy/money_regime.py`), one
of three: `supervised` (`AUTONOMY_MODE` is not effectively `autonomous`; no goal or cron
run moves funds), `autonomous` (the agent runs its own work, money not armed) and
`armed`. `armed` needs effective `AUTONOMY_MODE=autonomous` plus `DEFI_AGENT_AUTONOMY`,
`DEFI_TRADE_ENABLED`, `DEFI_AUTONOMOUS_TURN_TRADING`, `DEFI_TIERED_SPEND_LANE` and a
daily cap. Without the tiered lane a within-ceiling unattended spend still waits on a
tap that nobody gives. The posture card (`polyrob doctor`, `/status`, `/autonomy`)
names the regime and any missing key.

`DEFI_AGENT_AUTONOMY` is what makes the rail *reachable* — it adds `defi_data`,
`defi_trade`, `launchpad` and `dapp_browser` to the autonomous grant, including the
owner's own chat toolset, so asking the agent to trade stops answering "no such verb".
It is only meaningful under effective `AUTONOMY_MODE=autonomous`, and once armed a goal
the agent writes itself can carry `defi_trade`. **The bound is the cap, not the
toolset**: every verb still simulates and asserts its own deltas, the per-transaction
ceiling and rolling daily cap still apply, a correspondent-tainted session still cannot
reach it, and anything over `DEFI_AUTONOMOUS_MAX_USD` still routes to the owner queue.
Reaching a capability and arming it are different decisions — this flag also puts
`x402_pay` in the grant when that tool exists ([wallet.md](wallet.md#who-may-pay):
micro-payments only without a tap),
but no host tool, and `launchpad`/`dapp_browser` stay behind their own
switches.

**What you will be told.** Every broadcast reaches you, on a lane no delivery limit
silences: a BROADCAST notice (verb, network, amounts, USD value, tx hash, the lane that
allowed it, the daily-cap headroom it consumed) and a SETTLED notice with the measured
outcome — confirmed, reverted, arrived or in flight. The BROADCAST line waits
`TX_NOTIFY_SENT_HOLD_SEC` (default 60 s) for its settlement: a transaction that settles
inside the wait sends ONE line that carries both sets of facts, and one still unsettled
after it gets its own BROADCAST line (`0` always sends two). It is deliberately not stopped by the owner pause: a report about funds
already in flight is exactly what an owner who just stopped everything needs. A dry run
never notifies, and an unknown value renders `unknown`, never `$0.00`.
`WALLET_CONTEXT_VISIBLE` (default **on**) puts the agent's own balances into every
turn's health note, read from a cache the bridge watcher refreshes — never a network
read on the turn. An unreadable chain renders `unknown` and raises a health item; a
stale snapshot is labelled stale.

The usage bridge and ERC-8004 feedback each need their own flag
(`USAGE_INVOICE_BRIDGE_ENABLED`, `EIP8004_ENABLED` + `EIP8004_PAYMENT_FEEDBACK`) — read
the caveats in their sections first.

---

## 10. Flag reference

[`docs/CONFIGURATION.md`](../CONFIGURATION.md) owns every flag name, default and code
anchor. Four groups cover this page:

- [Billing / x402 / wallet](../CONFIGURATION.md#billing--x402--wallet) — the wallet, its
  caps, x402 in and out, invoicing, settlement, credits.
- [DeFi / on-chain trading](../CONFIGURATION.md#defi--on-chain-trading) — `defi_data`,
  `defi_trade`, the RPC pins, the autonomous ceiling, deploys, the launchpad, the dapp
  wallet, ERC-8004 identity.
- [Crypto trading (Polymarket / Hyperliquid)](../CONFIGURATION.md#crypto-trading-polymarket--hyperliquid) —
  the venue tools and their per-venue caps.
- [Posture ↔ billing](../CONFIGURATION.md#posture--billing) — which of the above
  a deployment posture turns on for you.

On a running box the resolved value and where it came from are one command away:

```bash
polyrob doctor --flags --search WALLET
polyrob doctor --flags --search X402_
polyrob doctor --flags --search DEFI_
polyrob doctor --flags --changed          # only what this box set away from a default
```

---

## See also

- [wallet.md](wallet.md) — the wallet, the signer, the caps, x402 pay side, `wallet_sign`.
- [onchain.md](onchain.md) — on-chain sight and value movement, `/trade`, the `markets` pack.
- [api.md](api.md#money-and-pricing) — the money and pricing HTTP routes.
- [`docs/CONFIGURATION.md`](../CONFIGURATION.md) — the authoritative flag reference, with a code anchor per row.
- [security-model.md](security-model.md) — the layered trust model these gates sit in.
- [owner-controls.md](owner-controls.md) — pausing, approving and the owner inbox.
- [deployment-postures.md](deployment-postures.md) — how the money flags interact with deployment shapes.
- [../examples.md](../examples.md) — a worked end-to-end money loop.
- `SECURITY.md` in the repository root — the crypto/wallet/payment security posture.
