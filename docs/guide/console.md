# POLYROB Console

The **POLYROB Console** is the web app in `webview/` — a FastAPI + Socket.IO
service that gives you a browser view of your agent's sessions, memory,
autonomy loops, identity, and (in multitenant deployments) billing. It runs
alongside the API/CLI, not instead of them; every page reads the same
underlying services the agent itself uses (session files, the memory
provider, the goal board, cron, `doctor_report`) rather than a second,
parallel data store.

Start it with:

```bash
polyrob dashboard          # alias: polyrob webgate
```

By default this binds to `127.0.0.1:5050` (Posture 0, `local`) and opens
`http://localhost:5050` in your browser (`--no-browser` to skip that,
`--host`/`--port` to change the bind). See
[deployment-postures.md](deployment-postures.md) for the full posture model
(`--posture local|own_ops|multitenant`) and [self-hosting.md](self-hosting.md)
for running it as a persistent service.

---

## Naming: framework vs. instance

**POLYROB** is the framework. A running deployment is one **instance** of it —
by default the instance id is `"rob"` (`core/instance.py::DEFAULT_INSTANCE_ID`,
overridable via `POLYROB_INSTANCE_ID`/`BOT_INSTANCE_ID`). The Console's product
name is resolved independently of the instance id:

- Default: **"POLYROB Console"** (the framework brand), regardless of what the
  instance is called.
- Opt-in override: set `POLYROB_CONSOLE_NAME` to rename the Console itself
  (e.g. an operator running an instance called "rob" can present its web app
  as "Rob Console" without that silently happening just because they set an
  instance id).

This is deliberate: setting `POLYROB_INSTANCE_ID` alone does **not** rename the
Console (`core/instance.py::console_display_name`) — naming the instance and
branding the Console are two independent, both opt-in, decisions. Every page
template renders the name via the `console_display_name()` Jinja global
(`webview/server.py`, `webview/pages.py`), so a single env var repaints the
whole app (title bar, header, page `<title>`).

---

## Deployment posture

The Console runs in one of three postures — `local` (default, loopback,
no auth), `own_ops` (off-loopback, owner-login), or `multitenant`
(wallet/SIWE auth, admin + billing pages registered). See
[deployment-postures.md](deployment-postures.md) for the full posture model,
resolution order, and how to launch each one. Most of what follows applies
to all three postures; where a capability is posture-gated, it's called out
explicitly below.

---

## The 11 capabilities

### 1. Chat / live session feed

`/session/{id}` renders a session's running transcript; the page opens a
Socket.IO connection and joins the session's room to stream new feed events
live as the agent works (`webview/server.py` `join_session`/`_feed_watcher`),
backed by `GET /api/session/{id}/feed/events` for the initial/paginated
history (the older plain `/feed` endpoint is deprecated and just redirects
there). In `own_ops`/`multitenant` posture, the
Socket.IO `connect` handler requires a decoded owner-login cookie or
wallet/SIWE token before a socket is attached to a session room (`local`
posture skips this — the loopback operator is trusted by construction).

### 2. File workspace (tree / preview / download)

`GET /api/session/{id}/workspace/tree`, `/workspace/status`,
`/workspace/file` (text preview) and `/workspace/serve/{path}` (raw
download) expose the session's workspace directory as a browsable file tree.
These endpoints deliberately serve under the **session owner's** identity
even when a different authenticated caller is viewing (`webview/server.py`,
commented "allow public viewing of shared sessions") — i.e. knowing a
session's URL is the access key for a shared session's files, the same as
its live feed, independent of who is logged in.

### 3. Screenshots

`GET /api/session/{id}/screenshot` (latest, JSON) and
`/screenshot/file?ts=...` (a specific captured PNG) surface browser-tool
screenshots saved to the session's `screenshots/` directory. Same
session-owner-scoped access model as the workspace endpoints above.

### 4. Memory browse (`/memory`)

Read-only browse + keyword search over the **active `MemoryProvider`**
(`webview/pages.py::api_memory` → `provider.search(...)`) — the exact same
provider the agent's `session_search`/prefetch calls use
(`modules/memory/*`, selected by `MEMORY_BACKEND`). An empty query browses
most-recent entries; a non-empty query searches. No separate memory store —
this is a read window onto the agent's real recall.

### 5. Autonomy (`/autonomy`)

Shows the durable **goal board** and **cron jobs** for the effective tenant:
`GET /api/webgate/goals` reuses `GoalBoard.list()`
(`agents/task/goals/board.py`) and `GET /api/webgate/cron` reuses
`CronService.list_jobs()` (`cron/service.py`). Both report `{"enabled":
false}` (not an error) when `GOALS_ENABLED`/cron is off — the page degrades
to an empty state rather than a broken one. Read-only; goals/cron are
created by the agent itself (`goal_create`/`cronjob_schedule` tools) or the
CLI, not from this page.

