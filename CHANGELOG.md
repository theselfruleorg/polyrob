# Changelog

Notable changes to POLYROB. Newest first. This file is the home for *dated, point-in-time* change
history — `AGENTS.md` describes how the system works **now**; this file records how it got there.
Flag defaults live in `docs/CONFIGURATION.md` (the SSOT). Dates are when the work landed on `main`.

> Scope note: this captures the major June 2026 rework. Older history is in `git log`.

## [Unreleased]

### Added

- **2026-07-04 — Intelligence-wiring upgrade: final polish + out-of-scope closeout (handoff
  `docs/plans/2026-07-03-intelligence-wiring-upgrade.md`, Groups A–E).** The quality/test/doc minors
  and out-of-scope items deliberately deferred by the shipped 20-task wiring upgrade:
  - **A1** placeholder brain is now treated as empty for the loop-duplicate heuristic (not just the
    H-MEM write). **A2** output-validation judge prompt is task-type-neutral (browser guidance only
    when a browser session exists). **A3** `DELEGATION_RESULT_KIND` single-sources the forged-turn kind.
  - **E3** `deliver="email"` cron delivery is reachable on single-owner headless deploys via a new
    `POLYROB_OWNER_EMAIL`/`BOT_OWNER_EMAIL` fallback (`core.instance.resolve_owner_email`).
  - **E8** the Grok MCP tool-call block now teaches the direct `{server}_{tool}` flat-param form (not
    the deprecated nested `mcp_execute_tool(arguments=…)`) and is injected only when MCP is loaded.
  - **E7** verified non-issue (provider re-derived per call); **E6** documented deliberate scope.
    Test-coverage (Group B) and doc/comment (Group C/D) cleanups. Owner-decision items (E1 H-MEM
    server tail-placement, E2 cron failure-breaker schema, E4 `memories` retention, E5 per-tenant
    planner/quota, E9 config-anchor re-sweep, E10 CRON_ENABLED-local / REFLECTION blank-string) are
    surfaced for owner sign-off, not silently changed.

- **2026-07-04 — Autonomy/learning/evolution loop closures (plan
  `docs/plans/2026-07-04-autonomy-learning-evolution-vs-hermes-REVIEW.md`).** Closes the "loops don't
  close" gaps found in the overnight review — each learning loop terminated one hop before it changed
  anything, invisibly to the owner. All flag-gated, default-OFF on server (safe subset ON under
  `POLYROB_LOCAL`), fail-open:
  - **§7.1 Self-evolution transparency + control loop (P0).** `core/self_evolution.py` — one owner
    aggregator over BOTH pending→promote pipelines (self-context + authored skills):
    `list_pending`/`promote`/`reject`, a proactive owner Telegram notification, and new
    `polyrob owner pending/promote/reject` CLI verbs. `SelfContextWriter`/`SkillWriterMixin` gained
    `list_pending`/`reject`. Wired into `self_context_manage`/`skill_manage` so a pending write tells
    the owner. `SELF_EVOLUTION_TRANSPARENCY` (OFF/ON-local).
  - **§7.2 Blocker → owner escalation.** `agents/task/goals/escalation.py` — a blocked goal now
    surfaces a concrete ask to the owner instead of dying silently. `GOAL_BLOCKER_ESCALATION` (OFF).
  - **§7.3/7.4 Objective success-criteria + planner anti-paralysis floor.** Objectives carry an
    optional `success_criteria` (settable via `objective_add`, surfaced to the planner); the planner
    prompt no longer terminates with "create NOTHING" — it must surface the blocker/ask instead.
  - **§7.5 Autonomous continuity bridge.** `build_mission_continuity` carries recent activity INTO a
    goal/cron tick so it stops re-deriving "nothing new". `AUTONOMOUS_CONTINUITY_BRIDGE` (OFF).
  - **§7.7 Session-close reflection trigger.** Consolidates a short session's findings at close (the
    per-step 25-finding trigger was unreachable for cron/goal sessions). `REFLECTION_ON_SESSION_CLOSE`
    (OFF) + `REFLECTION_SESSION_CLOSE_THRESHOLD` (5).

- **2026-07-04 — POLYROB avatars (Mindprint): deterministic agent face + voice signature.** A new
  `avatar/` engine (`mindprint.js`, the SSOT for pixels) generates a per-agent face from a seed
  (tiered-rarity trait genome, any-hue palette) plus an engine-agnostic voice signature
  `{pitch,rate,timbre}` for the upcoming voice-interface app. It renders **live** everywhere: in the
  **CLI** from a pure-Python field port (`modules/pfp/mesh.py`, JS↔Python parity-tested; truecolor
  half-blocks + animation, no PNG/Chromium) and in the **webview** via the real engine on a canvas
  (`webview/pages.py` serves `/pfp.json` + `/avatar/*.js`; embedded in the Identity page). `polyrob
  pfp` commands: `pick` (independent face/voice shuffle → `avatar/config/rob.json`), `generate`
  (headless still via the `[browser]` extra, idempotent, committed-PNG fallback → instance identity
  home), `show`, `studio`, `push` (X live via v1.1 `update_profile_image`; Telegram assisted). The
  frozen config is an instance-scoped identity blob beside SOUL/SELF; avatar creation is optional and
  deferrable. Also lands the canonical **polyrob logo** on the dev portal (`web/portal/`), rendered
  from the same engine. Flags: `PFP_PUSH_TWITTER` / `PFP_PUSH_TELEGRAM` (both default OFF). See
  `docs/plans/2026-07-03-polyrob-avatars-FINALIZED-upgrade-instructions.md`.

### Changed

- **2026-07-03 — Removed the dead surface-layer role ladder (permissions audit F2/F3, single-owner
  model).** `core/surfaces/surface_permissions.py::SurfacePermissions` (the `user < moderator <
  admin < super_admin` ladder) had **zero production callers** — it was defined and unit-tested but
  never wired into any gate (permission theatre / false assurance), and its `is_owner` (whole
  `SURFACE_SUPER_ADMIN_USER_IDS` set) would have *disagreed* with the real owner SSOT
  `core.instance.is_owner` (which uses only the first entry, via `resolve_owner_principal`). POLYROB's
  model is single-owner: `core.instance` is the one owner SSOT; HTTP/API admin is a separate concern
  keyed on `request.state.role ∈ ADMIN_ROLES` (`core/constants.py`). Deleted the class + its test.
  `SURFACE_SUPER_ADMIN_USER_IDS` keeps its only real effect (first entry → owner principal);
  `SURFACE_ADMIN_USER_IDS` is now documented as inert (`docs/CONFIGURATION.md`). No behavior change —
  the ladder gated nothing.

