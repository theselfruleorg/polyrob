# Changelog

All notable changes to POLYROB are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.5.1] — 2026-07-08

Bug-fix release on top of 0.5.0.

### Money / wallet
- 2026-07-08: **Agent wallet spends from the address it tells you to fund (fund == spend).** The
  agent wallet is hub-and-spoke (one seed → per-venue keys); the x402 spend path signed with the
  `x402` venue key while `AgentWallet.address` (the owner-facing "fund me" address) returned the
  `treasury` key — so funding the surfaced address funded an address no spend path used, stranding
  funds. Now `AGENT_WALLET_OPERATIONAL_VENUE` (default `treasury`) is the venue same-chain spend
  paths sign with, `AgentWallet.address` tracks it (surfaced == spent), and a regression test locks
  the invariant. The operational venue is clamped to the fundable same-chain venues
  (`treasury`/`x402`); hyperliquid keeps its own delegated key. New **`polyrob wallet [--json]`**
  shows per-venue address + on-chain balance + network + caps and marks which address to fund
  (delegated venues are labeled "not funded here" so they can't be mis-funded). "Venue" elsewhere
  stays a policy/accounting label — per-venue caps are unchanged. Fusion-of-opuses reviewed.

### CLI / update
- 2026-07-08: **`polyrob update` apply works on a tag-pinned instance.** The git apply runner did
  `git pull --ff-only`, which fails on the detached-HEAD pinned-tag posture the instance runs (and
  would pull unreviewed `main` on a branch). It now fetches tags and checks out the resolved release
  tag for the `stable`/`pre` channels (`--channel git` keeps the branch fast-forward). Also
  `polyrob update --apply --json` no longer crashes on a failed apply (it serialized a raw
  exception); the failure payload is now valid JSON. The full apply lifecycle
  (snapshot → install → guarded-migrate → verify → auto-rollback) was validated end-to-end.

## [0.5.0] — 2026-07-08

**0.5.0 is a large capability release** on top of 0.4.3: the compute-posture ladder (installable
sandbox + persistent shell/process + `self_env`), the agent money loop, the full-control monitoring
console, restart-durable autonomy, and a broad intelligence/memory/prompt/security polish pass.
Every capability is flag-gated and a default server is behavior-identical to 0.4.3 unless a bullet
says otherwise.

### Computer-use / system-use (compute posture)
- 2026-07-07: **`AGENT_COMPUTE_POSTURE` capability ladder (0–3), default 0.** A third
  orthogonal capability axis (beside `POLYROB_LOCAL` trust and `AUTONOMY_POSTURE`
  loops): how much host/compute capability the agent has. Frozen at import (a
  mid-process env write can't raise it); garbage/out-of-range never rounds up.
  One gate predicate `compute_posture_allows(ctx, N)` — posture≥N AND owner tenant
  AND not-leaf/sub-agent AND not a forged self-wake/delegation-result turn — governs
  every posture-gated capability. A default server (`AGENT_COMPUTE_POSTURE` unset)
  is byte-identical to before. (`agents/task/constants.py`)
- 2026-07-07: **Posture 1 (`sandbox-dev`) — an installable, stateful, HTTP-testable
  sandbox.** For an entitled session: the docker sandbox mounts a writable `/install`
  (session-bound `.pylibs`), runs `python -s` with `PYTHONPATH=/install` (instead of
  the env-ignoring `python -I`, which stays at posture 0) so `pip install --target`
  imports; `run_code` gains `env` + `packages` (declarative install, network-gated);
  dev containers default to `bridge` network. A persistent **`shell`** tool
  (`shell_run`, cwd/env persist across calls; foreground/background discipline) and a
  **`process`** job manager (list/poll/log/kill) run inside the session's container.
  Container ports publish to host loopback and a narrow allowlist lets the browser/
  `web_fetch` reach exactly those ports (never RFC1918/metadata) so the agent can
  HTTP-test its own server. (`tools/code_exec`, `tools/shell`, `tools/browser`,
  `tools/web_fetch`)
- 2026-07-07: **Posture 2 (`self-maintain`) — the approval-gated `self_env` tool.**
  Distinct approvable verbs (never raw bash): `install_dep` (own venv, pinned),
  `read_source`/`patch_source` (install-tree-confined, env/config hard-denied),
  `git_pull` (ff-only, ext:: rejected), `restart_service` (supervised only). Every
  call is `compute_posture_allows(ctx,2)`- AND approval-gated and emits a
  `self_modification` audit event. At posture≥2 the Controller auto-gates
  `shell_run` + the `self_env_*` verbs behind the interactive approver (fail-closed to
  deny; headless denies). (`tools/self_env`, `tools/controller/approval.py`)
- 2026-07-07: **Self-escalation hardening.** `AGENT_COMPUTE_POSTURE`, `APPROVAL_REQUIRED_TOOLS`,
  `APPROVAL_PROVIDER` are frozen at import; the env/config files that hold them are
  hard-denied to every agent-writable surface — `secret_guard` now catches `*.env`
  (the prod `polyrob.env` basename that `.env*` missed) and adds
  `is_protected_config_path` for `/etc/polyrob`. `shell`/`process`/`self_env` are in
  `DELEGATE_BLOCKED_TOOLS` and the correspondent-taint high-impact set — never reachable
  by a leaf/forged/correspondent turn. Autonomous goal/cron runs are provisioned with
  the compute toolset only at posture≥1.

### CLI / operability
- 2026-07-07: **Flag registry + `polyrob doctor --flags` (Wave D / SA-05).** POLYROB's ~300 env
  flags are now a runtime-enumerable registry (`core/flags.py`, catalog extracted from
  `docs/CONFIGURATION.md` with a contract test keeping doc rows ⊆ registry). `polyrob doctor
  --flags` dumps every flag's resolved value + source — including live posture/local-derived
  defaults (`default(posture:owner-visible)`, `default(local=ON)`) via
  `agents/task/flag_defaults.py` — with key/token/secret values always masked. The
  "shipped dark, nobody knew" flag failure class is now visible from one command.

### Money / financial agency
- 2026-07-07: **Money-loop + wave hardening (adversarial review).** Anonymous/empty-tenant
  callers are refused across `accounting`/invoicing (an empty `user_id` previously widened the
  wallet-spend query to ALL tenants — cross-tenant financial-data leak — and created a shared
  anonymous invoice bucket); the settlement watcher claims atomically before notifying (exactly
  one wake/event per settlement under concurrent processes) and expiry never mis-reports a
  concurrently-settled invoice; `doctor --flags` masks `_SEED`/`_HASH` values
  (`PAYMENT_MASTER_SEED`, owner password hash were printed in clear) and now agrees with
  `doctor` about `POLYROB_LOCAL`; the cron wake change-gate records an outcome-tagged baseline
  so a persistently-failing gated job retries instead of being skipped as no-change;
  `autonomy_state.db` co-locates with its sibling DBs via the container data_dir and its store
  is memoized (zero sqlite I/O per session construction). Flags-catalog generator checked in
  (`scripts/gen_flags_catalog.py`, `--check` parity enforced by test); 4 documented-but-
  unregistered flags gained proper rows.
- 2026-07-07: **Money loop v1 (vision Pillar 1, flagship).** The agent can now invoice,
  get woken on settlement, and account for itself — all behind `X402_INVOICE_ENABLED`
  (default OFF): (1) new `x402_invoice` tool — `x402_request` creates a *pending*
  `x402_payment_requests` row (amount ceiling `X402_INVOICE_MAX_USD`, per-tenant daily cap
  `X402_INVOICE_DAILY_MAX`, session provenance in metadata, `payment_requested` event;
  the action is in the recommended approval set and the tool is leaf-delegation-blocked);
  `x402_invoices` lists them; `accounting` renders the unified ledger. (2) A settlement
  watcher on the autonomy-runtime ticker seam expires stale invoices and, when one settles,
  re-enters the originating session via the self-wake rail ("I invoiced → I got paid" as one
  continuous piece of work) and emits `payment_settled`/`payment_expired` events; settlement
  is an attested transition (`polyrob owner settle <id> [--tx-hash]`, plus `owner invoices`).
  (3) `modules/credits/unified_ledger.py` — one read-only view joining LLM/tool costs
  (`usage_records`), wallet spend (`wallet_spend` events), and x402 receipts/pipeline:
  earned / pending / spent / net, evidence-backed and tenant-scoped. Agent finances stay
  separate from platform billing; every leg is fail-open.

### Autonomy
- 2026-07-07: **Continuity on + restart-durable autonomy (vision Pillar 4).**
  (1) `AUTONOMY_POSTURE` owner-visible/full now also turns on the continuity/learning trio
  that was local-only dark on the server: `EPISODIC_MEMORY_ENABLED`, `EPISODIC_DIGEST_INJECT`,
  and `REFLECTION_ON_SESSION_CLOSE` (now posture-governed via
  `AutonomyConfig.reflection_on_session_close`; explicit env always wins).
  (2) The two volatile autonomy registries persist to a new `autonomy_state.db` sidecar
  (WAL+jitter, registered in `core/db_manifest.py`), gated `AUTONOMY_STATE_DURABLE`
  (default ON, fail-open): background delegations write dispatched/terminal rows and a
  startup sweep (`core/autonomy_runtime.py`) marks crash-interrupted delegations
  `interrupted` and surfaces them back to their session via the self-wake rail — never a
  silent evaporation, never a magic resume; the self-wake `ReentryBudget` depth cap now
  survives restart (a mid-storm loop can't get a free reset by crashing), with stale rows
  aged out and per-session ids seeded past persisted history.
- 2026-07-07: **Wake change-gate (vision Pillar 3).** A cron review job with
  `payload.change_gated` now skips the paid model call when nothing observable changed since
  its last tick — a cheap fingerprint over the tenant's goal board/events, other cron runs,
  and newest episode is compared to the per-job baseline in `cron.db::wake_gate`
  (`cron/wake_gate.py`); an unchanged fingerprint is a $0 tick (`cron_run skipped/no_change`),
  the fix for the observed ~23/25 no-op review-wake economy. Delivery jobs are never gated and
  every fingerprint error fails open (the tick runs). Gated `WAKE_CHANGE_GATE` — default OFF,
  ON under `AUTONOMY_POSTURE=full` (it pairs with `CRON_ENABLED`); explicit env always wins.

### Console / Webview
- 2026-07-07: **Full-control console: one data root, all sessions, in-process interaction.**
  (1) RC-1: the webview installs its process-global `pm()` from the shared resolver
  `core/runtime_paths.py::resolve_session_data_root()` (`DATA_ROOT` wins →
  `{POLYROB_DATA_DIR}/sessions` → legacy `./data/task`) at startup, so the console reads the
  SAME session tree the agent writes (prod previously browsed a stale `/opt/polyrob/data/task`
  while the agent wrote `/var/lib/polyrob/sessions` — catalog, feeds, and the /activity
  feed-watcher/telemetry tail were all wrong). (2) RC-2: in own_ops/local the owner's catalog
  lists sessions across ALL user dirs (CLI=`local`, telegram=`u_<hash>`, …) with a per-row
  user chip; own_ops non-owner identities get `[]`; multitenant stays strictly per-tenant.
  (3) WS-3: `POST /api/session/{id}/messages` and queue-status call the IN-PROCESS task
  router/TaskAgent when mounted (prod is single-service; the legacy `:9000` proxy remains the
  two-service fallback); the directly-mounted `/api/task/*` routes gain a read-only mutation
  guard and are pinned non-public; posture `local` now stamps the canonical owner auth state
  (the loopback operator IS the owner) so the local console can create sessions instead of
  402ing. (4) WS-4: active catalog rows carry an honest runtime chip via the P6 routing seam
  (`live@agent` in another process / `live` here / idle), and a remote-owned send returns an
  honest 409 instead of a false 404.
- 2026-07-06: **UI/UX finalization (rendered-page evaluation fixes).** Socket.IO now accepts
  the console's own serving origin (bind-port origins in the default allowlist + a true
  same-origin gate that never trusts JS-settable `X-Forwarded-*` headers) — live streams work
  from any local origin instead of dying with engineio 400s. `/settings` probes for the
  separate API service once and renders an honest "needs the POLYROB API service" state
  (crypto-trading cards only render when their tools answer; the Preferences/API-Keys
  "Coming soon" stubs are gone). Tenant nav (Profile/Sign In) no longer leaks into
  local/own_ops pages (posture-aware layout default). The System page's memory-backend header
  and doctor output flow through one resolution and can't contradict each other.
- 2026-07-06: **`/activity` daily-driver polish.** Day-separator rows + full-timestamp
  tooltips; goal events enriched with the goal's title (cached fail-open goals.db lookup) and
  outcome/status so dispatcher start/done pairs read start→done; kind-filter chips collapse
  behind "+N more" past 8 kinds; the session drill-down panel shows summarized feed lines
  with status coloring and live tail-follow; a reconnect hint appears when the rejoin
  snapshot can't cover the gap.
- 2026-07-06: **Page polish.** Memory page captions results ("showing the N most recent" /
  "N matches") over a structured `{items,count,mode}` API; identity page probes `/pfp.json`
  once instead of firing a 404 chain for avatar-less instances; session catalog rows
  deep-link to the Feed tab (`#feed` hashes now honored) with an SVG empty state; chat empty
  state gains an orientation hint; read-only consoles render a monitoring hint instead of
  dead model/tools pickers.
- 2026-07-06: **Global `/activity` terminal.** New console page streaming everything the
  instance does live — every session's feed events (steps, tool calls, LLM calls, lifecycle)
  plus goals/cron/telemetry/skill events — with kind/text/session filters, follow-tail,
  per-event JSON unwrap, and per-session drill-down panels. Cross-process backbone
  (`webview/activity.py`): recursive `watchfiles` over the session data root + id-cursor tails
  over `telemetry_events.db`/`goal_events`/`skill_install_audit`, one normalized event shape,
  Socket.IO room `activity`. Owner/admin-gated in every non-local posture
  (`WEBVIEW_ACTIVITY_ENABLED`, `WEBVIEW_ACTIVITY_TAIL_SEC`).
- 2026-07-06: **Display gaps closed + bug fixes.** The rich per-event Feed renderer is finally
  reachable (the session view was missing its Feed tab button); Stats now shows the computed
  provider-cost/markup breakdown; `POST /api/internal/emit` emits to the room clients actually
  join (was a dead `session:`-prefixed room); `/api/repair/{id}` runs the REAL
  `repair_sessions.repair_session_telemetry` (was fake success); duplicate startup handlers
  merged; dead `compute_feed_checksum`/shadowed duplicate stream route removed.
- 2026-07-06: **Security hardening.** Owner-login gains a per-IP attempt throttle (5/5min →
  429) and stateless double-submit CSRF; `return_to` open redirect neutralized; new enforced
  `WEBVIEW_READ_ONLY` mode (mutations 403, chat input hidden) for monitoring-only deploys.
- 2026-07-06: **Standalone VPS deployment shape.** `deployment/polyrob-webview.service`
  (loopback bind, `--forwarded-allow-ips=127.0.0.1`, env from `/etc/polyrob/*.env`) +
  `deployment/nginx-webview-ownops.conf` (TLS + websocket proxy) + `scripts/deploy_webview.sh`
  (backup → rsync → install → verify). Dead `webview/deploy.sh` (port-3000/`/opt/rob` era)
  removed; `webview/README.md` rewritten to match reality.

### Earning & owner experience
- 2026-07-08: **Payable endpoint + financial visibility + owner continuity.** A payable x402 invoice
  endpoint with correspondent-rail settlement delivery; a webview **financial dashboard** over the
  unified ledger; a **deterministic ($0, no-LLM) owner daily digest** over the ledger + event log; a
  bounded **owner-facts doc** on the SELF/SOUL seam. CLI: **`/journey`** + `polyrob journey` (a
  did/learned/earned/changed timeline), **`/learn`** (distills a described procedure into a
  quarantined skill), and **`polyrob init`** (pairs an owner + instance id; `doctor` reports the
  pairing). A crash-interrupted running session now **resumes on the next message** (durability
  documented). A Fusion-panel review closed reachability / resume / fund-safety / spam / quarantine
  gaps. All flag-gated OFF by default.

### Intelligence, memory & prompts
- 2026-07-08: **Intelligence-layer polish (P0/P1/P2 waves).** A broad correctness pass over the core
  agent loop and its memory/prompt/context/aux seams: core-loop fixes (P0-1..7); automatic prefetch
  no longer self-echoes the current session and skips sub-agents; reflection/forgetting is no longer
  disabled when `phase=None`; reflection summaries are capped + threat-scanned before a durable write
  and now actually reach the cross-session store; one-shot ephemeral context is consumed on success
  and restored on failure; the compaction cooldown isn't stamped on a no-op/aborted compaction and
  the pre-synthesis placeholder brain never persists to history; the background reviewer/judge aux
  clients are provisioned off the event loop and closed instead of leaking a pool per fire;
  autonomous sessions default to prefetch cadence 3. `HMEM_TAIL_PLACEMENT` now defaults **ON** (H-MEM
  rides the cacheable tail). Prompt pass: valid brain-state JSON + accurate rules + an agency
  charter, a compact `<available-actions>` index in native mode, identity-precedence/owner
  unification, a true turn-exit contract, de-feared delegation, and browser/vision/input-format/
  MCP-fallback guidance gated on the session's real tools; the per-step brain-state format nag is
  family-gated. One persona resolver across all surfaces.

### Self-evolution & skills
- 2026-07-08: **Self-tooling + skill-authoring safety.** The orphaned self-tooling path is wired as
  **`mcp_install`**; a full-body `show` verb precedes promote/reject; skill promote preserves
  original authorship and is **owner-only** (not merely non-forged); a fallback-loaded skill
  registers into the session skill set; keyword matching is **word-boundary anchored** (no substring
  false positives). The REPL gains **`/pending`** — an owner review queue for agent self-evolution.

### Telemetry & observability
- 2026-07-08: **First-class events to the durable event log.** `memory_recall` / `memory_write`,
  `self_modification`, and `goal_run` are now first-class telemetry events — learning, self-edits,
  and autonomous goal runs are observable after the fact instead of inferred from logs.

### Security
- 2026-07-08: **Untrusted-data & gating hardening (P1 wave).** A real gated-skill load gate +
  external-skill scan; untrusted content offloaded to workspace files is framed as DATA; delegation
  results are wrapped and their wake-kicks bounded; the correspondent capability-gate now covers
  money / egress / exec verbs; the email surface can't fall through to the obey-path when the tier
  model is off; curated-memory reads are wrapped as untrusted DATA.

## [0.4.3] — 2026-07-06

### Tools
- 2026-07-05: New agent-callable `message` send tool (behind `MESSAGE_TOOL_ENABLED`, default
  off, ON under `POLYROB_LOCAL`) with an owner-scoped outbound allowlist — every non-owner
  target is denied by default until the owner allows it (`polyrob owner allow/deny/allowlist`,
  or the Telegram `/allow` verb).

### Autonomy
- 2026-07-05: **Goal completion verification (intelligence-first).** Goals can now honestly fail:
  the goal-run prompt teaches `OUTCOME: BLOCKED — <need>` and a declared BLOCKED routes to the
  failure/escalation rail with an immediate block (retries are pointless when the agent itself
  says so; owner cancel always wins). An optional **completion judge** (`GOAL_COMPLETION_JUDGE`,
  default off) has a cheap aux model verify `payload.acceptance` against the framework-recorded
  action ledger — `unmet` fails the goal, uncertainty always passes. Deliberately NO
  string-matching side channels: an earlier refusal-scan + hardcoded capability-notes layer was
  removed the same day (owner directive — platform/capability knowledge lives in the agent's
  memory/skills/mission content, not framework code).
- First-class **asks**: when a goal blocks or the planner leaves the pipeline empty, the agent now
  leaves a durable "I need X from you" ask on the goal board (behind `GOAL_BLOCKER_ESCALATION`);
  fulfilling one (`polyrob owner fulfill <id>`) flips its blocked goals back to ready.
- Empty-pipeline stalls now escalate to the owner once per stall after
  `GOAL_EMPTY_PIPELINE_ESCALATE_AFTER` consecutive fruitless planner runs (a "queue healthy"
  verdict never escalates).
- Telegram owner-admin verbs: `/pending`, `/approve <id>`, `/reject <id>`, `/asks`,
  `/fulfill <id>` — the self-evolution approve loop and the ask queue are now reachable from a
  phone, not just the CLI. Owner-gated by principal; no local bypass on network surfaces.
- New CLI verbs: `polyrob owner asks`, `polyrob owner fulfill <id>`.

### Skills
- New `x-engagement` bundled skill: write-side X/Twitter engagement playbook (route selection,
  quality bar, live-URL completion proof; documents that cold replies AND cold quote-tweets are
  403-blocked for automated accounts).

### Fixed
- CI: removed five test modules that imported private (non-exported) helper scripts and broke
  test collection on a clean checkout (`tests/unit/test_battletest_metrics.py`,
  `tests/unit/test_seed_battletest.py`, `tests/unit/test_seed_cron_outreach.py`,
  `tests/unit/memory/test_e1_harness_smoke.py`, `tests/unit/memory/test_e2a_harness_smoke.py`).
- Goal completion judge: dedicated tolerant judge-response parser plus one corrective retry, so a
  chat model that narrates instead of emitting the verdict JSON no longer masks verdicts via
  fail-open.

## [0.4.2] — 2026-07-04

Initial public release. POLYROB is a self-hosted autonomous AI agent that pursues goals, learns
from experience, and runs entirely on your own machine.

### Agent core
- Autonomous task loop: give it a goal in plain language and it plans, browses the web, reads and
  writes files, runs code and shell commands, calls tools/APIs, and recovers from its own errors.
- Multi-provider LLM — OpenAI, Anthropic, Google Gemini, DeepSeek, OpenRouter, NVIDIA NIM — behind a
  native LLM layer (no third-party agent framework), with automatic failover and live model
  switching (`/model`), prompt caching, and optional extended thinking.

### Memory & learning
- Cross-session recall: SQLite FTS5 keyword search by default, or optional hybrid keyword+vector
  recall (`sqlite-vec`) that degrades gracefully to keyword search.
- Reflective, hierarchical memory with importance-based forgetting; an episodic activity log that
  bridges new sessions.

### Autonomy (personal-agent mode, `POLYROB_LOCAL`)
- Durable goal board (SQLite, atomic claims, circuit breaker) that survives process restarts.
- Natural-language cron with out-of-band delivery; self-wake; background review; a skill curator.
- Self-written skills through a scanned, quarantined pipeline; every self-modification is reviewed
  before it takes effect. Skills use the open [agentskills.io](https://agentskills.io) `SKILL.md`
  format.
- Least-privilege sub-agent delegation (`delegate_task`), sync or detached.

### Interfaces & interoperability
- Terminal agent (`polyrob`), single-user web dashboard (Socket.IO), REST API with SSE streaming,
  and a drop-in OpenAI-compatible `/v1` endpoint.
- A2A (Agent-to-Agent) protocol, MCP client (STDIO/SSE/HTTP/Streamable HTTP), and chat surfaces:
  Telegram, email, WhatsApp.

### Tools
- Lightweight `web_fetch` (URL→markdown, no browser) and full Playwright browser automation;
  structured web data (AnySite), Perplexity search, coding tools, and opt-in code execution.

### Safety (on by default)
- Untrusted-input wrapping, least-privilege delegation, schema sanitization, and SSRF confinement.
- Three-tier access control (OWNER / CORRESPONDENT / DENIED) for chat surfaces, with a capability
  gate for correspondent-tainted sessions; optional memory threat-scan.

### Optional crypto/web3 (off by default, unaudited)
- x402 pay-per-request, a native agent wallet with spend caps, and ERC-8004 agent identity. This
  code has not had an independent security audit — see [SECURITY.md](SECURITY.md).

### Deployment
- Self-hosted, MIT-licensed. Modular install extras (`server`, `browser`, `memory-vector`, `crypto`,
  `telegram`, `twitter`, `voice`). Three deployment postures (local / own_ops / multitenant) and a
  Docker Compose setup.

[0.4.2]: https://github.com/theselfruleorg/polyrob/releases/tag/v0.4.2