### 6. Identity (`/identity`)

Read-only view of the instance's SOUL and SELF context:
`GET /api/webgate/identity` returns `load_self_context()` (the
operator-authored, instance-wide SOUL/IDENTITY docs) and `load_self_doc()`
(the per-`(instance, user)` evolving SELF doc the agent can write via the
`self_context_manage` tool). There is **no write path from the Console** —
SOUL is frozen/operator-only by design, and SELF is edited only through the
agent's own owner-gated tool, never a web form.

### 7. System health (`/system`)

`GET /api/webgate/doctor` runs the exact same checks as `polyrob doctor`
(`cli/commands/doctor.py::doctor_report`, imported directly — not shelled
out) and reports them alongside the resolved provider/model
(`cli/config_store.py::resolve_provider_model`) and active memory backend.
This is diagnostic/legibility only, not a control surface.

### 8. Settings (MCP / skills / prefs / API keys)

`/settings` is a tabbed page:

- **MCP Servers** — lists platform (global) MCP servers and lets you add
  your own custom server (URL, auth method, transport, retry/timeout
  knobs); credentials are encrypted at rest (Fernet/AES-128-CBC,
  `tools/mcp/security.py::MCPEncryption`).
- **Skills** — lists system skills (read-only, viewable/forkable) and lets
  you create/edit your own custom skills (id, markdown body, trigger
  keywords/tool-ids/priority) — the same skill system the agent loads at
  session start.
- **Preferences** — placeholder ("Coming soon") as of this writing.
- **API Keys** — placeholder in the Settings UI ("Coming soon"); the
  underlying capability is already live as a **REST** surface, not yet
  wired into this tab: `POST/GET/DELETE /api/auth/api-keys`
  (`api/auth_endpoints.py`) lets an authenticated (wallet-logged-in) user
  self-service create/list/revoke their own `rob_…`-prefixed API keys for
  programmatic access (A2A, `/v1`). The full key is shown exactly once at
  creation time.

Two trading-tool config modals (Polymarket, Hyperliquid) also live on this
page — wallet/private-key config, demo-mode toggle, and per-tool trading
limits (max order size, exposure, leverage) — independent of the 11 items
listed here.

### 9. Token-gating / tiers (multitenant only)

`/profile` is registered only in `multitenant` posture
(`webview/server.py::_multitenant_get`, the same gate as the admin pages in
capability 10) — in `local`/`own_ops` posture the route doesn't exist and a
request 404s. Where it's available, it shows an **Account Tier** badge and a
connected-wallet address. Tiers are a `user_profiles.tier` column (e.g.
`x402`, `admin`, a DEN-token-gated tier) consulted at the point of use, not
verified live by the Console page itself — e.g. `POST /api/auth/api-keys`
requires "DEN token ownership (verified via tier)" per its own docstring, and
`LLMUsageTracker._get_user_tier()` reads the same column to decide whether a
user is billed per-token or exempt (x402/admin). The **on-chain
verification** that establishes a tier happens elsewhere (wallet
auth/SIWE verification, x402 settlement, or an admin/tier-assignment path)
— the Console banner only *displays* the tier already recorded in
`user_profiles`, it does not itself talk to a chain.

### 10. Admin dashboard (multitenant only)

`/admin`, `/admin/users`, `/admin/users/{id}`, `/admin/activity` are
registered **only in `multitenant` posture**
(`webview/server.py::_multitenant_get` — the route table simply doesn't
include them in `local`/`own_ops`, so a request 404s rather than being
access-denied). Each handler additionally requires
`request.state.is_admin` at request time; a non-admin authenticated user
gets redirected/alerted rather than seeing the dashboard.

### 11. Crypto deposits / transaction history (multitenant only)

Same posture gate as capability 9 — the `/profile` page only exists in
`multitenant` posture. Its **Top Up Credits** section shows a per-user deposit
address (QR code + copy button) and a paginated **Transaction History**
table. The deposit address is deterministically derived from a master seed
+ `user_id` (`modules/payments/wallet_generator.py::DepositWalletGenerator`
— same user always gets the same address, so the seed can regenerate the
sweep key later). Crediting is driven by `DepositMonitor`
(`modules/payments/deposit_monitor.py`), which is **off by default**
(`DEPOSIT_MONITOR_ENABLED`, also needs `SEPOLIA_RPC_URL`/`ETHEREUM_RPC_URL`)
and polls for on-chain USDC/USDT/ETH transfers, converting ETH via a live
price oracle (`modules/payments/price_oracle.py::get_eth_price_usd`,
clamped by `ETH_PRICE_USD_MAX` so an oracle glitch can't over-credit an
account) at $0.01 USD = 1 credit, with a $5.00 minimum deposit.

### Tenant scoping (multitenant)