### Fixed

- **2026-07-03 — Approval-gate docstring corrected (permissions audit F5).** `tools/controller/
  approval.py` claimed "the server sets `APPROVAL_REQUIRED_TOOLS` to this unless overridden" — but
  nothing wired that: `APPROVAL_REQUIRED_TOOLS` defaults empty (hook never registered) and the
  default `APPROVAL_PROVIDER=auto` (`AutoApprover`) allows all. The docstring now states the
  mechanism is opt-in and needs BOTH the env set AND a non-`auto` provider (there is no safe default
  to flip without a real interactive approval UI). No behavior change. Full findings:
  `docs/plans/2026-07-03-permissions-system-audit-FINDINGS.md`.

- **2026-07-03 — `self_context_manage` promote gate honored the global `POLYROB_LOCAL` flag with
  no surface filter (security, permissions audit F4).** The owner-only `promote` (activate a pending
  SELF/identity doc) resolved owner via `is_owner(user_id, local=local_mode_enabled())` — and
  `local=True` returns True for ANY non-empty uid. Unlike `core.surfaces.access`/`core.pairing`
  (which only honor the local-owner bypass for `{cli,local,repl}` surfaces), this call site has no
  surface id (it runs inside a session, seeing only the `execution_context`), so under
  `POLYROB_LOCAL=true` with a network surface attached a non-owner network turn could self-promote
  its own (per-tenant) identity doc. Fix: new `core.instance.is_owner_local_safe` honors the local
  bypass ONLY for the genuine single-user local operator tenant (`"local"`, from
  `build_cli_container`'s `LocalIdentity`); a forgeable network sender's hashed `u_…` id / owner
  alias never equals it. Owner-by-principal is unchanged. The CLI operator (uid `"local"`) still
  promotes; the bound server owner still promotes by principal match.

- **2026-07-03 — Correspondent capability gate: tool_id-vs-action-name confusion let high-impact
  actions bypass the taint gate (security, permissions audit F1).** The WS-A capability gate
  (`agents/task/agent/core/correspondent_gate.py`, active under `CORRESPONDENT_ACCESS_ENABLED`) is
  the structural backstop that blocks money/comms/code-exec/delegation tools while a session is
  tainted by untrusted correspondent DATA. But its denylist mixed **tool_ids** (`code_execution`,
  `goal`, `cronjob`, `coding`, `browser`, `twitter`, `x402_pay`, `mcp`, `git`…) with action names,
  while the pre-tool-call hook only ever receives the bare **action name** (`run_code`,
  `goal_create`, `cronjob_schedule`, `x402_fetch`, `twitter_post`, `go_to_url`, `apply_patch`,
  dynamic MCP `{server}_{tool}`…). Every tool_id token was therefore dead — a tainted (forged/
  injected) session could still execute code, auto-pay (`x402_fetch` — the gate's own headline
  promise), schedule durable autonomous work, post, browse, or drive MCP. Only the separately
  enumerated crypto trade verbs, delegation, and skill/self-context actions were actually blocked.
  Fix: the hook now **resolves each action's owning tool_id** at call time
  (`build_tool_resolver` → `Controller.get_action_details(name).tool`) and blocks the whole tool via
  a real tool-id set — covering even dynamically-named MCP actions. Crypto (`hyperliquid`/
  `polymarket`) stays on the enumerated-verb model so its `get_*` reads remain allowed. Fail-closed:
  a resolver fault degrades to the name-only decision and never opens a hole. Default posture
  (`CORRESPONDENT_ACCESS_ENABLED` OFF) is byte-identical — the gate isn't registered.

- **2026-07-03 — User-facing docs actualization pass.** Re-verified `README.md`,
  `docs/guide/getting-started.md`, and `docs/guide/cli.md` against the real CLI surface and fixed
  drift: the `/model` colon-syntax bug (real syntax is space- or slash-separated, never
  `provider:model`), de-pinned example model names, and corrected stale REPL slash-command and
  `polyrob doctor`/browser-extra claims. `AGENTS.md`'s documentation map now also lists
  `docs/guide/skills.md` and `docs/guide/console.md`.

- **2026-07-03 — Deploy / onboarding / update readiness (Fusion opus4.8-4.8 review, 10 items).**
  A deep review of the deploy/onboard/update/maintenance surfaces for the OSS release +
  self-hosting, then the fixes:
  - **Migration-on-boot + snapshot-before-migrate** (`migrations/boot.py`, wired into the API
    lifespan). Schema migrations previously never ran at boot or on update — the base schema is
    built inline at HEAD and the semver framework was never engaged, so a future `ADD COLUMN`
    migration would hit `no such column` at runtime on an upgraded DB. Boot now baselines an
    at-HEAD schema (no risky replay) and auto-applies genuinely-pending migrations, idempotent +
    single-flight-locked + fail-open (a failure leaves the inline schema, boot can't regress),
    taking a WAL-safe snapshot first so `polyrob update --rollback` finally has real restore
    points. Every non-docker `update` manual step now runs `migrate upgrade`.
  - **Security:** removed a hardcoded `JWT_SECRET_KEY` from the tracked `deployment/*.service`
    units (source it from the EnvironmentFile / generate per-deploy with `openssl rand -hex 32`);
    invariant test guards regression. Parameterized the prod host/SSH-key out of the ops/deploy
    scripts and added `scripts/publish_prune.sh` + `.gitattributes` as the fresh-squash publish gate.
  - **Fresh-clone breaks:** `docker compose up` referenced a gitignored env file → tracked
    `config/.env.example`; removed the red-by-construction CI `skills-ref` job; `update --check`
    resolved its "latest" from PyPI for git/systemd installs (POLYROB isn't on PyPI) → now checks
    GitHub. Docs lead with OpenRouter and drop the DeepSeek-only bootstrap dead-end; `install.sh`
    banner made public. Deleted the dead parallel migration runner (`modules/database/migrate.py`).

### Added

- **2026-07-03 — Skill system agentskills.io P1 (consumer) + P2 (install/marketplace).**
  POLYROB is now a full agentskills.io **client**. **P1:** discovers and lenient-loads
  external skills from `~/.agents/skills/` / `~/.claude/skills/` (user scope, always) and
  per-repo `./.agents/skills/` (project scope, trust-gated via `POLYROB_TRUST_PROJECT_SKILLS`
  — **fail-closed off on servers**, never scans the process CWD); precedence project > user >
  builtin with builtins never shadowed; a confined resource read-path (`load_skill` lists +
  reads `references/`/`assets/` realpath-confined and untrusted-wrapped, **never executes
  `scripts/`**); and `polyrob skills export`. **P2:** `polyrob skill install <local folder |
  owner/repo[/subdir] | git URL | SKILL.md URL>` → validate → fail-closed threat-scan of the
  `SKILL.md` and every text resource → `.pending` quarantine → `polyrob skill approve`; a
  sandboxed git clone (hooks/config disabled, symlink + traversal audit at the git-object
  level, byte/file caps, recorded commit SHA); remote sources never auto-approve; a server
  **hard-gate** (install/approve are owner/CLI-only, no REST endpoint); an install audit trail
  (source + resolved SHA + approver, origin persisted at quarantine); and `polyrob skill
  list/info/remove` + REPL `/skills`. New user-facing guide: `docs/guide/skills.md`. Installed
  skills live under `<data_home>/skills/user_<uid>/` and survive `polyrob update`.

- **2026-07-03 — Episodic activity ledger + idle-reset continuity bridge.** A durable,
  time-ordered `episodes` table in `memory.db` records one row per completed run
  (chat/goal/cron), independent of H-MEM's findings-driven memory — so a routine run that
  produced no "finding" is still visible in cross-session recall (the "what did you run in
  the last 8 hours?" incident, where 3 real goal completions were otherwise invisible to a
  fresh chat session). Write sites: goal-dispatcher completion, cron-runner completion, and
  chat session reset/cleanup (`modules/memory/episodic.py::finalize_episode`, plus
  `collect_provenance` for spend/steps/artifacts). Consumers: an agent-callable
  `recent_activity(since=...)` action (sees everything), a passive session-start digest
  pinned as a foundation message on chat sessions only (`exclude_surfaced=True` — omits rows
  already delivered out-of-band), and an idle-reset **continuity bridge** that writes a
  closing episode and seeds the next session's first step with a short "what happened last
  time" message (mechanical by default; `CONTINUITY_LLM_SUMMARY` opts into an aux-model
  summary, off everywhere by default). **Retention + dedup (this task):**
  `SqliteMemoryProvider.prune_episodes` runs a global (all-tenants) retention sweep on the
  curator's own tick cadence (`EPISODIC_RETENTION_DAYS`, default 90) — never the write path;
  `mark_episode_surfaced` flags a session's episode as already-reported after a successful
  cron out-of-band delivery or goal self-wake, so the digest doesn't repeat what the owner
  was already told. All new flags are additive, fail-open, and gated OFF by default on the
  multi-tenant server / ON under `POLYROB_LOCAL` (see `docs/CONFIGURATION.md` for the full
  flag table: `EPISODIC_MEMORY_ENABLED`, `EPISODIC_DIGEST_INJECT`,
  `CONTINUITY_BRIDGE_ENABLED`, `CONTINUITY_LLM_SUMMARY`, `EPISODIC_RETENTION_DAYS`). The
  `memory.db` schema change (`CREATE TABLE IF NOT EXISTS episodes`) is additive/safe but
  requires owner sign-off before enabling on the production single-owner VPS.

- **2026-07-03 — Skill system: agentskills.io-compliant `SKILL.md` frontmatter + data-safety.**
  Every bundled skill (`data/prompts/skills/<id>/SKILL.md`) now carries real, spec-compliant YAML
  frontmatter — `name` (== dir), `description`, `license: MIT`, and a flat string→string `metadata`
  block (`polyrob-priority`/`polyrob-auto-activate`/`polyrob-triggers` as a JSON string/`polyrob-version`)
  — parsed by a real YAML frontmatter layer (`agents/task/agent/skill_frontmatter.py`) that's
  authoritative for name/description and strips the frontmatter before injecting the body. A two-role
  validator backs this: `polyrob skills validate` (strict library-wide check with no argument, mirroring
  the agentskills.io rule set — name charset/length/dir-match, description length, metadata shape, no
  extra top-level keys) plus a `polyrob doctor` compliance line and a fail-open boot warning that logs
  (never blocks startup on) a non-compliant skill; the same validator backs the `test` CI job via
  `tests/unit/agents/task/test_library_invariants.py` (a separate CI job that shelled out to the
  upstream `agentskills`/`skills-ref` reference package was tried and removed the same day — it needed
  an unpinned placeholder commit SHA and reded the badge on every push for no runtime benefit).
  **Data-safety:** per-tenant authored/imported skills moved from the installed package tree to
  `<data_home>/skills/user_<uid>/` (`agents/task/agent/skill_store.py`), so a `polyrob update` code-swap
  no longer destroys them; a one-time, idempotent, lock-guarded migration moves any pre-existing
  code-tree skills (incl. `.pending`/`.archived` history) into data-home on first boot, and
  `cli/update/context.py` now snapshots `<data_home>/skills` on every update so authored skills survive
  an update or rollback. Also: the flat 12000-char hard-reject cap is replaced by a 40000-char on-disk
  ceiling plus a 20000-char (~5000-token) warn-only recommended-injection-size threshold; repeated
  `load_skill` calls for an already-active skill now short-circuit to a small ack instead of re-emitting
  the full body each time; and skills with `auto_activate:false` (e.g. the trading skills) stay hidden
  from the model's `<skill-catalog>` while remaining reachable via seeded/persona force-include.

- **2026-07-03 — POLYROB Console: deployment posture model + owner login + rebrand.** The
  webview gains a single posture SSOT (`webview/webgate.py::posture()`): **Posture 0 `local`**
  (loopback, no auth — today's default, unchanged), **Posture 1 `own_ops`** (public host, no
  billing, console gated by a simple **username/password owner login** — no wallet/SIWE
  required), and **Posture 2 `multitenant`** (full SaaS UI — auth/ownership/billing on).
  `POLYROB_POSTURE` is the explicit override; `WEBGATE_MULTITENANT=true` is a back-compat alias
  for Posture 2; `polyrob dashboard --posture`/`--multitenant` are the CLI-facing spellings.
  Owner login (`POLYROB_OWNER_USERNAME` + `POLYROB_OWNER_PASSWORD_HASH`, argon2, never
  plaintext) issues the same JWT-backed session cookie the rest of the console already used,
  with constant-time verification against a precomputed dummy hash so a wrong username can't
  be distinguished from a wrong password by timing. The webview UI is rebranded **"POLYROB
  Console"** (`console_display_name()`, opt-in override via `POLYROB_CONSOLE_NAME`) with a new
  env-driven `branding_config()` (support link/handle, access-gate label, footer/terms/privacy
  URLs) so an OSS fork isn't locked to the original instance's domain copy baked in at authoring time; the
  previously-dead Terms/Privacy `href="#"` links now render real URLs. A new unauthenticated
  public status page is served at `/` for `own_ops`/`multitenant`.

### Fixed

- **2026-07-02/03 — Payment system realignment (C1-C9).** Closed a live **double-billing** bug
  where a credit-paying request was charged once at the API gate and again by the per-token
  usage tracker — the gate now only *authorizes*, `LLMUsageTracker` is the sole deduction path.
  Collapsed x402 pricing onto **one SSOT price** (`X402_PRICE_USD`, read by the middleware
  charge, `/api/x402/pricing`, the Agent Card, and the 402 challenge alike), removing the
  now-dead 402-challenge 1.5x premium and a dead premium-tier override. Removed the dead
  `POST /api/x402/create-payment` endpoint (it always 503'd) and fixed the Agent Card's x402
  `SecurityScheme` header to the real `X-PAYMENT` handshake the middleware actually reads —
  A2A and `/v1` clients now get a real, working 402 challenge instead of a broken one.
  Consolidated 4 separate auth middlewares onto one canonical `request.state` contract
  (`api/auth_state.py`), fixing an API-token `role=` gap that fell through to an unconditional
  402. Unified the two historical master-seed env var names (`PAYMENT_MASTER_SEED` /
  `MASTER_SEED`) onto one `resolve_master_seed()` SSOT so both call sites derive the same
  deposit addresses. **Finished the deposit monitor**: replaced a hardcoded `3000.0` ETH/USD
  price with a live oracle (capped by `ETH_PRICE_USD_MAX`, overridable via
  `ETH_PRICE_USD_OVERRIDE` for ops/testnets), added user notification on a credited deposit,
  and closed a crash-window double-credit gap (credit + dedup row are no longer two separate
  autocommitted writes). Documented and locked the posture↔billing gate: `ENABLE_AUTH` (not
  `ENABLE_CREDIT_SYSTEM`, which has defaulted on since before this work) is what actually keeps
  every billing service unregistered on Posture 0/1.

### Security

- **2026-07-02/03 — Cross-tenant IDOR + SIWE hardening (E1/E4/E8/E9, S1/S5/S9).** Closed live
  cross-tenant IDORs: session endpoints (`POST /sessions/{id}/messages`, `GET /sessions/{id}`,
  `GET /users/{id}/sessions`, session cancel/queue-status), cron job cancel/get, and A2A
  push-notification-config set/get/delete all trusted a path-param id with no ownership check —
  any authenticated caller could read/write another tenant's session or cancel another
  tenant's cron job/goal. Gated Socket.IO `connect`/`join_session` by resolved tenant identity
  (was streaming a session's full historical + live feed to anyone who joined the room, in
  **both** `own_ops` and `multitenant` posture — two separate fixes, the first left `own_ops`
  open). Bound SIWE nonces to their issuing chain id and closed a fail-open bypass where
  omitting the `Chain ID:` line from the signed message skipped the check entirely — closes a
  cross-chain signature-replay window. Closed an owner-login username-enumeration timing
  oracle (constant-time verify regardless of username match). Wired two previously-dead
  guardrails into their startup paths: Socket.IO event rate-limiting and expired-SIWE-nonce
  cleanup.

- **2026-07-02 — Owner-message responsiveness (architecture, P1-P4/P6/P7 of the overnight
  handoff).** Root cause of "Rob answers a stale topic / says 'No new user input'" on the
  long-lived owner chat: `inject_user_guidance` inserted every new user message at history
  **position 1**, so on a resumed ~100-message session the fresh question sat at the top,
  buried under a tail of stale "Task Complete / no new input" turns (prod session
  `fa1212de`). New user messages are now **appended at the history tail** with a wall-clock
  `received <ts> UTC` stamp. Supporting fixes: message **origin is persisted** across
  save/load (`MessageOrigin.SELF_WAKE` added; pre-existing files default to `user`);
  metadata timestamps are stamped at add time and survive rehydrate; a **no-input resume
  gate** (a COMPLETED session only re-runs when genuine queued input exists) kills the
  "resume-to-check" LLM burn and its history pollution; an all-forged drain batch
  (self-wake / delegation-result) is framed as `🤖 AUTONOMOUS RE-ENTRY`, never as a user
  message; Twitter `post`/`reply`/`quote` **auto-split >280-char text into a chained
  thread** instead of dying at param-model validation (the real cause of "engage goals
  posted nothing" — not a missing `twitter_twitter_reply` action, which is the correct
  namespaced name). STEER ingress kind-preservation pinned by regression tests.

### Added

- **2026-07-02 — Objective-driven goal system (P5 T1-T10).** The durable goal board
  (`agents/task/goals/board.py`) gains a `kind` column so a `GoalBoard` row can be a **standing
  objective** (`kind="objective"`, e.g. "grow the substack") as well as a one-shot goal; goals
  created against an objective set `parent_id`, and objectives never themselves get dispatched.
  **Mechanical dedup**: `GoalBoard.create` normalizes the new title and trigram-matches it
  against the tenant's goals from the last 7 days, raising `DuplicateGoalError` (with the
  matched goal) on ≥0.6 similarity — no LLM judgment required, closing the 2026-07-01
  seeder repeat-goal burn (`GOAL_DEDUP_THRESHOLD`, `force=`/`--force` overrides). A new
  **in-runtime planner** (`agents/task/goals/planner.py`, gated `GOAL_PLANNER_ENABLED`,
  default OFF) fires from the dispatcher tick when the ready queue is thin: its prompt is
  built by code from live state — active objectives, recent done goals WITH outcome notes,
  blocked goals with errors, and a deliverables listing — replacing the standalone
  `scripts/seed_goal_seeder.py` cron (now deprecated; it regenerated generic themed goals
  with no objective/dedup/outcome awareness). `GOAL_DAILY_QUOTA` (default 6) caps goal RUNS
  started per trailing 24h — the mechanical backstop against runaway burn. The dispatcher
  (`agents/task/goals/dispatcher.py`) injects the parent objective + acceptance criteria
  into every goal run and honors an **outcome contract** — a completed goal's
  `OUTCOME: <text>` line is parsed off the run result and persisted to `payload["outcome"]`
  for the next planner prompt. **Intervention always wins**: cancelling/pausing a running
  goal now survives its in-flight completion (status guards + `stale_completion` audit
  events), and goal/objective MUTATIONS are REFUSED from autonomous (goal/cron-spawned)
  sessions — only the owner (CLI or chat) can change objectives. **Goal self-wake now
  defaults OFF** (`GOAL_SELF_WAKE_ENABLED`, was unconditional). CLI gains
  `polyrob goals objective add|list|pause|activate|drop`, `goals edit`, `goals tree`, and
  `goals create --tools/--acceptance/--objective`; the `goal` tool gains the matching
  objective/update actions.

- **2026-07-03 — Voice-transcript echo.** Voice notes now echo their transcript back into the chat as a persistent, voice-note-anchored message (`🎙️ Transcript: "…"`) before the agent answers, on Telegram and WhatsApp. WhatsApp additionally marks the voice note read (✓✓). Gated `VOICE_TRANSCRIPT_ECHO` (default on); `=false` restores prior behavior.

## [0.4.2] — 2026-07-01 — Initial open-source release

### Added

- First public, self-hostable release of POLYROB under the MIT license.
- Added `NOTICE`, a sanitized `config/.env.production.example` template, and a public
  CI workflow (gitleaks + pytest); removed the production/dev auto-deploy workflows.
- **Crypto trading tools — finalization (Polymarket + Hyperliquid).** Migrated Polymarket off the
  **archived/non-functional `py-clob-client`** to the maintained **`py-clob-client-v2`** behind a single
  adapter seam (`tools/polymarket/clob_adapter.py`): a missing client is now a typed
  `POLYMARKET_CLIENT_MISSING` error (+ install hint), not a silent read-only degrade. Reconciled the v2
  method names and routed Polymarket portfolio reads (positions/trades) to the **public Data API** (no
  wallet). Added the missing PM `place_market_order` (marketable order reusing all trade gates) and wired
  the hidden `get_balance`. **Read/trade split (additive):** new wallet-free, delegatable
  `polymarket_data` / `hyperliquid_data` read tools (in the `research` / `trading_research` toolsets);
  the `polymarket` / `hyperliquid` trade tools keep every gate. Hyperliquid: reads + delegated-signer
  trades now target the **master** account (never the empty agent address), plus `agent_status` and the
  `approve_agent` / `revoke_agent` delegation lifecycle. **Live-trade kill-switch** (`CRYPTO_TRADE_LIVE_ENABLED`
  + per-venue `*_TRADING_ENABLED` + `*_TRADE_MAX_USD`, all OFF/dry-run by default). Fixed a
  correspondent-gate bug where the `"trade"` substring false-blocked the `get_trade_history` read while
  not gating real trade verbs. Added 7 read-first crypto skills. Live order placement remains
  **testnet-gated**. See `docs/CONFIGURATION.md` → "Crypto trading".
- **Lightweight web reading by default (`web_fetch`, Tier-1):** new stateless
  `web_fetch` tool (`fetch_url(url) -> markdown`, aiohttp + markdownify, no browser) is now the
  default web reader in `server_default_tools()`, the `research`/`full` toolsets, and the CLI base
  list. The heavyweight Playwright `browser` tool is **opt-in** — it stays in the
  `browser`/`development` toolsets and `tool_ids=['browser']`, and `playwright` moved out of
  `requirements.txt` into the `[browser]` extra (`pip install '.[browser]' && playwright install
  chromium`), so a default install no longer pulls ~300 MB of Chromium. SSRF-hardened (per-hop
  `validate_and_resolve` + IP pinning, no auto-redirects, byte/redirect caps); fetched content is
  auto-wrapped as untrusted (prompt-injection framing). New flag `WEB_FETCH_ALLOW_PRIVATE_URLS`
  (default false). Removed the dead, undecorated, SSRF-unsafe `filesystem_docproc.process_url` /
  `process_web_content` it supersedes. Plan:
  `docs/plans/2026-06-29-web-fetch-tier1-IMPLEMENTATION-PLAN.md`.

### Changed

- **Canonical AI-agent doc renamed `CLAUDE.md` → `AGENTS.md`.** The ~935-line architecture/landmines/
  invariants doc that lived in `CLAUDE.md` now lives in the vendor-neutral `AGENTS.md` (read by Claude
  Code, Codex, Cursor, and any tool honoring the `AGENTS.md` convention); `CLAUDE.md` is now a 7-line
  pointer (`@AGENTS.md`) so Claude Code still loads it automatically. All 11 per-package `README.md`
  cross-references and `RELEASING.md`'s ship-list were updated to point at `AGENTS.md`. Fixed a batch of
  issues surfaced by review of the rename: `AGENTS.md` had stayed on the pre-rename `ROB_LOCAL` flag name
  (code and every other doc use `POLYROB_LOCAL`); `docs/guide/getting-started.md` documented three
  slash commands (`/new`, `/skills`, `/load`) that don't exist in the REPL command registry and
  mischaracterized `/memory`; its "Where Data Lives" tree pointed at a stale `./data/task/` instead of
  the actual `./.polyrob/` runtime root; its "Extras Reference" table was missing the real `voice`
  extra; it labeled `~/.polyrob/cli.json` "legacy" while also instructing `model set-default`, which
  writes into it; and it told macOS users to `brew install polyrob`, which doesn't exist. Also
  deleted the dead `[tool.pytest.ini_options]` block from `pyproject.toml` (silently ignored because
  root `pytest.ini` wins pytest's config-file precedence) and added `tests/unit/docs/
  test_claude_agents_sync.py` as a regression guard against `CLAUDE.md`/`AGENTS.md` drift.
- **Project-context file: native `polyrob.md` + precedence + server opt-in.** The C9
  runtime project-context loader (`agents/task/agent/core/project_context.py`) now recognises a
  native **`polyrob.md`** / `POLYROB.md` name at top precedence, so a user can give POLYROB
  per-repo guidance without editing the `AGENTS.md`/`CLAUDE.md` their other agents read.
  **Behavior change:** recognised names are no longer concatenated — the highest-precedence name
  that exists anywhere on the CWD→git-root walk wins (`polyrob.md` > `POLYROB.md` > `AGENTS.md` >
  `CLAUDE.md` > `.cursorrules`), most-local instance, then stop (previously a repo with both
  `CLAUDE.md` and `AGENTS.md` injected both). New `PROJECT_CONTEXT_SERVER_MODE` (default OFF, NOT a
  safe-local flag) lets the loader run on the multi-tenant server, where the file is injected
  **untrusted-wrapped** (`<untrusted_tool_result>` DATA framing) since it may come from a repo the
  operator merely opened; local CLI stays trusted/steering. Server-mode searches the **tenant
  session workspace** (`pm().get_workspace_dir`), never the process CWD/install dir, so one
  tenant's session can't read the deployment's own files or another tenant's. Server is
  byte-identical by default (both gates OFF). Secret-skip + `is_suspicious` fail-closed scan + 20k
  cap unchanged; a flagged high-precedence file falls through to a clean lower-precedence one.
  Proposal: `docs/plans/2026-06-30-polyrob-project-context-file-PROPOSAL.md`.
- README rewritten for a self-hosted single-user instance (`pip install -e ".[dev,all]"`,
  `polyrob doctor`, `polyrob chat`, `polyrob dashboard`).

### Fixed

- **Persistent project workspace (Model C):** `POLYROB_PROJECT_DIR` / `rob --project <path>` makes the
  agent operate in one launched folder across sessions/goals/cron, decoupled from `POLYROB_DATA_DIR`
  (fixes headless multi-session fragmentation — the battle-test "shared toolkit" goal that scattered
  527 files across 77 session folders). Single-flight goal concurrency in project-root mode (keyed off
  the installed `pm()`, never an env var); cross-process workspace lock keyed off the project, not the
  data home; SEC-1 startup warning when the cwd-default workspace holds secrets/`.git`. Multi-tenant
  server behavior unchanged (ratchet-tested). Default (unset) is byte-identical to prior behavior.

## 2026-06-27 — Launch-finalization program (docs 01–03)

- **Runtime isolation + packaging (doc 01).** New `core/runtime_paths.py::resolve_runtime_paths`
  centralizes code/config/data-home/workspace roots; the **server** runtime data anchors to
  `POLYROB_DATA_DIR` (a data-home OUTSIDE the code tree) when set — so the agent workspace is no
  longer a sibling of `config/.env.*` secrets. New `polyrob serve` console entry (binds 127.0.0.1)
  and a `core/assets.py::webgate_asset_dir` package-asset seam (packaged `web_dist` → dev fallback).
  `polyrob doctor` now reports `workspace isolation`. New flag: **`POLYROB_DATA_DIR`** (server
  data-home; unset = legacy base_dir anchoring).
- **Framework rename rob → polyrob (doc 02).** `core/paths.py::polyrob_home()` (`~/.polyrob`,
  `POLYROB_HOME` override); one-time copy-not-move `~/.rob → ~/.polyrob` migration
  (`core/home_migration.py`, fail-open, marker-gated) with a read-only `~/.rob/.env` fallback overlay.
  Clean-break `ROB_PERSISTENT_INPUT→POLYROB_PERSISTENT_INPUT`,
  `ROB_ENV_KEY_BACKFILL→POLYROB_ENV_KEY_BACKFILL`. `/opt/rob → /opt/polyrob` across the deploy
  script + env templates (now set `POLYROB_DATA_DIR=/var/lib/polyrob`); service files renamed
  (`polyrob-api.service`, `polyrob-webgate.service`), stale repo-root `rob.service` deleted. Closes
  the `/opt/rob ↔ /opt/polyrob` deploy/nginx mismatch. `instance_id="rob"` unchanged.
- **Single-user webgate (doc 03).** `webview/webgate.py` gates the multitenant stack behind
  **`WEBGATE_MULTITENANT` (default OFF)**: the webview now boots loopback / no-auth / no
  signin·profile·admin by default (flag ON = today's multitenant behavior, byte-equivalent). New
  `polyrob dashboard` launcher; new v1 read-only pages **Memory / Autonomy / Identity / System**
  that reuse existing services (MemoryProvider.search, GoalBoard, CronService, `core.instance`,
  `doctor_report`). Rebranded to POLYROB; webview assets ship in the package. New flags:
  `WEBGATE_MULTITENANT`, `WEBGATE_HOST`/`WEBGATE_PORT`, `POLYROB_LOCAL_OWNER`.

## 2026-06-25

- **Three-tier chat-surface access model (WS-A, default OFF — `CORRESPONDENT_ACCESS_ENABLED`).**
  Multi-user surfaces previously turned every routed inbound into a STEERING user-turn the agent
  obeys. WS-A adds OWNER / CORRESPONDENT / DENIED, resolved once at the routing boundary
  (`core/surfaces/access.py::resolve_access_tier` → `dispatcher.py`). A **correspondent** (a third
  party the agent emailed, tracked in the `core/surfaces/correspondents.py` SQLite registry — the sole
  routing authority, keyed on the authenticated sender ADDRESS so an unknown sender never resolves)
  has its reply delivered as **DATA into the originating session only**, via a
  `MessageOrigin.CORRESPONDENT` control message (`<correspondent-message>` envelope + inner
  `wrap_untrusted`) on the **ephemeral/observation channel — NOT the user obey-queue**
  (`orchestrator.inject_correspondent_message` / `TaskAgent.deliver_correspondent_data`); it can never
  reach COMMAND/STEER/TASK_AGENT. OFF ⇒ byte-identical. Design + 2× Fusion (`opus4.8-4.8`) reviews in
  `docs/plans/2026-06-25-chat-surface-finalization-WS-A-RESULT.md`.
- **Fusion-driven security hardening (folded into WS-A).** A post-implementation Fusion review found
  and we fixed: the local-owner bypass is now **surface-scoped** (`is_owner(local=True)` no longer owns
  a forgeable email/telegram sender); the dispatcher tier block is **fail-CLOSED** once the model is on
  (a resolver/registry fault denies, never falls through to STEER); a **capability gate**
  (`agents/task/agent/core/correspondent_gate.py`, fail-closed pre-tool hook) denies high-impact tools
  (money/comms/code-exec/delegation/browser) while a session is correspondent-tainted; closing
  fence-tags in correspondent bodies are neutralized (no delimiter breakout); `user_id` is in the
  correspondents PK (no cross-tenant leak); the awareness frame
  (`core/instance.py::owner_awareness_line`) is emitted whenever the model is on.
- **Email surface (WS-B, default OFF — `EMAIL_SURFACE_ENABLED`; ON for `polyrob email`).** IMAP-poll
  inbound + buffered SMTP outbound inheriting the base `Surface` engine (`surfaces/email/`). v1 is
  **correspondent-only — owner-by-email is OFF** (forgeable `From:`). Inbound is transport-free
  (`process_email`: Message-ID/surrogate dedup → identify → route; `truncate_quoted_history` strips
  quoted history incl. Outlook/localized attributions, anti-smuggling; marks `\Seen`). Auto-seed is
  approval-gated (`CORRESPONDENT_REQUIRE_APPROVAL` default ON) + per-day capped
  (`CORRESPONDENT_MAX_NEW_PER_DAY`, 20); poll cadence `EMAIL_IMAP_POLL_SEC` (60).
- **Owner/access quick-access (WS-D).** `polyrob owner {show,correspondents,approve,invite}` — inspect
  the bound owner + per-surface posture, list third-party correspondents, approve a pending one, and
  owner-seed a correspondent against a session (`core/surfaces/owner_admin.py`). All new flags are
  documented with code anchors in `docs/CONFIGURATION.md`.

## 2026-06-24

- **Telegram voice transcription (#9, default OFF).** Inbound voice/audio notes are transcribed to
  text before routing (`VOICE_TRANSCRIPTION_ENABLED`), so a voice message is handled exactly like a
  typed one. The engine is **surface-agnostic** (`modules/transcription/`: `Transcriber` ABC +
  faster-whisper impl that loads lazily and runs in a worker thread + `NullTranscriber` fallback when
  the extra is absent) so WhatsApp/etc. reuse it; only the Telegram file download/extraction lives in
  `surfaces/telegram/voice.py`. `process_update` stays transport-free — it takes an injected
  `transcribe_voice` callable (same pattern as `dedup`/`user_directory`); transcription runs after
  dedup so a redelivery never re-downloads. Fail-open throughout.
- **Base-Surface streaming engine (#8, default OFF — engine landed, live wiring is follow-up).** The
  streaming **state machine now lives in the base `core/surfaces/Surface`** (accumulate → throttle →
  render → finalize → split-overflow); a surface plugs in four transport primitives
  (`_open_stream_message`/`_edit_stream_message`/`_send_stream_overflow`/`_stream_target`) + two policy
  hooks, and Telegram shrank to just those (`editMessageText` live edits, flood-throttled via
  `TELEGRAM_STREAM_EDIT_INTERVAL_SEC`, RetryAfter-aware). Proven reusable by a non-Telegram surface in
  tests, and OFF = byte-identical buffered path. **Now wired live:** `feed.py` publishes a turn's deltas
  with a stable per-TURN `stream_id` (the session_key) so they open+edit ONE message, and the turn's
  discrete reply finalizes that bubble in place via `Surface._finalize_live_on_send` (the persisted final
  is the clean curated reply — never raw tokens — with no duplicate message). A bounded `_MAX_LIVE_STREAMS`
  cap bounds any un-finalized stream. **Stays opt-in (default OFF):** intermediate mid-stream frames are
  raw deltas, per-chunk brain-scrubbed (best-effort) by `MessageRouter` — a block straddling a chunk
  boundary can briefly surface before the clean final commit; enable only where that's acceptable.
- **#7 — flip `SESSION_RESET_MODE` server default `none`→`idle`.** Gated on the now-landed #0/#2
  fixes; the idle window still differs by profile (720 local / 1440 server). Pin `none` to restore the
  legacy inert behavior.
- **Chat-finalization bugfixes (sequence items #2–#6).** Hardening the resume/recreate rail behind
  the Telegram surface: (#2) ALL orchestrator-recreation paths now route through one locked
  `TaskAgent._resolve_or_recreate`, so a STEER message racing a self-wake on an evicted session
  can't double-build the orchestrator and orphan one; the `_recreate_locks` map no longer leaks
  (popped on evict + cleared on shutdown). (#3) `ensure_session_and_deliver` returns
  `delivered`/`busy`/`gone` instead of a bool — a full message queue is `busy` (the session is alive),
  so the surface replies "still working" instead of dropping the user into a fresh amnesiac session.
  (#4) `_evict_session` skips a session whose execution lock is held (never reap a live run loop).
  (#5) `/new` now drops the chat binding (`TaskAgent.unbind_chat`) so the next message starts cold.
  (#6) hourly `SURFACE_GC_ENABLED` ticker purges stale `session_chat_map` bindings
  (`SessionChatRegistry.purge_stale`, horizon `max(2× idle, 7d)`). The GC ticker rides `start_autonomy`,
  now started by the standalone `polyrob telegram` command too (Fusion fix) so GC actually runs on the
  surface that mints bindings — not just the API lifespan + REPL.
- **Telegram chat surface (local long-polling) + week-long-chat continuity.** `polyrob telegram`
  runs the full Task agent over Telegram (owner-locked via `ALLOWED_TELEGRAM_USER_IDS`, typing
  indicator, `RouteKind.DENIED` guard). **P0.2 Fix A (the core bug):** a follow-up message after a
  session was evicted (24h TTL / LRU) used to mint a NEW amnesiac session; now `TaskAgent.
  ensure_session_and_deliver` resumes the BOUND `session_id` (resident or recreated-from-disk via
  `load_from_disk`), so a week-long chat keeps its transcript. **P0.1:** `core/surfaces/session_policy.py`
  ports Hermes's idle/daily session-reset policy onto `session_chat_map` (gated `SESSION_RESET_MODE`,
  default `none` = inert until the continuity bridge lands; `SESSION_IDLE_MINUTES`/`SESSION_RESET_HOUR`).
  `SessionChatRegistry` gains `touch`/`delete`. Plan: `docs/plans/2026-06-24-chat-abstraction-multisurface-finalization-FUSION.md`.
- **OpenRouter is the preferred default client.** `modules/llm/profiles.py` `PROFILES` reordered
  so `openrouter` is first → "first provider with a key" prefers OpenRouter whenever its key is
  present (explicit `-p` and `DEFAULT_PROVIDER`/`CHAT_PROVIDER` pins still win). See
  `docs/CONFIGURATION.md` → Default provider preference. Also fixed `OpenRouterClient`'s hard-coded
  default model off the deprecated `x-ai/grok-4.1-fast` → `x-ai/grok-4.3`.
- **Curated OpenRouter model shortlist (+15).** Added vetted OpenRouter models ROB has no native
  client for — `moonshotai/kimi-k2.5`, `minimax/minimax-m2`, `z-ai/glm-4.6`,
  `qwen/qwen3-coder-30b-a3b-instruct`, `deepseek/deepseek-v3.2`, `deepseek/deepseek-v4-flash`,
  `meta-llama/llama-3.3-70b-instruct`, `mistralai/mistral-small-3.2-24b-instruct`,
  `qwen/qwen3-30b-a3b-instruct-2507`, `openai/gpt-oss-120b`, `nousresearch/hermes-4-70b`,
  `nousresearch/hermes-4-405b`, `qwen/qwen3-vl-8b-instruct`, `meta-llama/llama-4-scout`,
  `minimax/minimax-m3`. Pricing/specs live-verified against the OpenRouter models API and pinned
  in `tests/unit/modules/llm/test_openrouter_pricing_verified_2026_06_24.py`. Hermes-4 marked
  `supports_tools=False` (OpenRouter doesn't expose `tools` for it → JSON-fallback path).

## 2026-06-21

- **OpenAI-compatible `/v1` API.** New `api/openai_compat/` surface — `POST /v1/chat/completions`
  (non-streaming, over `TaskAgent.chat_once`) and `GET /v1/models`, with OpenAI→POLYROB
  `(provider, model)` mapping. Gated `OPENAI_COMPAT_API_ENABLED` (default OFF).
- **Filesystem fixes.** Sub-agent writes route to the parent tenant (not `_anonymous_`); redundant
  leading `workspace/` path segments collapsed.
- **Documentation audit & cleanup** (this pass) — drift fixes in `CLAUDE.md`/`README.md`, new
  `docs/CONFIGURATION.md` flag SSOT, this `CHANGELOG.md`, and `docs/plans/` indexing/archival.

## 2026-06-20

- **Skill system finalized** — Tier-0 skill library, drift guard, recall + safety fixes.
- **CLI health.** `polyrob doctor` (read-only health + config legibility) and the `/autonomy` REPL
  command (show loops + scheduled cron jobs/goals). `cron` `wake_agent` cost-gate seam ($0 no-LLM
  tick when `wake_agent=false`).
- **Pricing/telemetry.** Corrected OpenRouter model pricing to live API values; persist
  `cached_tokens` in `llm_usage` JSON.
- **Live-test bug sweep (F-series).** Verbatim file writes (stop `_clean_text` mangling code, F9);
  permanent-error halt classified as `error` not `completed`; failed/suspended sessions counted as
  goal/cron failures; goal-claim heartbeat against double-dispatch; `ToolStatus.DISABLED`;
  idempotent `user_profiles` schema backfill; absolute per-request output cap
  (`LLM_MAX_OUTPUT_TOKENS`); `param_model` dispatch repaired for future-annotated tools.

## 2026-06-19

- **Evolving identity / polyrob foundation (SHIPPED, inert by default).** `core/instance.py`
  (instance_id, owner principal, SOUL/SELF identity docs), `core/self_context_writer.py`
  (guarded `.pending` writes), `core/pairing.py` + owner-allowlist ingress gate, `self_context_manage`
  action. Default-inert (instance_id `"rob"`). Framework rename + two-axis DB keying deferred — see
  `docs/plans/2026-06-21-polyrob-analyze-and-implement-HANDOFF.md`.
- **AnySite as a first-class CLI tool.** Retired the AnySite-via-MCP path; `anysite_api` tool taught
  in the prompt and added to default tool sets.
- **Twitter/X write surface** (gated `TWITTER_ENABLED`); data-retrieval routed to AnySite, posting to
  Twitter. **Wallet** PolicyGate audit persistence + rolling daily spend cap. **Goals** seeder +
  child-goal tool inheritance. Confinement/leak regression tests + ops checklist.
- **Coding tool** surface (`apply_patch` unified-diff applier; default ON under `POLYROB_LOCAL`).
- **20-bug Fusion-audit sweep** across agent/llm/mcp/cron/x402.
- **Dead-code retirement.** Legacy `ChatAgent`, the `managers/` package, and the superseded hybrid
  memory provider removed (HANDOFF-C); HTTP chat consolidated onto the unified Task agent.

## 2026-06-18

- **Local vector RAG finalization.** Compact local sqlite-vec store in `memory.db`; Pinecone/Chroma
  retired. `MEMORY_BACKEND=local_vector` (hybrid keyword+vector). This **superseded** the short-lived
  `rag`/`hybrid` provider-bridge from 06-17. Default backend remains `sqlite` (FTS keyword recall).
- **Singular chat interface (surfaces).** `Surface` contract + `CLISurface`/`TelegramSurface`,
  `UserDirectory`, `SurfacePermissions`, inbound dispatcher, outbound bus. Telegram surface MVP.
- **Agent wallet + native crypto** merged (PR #2) — `core/wallet` (Signer/AgentWallet/PolicyGate),
  gated `x402_pay` tool. Mainnet-enable blockers recorded.
- **Separation-of-concerns / dedup pass.** LLM layer consolidation (single PROVIDER_CONFIG, error
  classifier, tool-generation contract); env truthy-parsing converged to `core.env`; tool/registrar
  factories; cron WAL helper reuse. Branching policy codified: **work directly on `main`**.

## 2026-06-17

- **Terminal-native consolidation.** `ROB_LOCAL` profile flips the safe autonomy flags ON as a group;
  shared `core/autonomy_runtime.py::start_autonomy` runs cron/goal/curator tickers for BOTH the API
  lifespan and the CLI REPL; idle-gate so background goal/cron work skips while an interactive turn is
  busy. Key-aware `resolve_provider_model` (no hard gemini default).

## 2026-06-16 and earlier (June rework highlights)

- **Controller god-file decomposition** — `tools/controller/service.py` 2369→387 lines via focused
  mixins (`execution`, `tool_management`, `introspection`, `action_registration`).
- **Agent upgrades UP-01…UP-12** — async hook pipeline + approval gating, sub-agent least-privilege,
  untrusted-tool-result wrapping, reasoning-token scrubber, prompt-caching breadth, schema sanitizer,
  durable async/background delegation, memory-intelligence activation, dead-surface cleanup.
- **Autonomy loops W1–W7** — self-wake rail, writable skills + background-review fork, cron run-loop
  FIX (it never ran the session), durable goal board, curator, cross-session search.
- **Native LLM layer** — LangChain and the Llama provider removed; the agent loop, adapters,
  tool-calling, caching, and token-counting are all native (`modules/llm/`).
- **Memory backend default-ON** — `MEMORY_BACKEND=sqlite` (FTS5 cross-session recall), tenant-scoped,
  with anonymous-recall refused by default (`MEMORY_REQUIRE_USER_ID`).

See `docs/plans/agent-upgrades-2026-06/` and `docs/plans/finalization-program-2026-06-18/` for the
detailed per-item plans and results.

[Unreleased]: https://github.com/OWNER/polyrob/compare/v0.4.2...HEAD
[0.4.2]: https://github.com/OWNER/polyrob/releases/tag/v0.4.2