Per the B7/E4 fixes, the cross-session pages (Memory/Autonomy/Identity) and
the live feed socket now resolve the **authenticated caller's** identity in
multitenant posture, not the instance owner's:
`webview/pages.py::_effective_user_id` returns
`request.state.user_id` in `multitenant` and **fails closed** (403) if that's
missing — it never silently falls back to the owner's data. In `local`/
`own_ops` posture there is exactly one owner and no separate caller-identity
concept, so these pages are simply "your" data by construction. The
session-scoped pages (chat feed, workspace, screenshots) use a different,
intentional model: they're gated by knowledge of the session's own URL /
Socket.IO room, which is what makes "share a session link" work — see
capabilities 1–3 above.

---

## Payments

Billing in POLYROB has one shape after the C-workstream consolidation (see
`docs/CONFIGURATION.md` → "Billing / x402 / wallet" for the full flag
reference); this section is the narrative version for Console users.

### a. Credits are deducted once, per token, at the end

`modules/credits/usage_tracker.py::LLMUsageTracker.record_llm_usage` is the
**single** deduction path. A request first passes
`api/payment_verification.py::verify_payment_for_request`, which only
**authorizes** (checks `has_sufficient_balance`) — it does **not** deduct.
The actual charge happens once per LLM call, sized to real token usage
(input/output/cached, with markup), when `record_llm_usage` runs. This
closed a prior double-billing bug where the gate deducted a flat cost *and*
the tracker deducted again per token.

### b. One x402 price, quoted == charged

`modules/x402/x402_integration.py::get_x402_price_usd()` is the single
source of the x402 per-request price. An explicit `X402_PRICE_USD` always
wins; otherwise the price is *derived* — worst-case cost of the
`X402_MAX_TOKENS_PER_REQUEST` budget at the most expensive model's output
rate, times a safety markup (`X402_PRICE_MARKUP`, default 2×) — because
x402 settles *before* the request runs. Every caller of this value (the
live middleware charge, `/api/x402/pricing`, the A2A Agent Card, and the
402-challenge body) reads the same function, so what's quoted is always
what's charged. The dead premium-tier override that used to diverge from
this SSOT has been removed. `LLMUsageTracker._enforce_x402_budget` then
caps actual token usage to the prepaid budget per session, so a request can
never cost the platform more than it collected.

### c. Anonymous x402 works on A2A + `/v1` — not the REST API

The pay-per-request 402-challenge handshake (`X-PAYMENT` header, Coinbase
facilitator verify+settle) is gated to exactly four path prefixes:
`/a2a/rpc`, `/a2a/message/stream`, `/a2a/tasks`, `/v1/chat/completions`
(`modules/x402/middleware.py::X402_GATED_PATH_PREFIXES`). The authenticated
`/api/task/*` REST API is intentionally **not** included — POLYROB's
outermost `fallback_auth_middleware` already 401s anonymous callers on
every `/api/`/`/task/` path, so an anonymous, pay-per-crypto-signature
caller who wants access without an account should use the A2A protocol or
the OpenAI-compatible `/v1` surface, not the REST API. A caller that
already carries an `Authorization`/`X-API-KEY`/session cookie is never
intercepted by the 402 challenge, regardless of path — only genuinely
anonymous requests hitting the four gated prefixes see it.

### d. Crypto top-up via deposit addresses

See capability 11 above: a per-user deterministic deposit address, a
`DepositMonitor` background poller (off by default,
`DEPOSIT_MONITOR_ENABLED`), a clamped live ETH price oracle, and $0.01
USD = 1 credit at a $5.00 minimum. There is **no fiat/card payment path**
anywhere in POLYROB — credits/x402/deposits are the entire billing surface.

### e. Billing only exists when `ENABLE_AUTH` is on

`core/initialization.py::initialize_auth_services()` is called
unconditionally on every boot but returns immediately if
`container.config.enable_auth` (env `ENABLE_AUTH`, default **OFF**) is
false — *before* it registers `balance_manager`, `api_key_manager`,
`wallet_generator`, or the deposit monitor. Posture 0 (`local`) and Posture
1 (`own_ops`) run with these services never constructed: no credit
deduction, no deposit address, no tier — because you're using your own LLM
provider API keys directly, there's nothing for POLYROB to meter.
Multitenant deployments are the intended posture for turning `ENABLE_AUTH`
(and `X402_ENABLED`) on. `api/payment_endpoints.py` and
`api/x402_endpoints.py` are mounted unconditionally regardless of posture —
each request-time handler self-checks for a registered `balance_manager`
and 503s/no-ops when billing isn't wired up, rather than the routes being
absent.

---

## See also

- [architecture.md](architecture.md) — overall system architecture
- [configuration.md](configuration.md) — configuration guide
- [../CONFIGURATION.md](../CONFIGURATION.md) — full environment-flag SSOT,
  including every billing/webgate flag referenced above
- [api.md](api.md) — REST + A2A + OpenAI-compatible API reference
