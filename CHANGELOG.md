# Changelog

All notable changes to POLYROB are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.8.1] — 2026-07-21

### 2026-07-20 — Reliability & honesty fixes (live battle-test hardening)

- **Financial-language honesty**: an unpaid fetch / x402 attempt now explicitly
  states it did NOT pay (the proximate cause of a fabricated "payment sent" claim),
  and the agent is steered to `x402_quote` instead of a rejected `max_amount_usd=0`.
- **Owner-delivery priority lanes**: the user-delivery rail is now priority-ordered
  so a credit-death / halt notice can no longer be starved behind ordinary chatter
  under the flat FIFO send cap.
- **Credit-death sentinel reachability**: the fatal-halt branch the sentinel needs
  was unreachable — it now walks the exception chain to recover the 402's billing
  text, and re-trip notices no longer self-dedupe (each trip carries its timestamp).
- **Message tool**: results acknowledge attached media (killing a retry-to-`BLOCKED`
  loop), carry real content evidence + the true error for the completion judge, and
  resolve `owner` as a target alias before access-tier resolution.
- **Code execution (docker)**: the sandbox workspace bind-mount is writable by the
  forced uid, and `chmod` recurses into pre-existing subdirs.
- **Filesystem tool**: `write_file`/`append_file` coerce dict/list content to JSON
  instead of erroring.
- **Surfaces / Twitter**: a goal's owner-notify message is no longer literally
  addressed to the bot's own handle; Twitter `media_paths` resolve against the real
  session workspace; and the agent is steered away from posting debug-scratch text
  to the live account.

### 2026-07-20 — Dynamic tool rig S3+S4: mcp un-exclusion + create-time narrowing removed

- **`mcp` registers in the CLI/headless container** (S3 tail; browser was un-excluded
  by the maint loop @14381f62): `MCPTool.__init__` is config parsing only, so it left
  `_CLI_INCOMPATIBLE`; registration is gated by explicit `MCP_ENABLED`, the
  autonomous-mode capability default, or local server files (`_cli_extra_gate`).
  Missing gateway secrets (`MCP_GATEWAY_TOKEN`/`ANYSITE_JWT`) now fail loudly at
  load/connect time — an owner ask — never a silent "not found in container".
- **`goal_create` stops narrowing** (S4): under `TOOL_PROGRESSIVE_DISCLOSURE`, an
  inference-only goal no longer writes keyword-guessed `payload.tools` (which
  short-circuited dispatch's wide `default_goal_tools()`); dispatch-time inference
  remains as a widening hint, explicit tools + baseline union unchanged, flag off =
  byte-identical. Seeding doctrine ("omit payload.tools unless deliberately
  narrowing or granting a money verb") stamped into `scripts/seed_goal.py`.

### 2026-07-19 — Dynamic tool rig S1+S2: honest `<tool-catalog>` + self-serve `load_tool` (progressive tool disclosure)

- **Every session can now SEE the whole tool universe and self-serve what it needs**
  (owner directive: the static, silently-narrowed toolset was the rigidity behind
  the "17 steps researching around a missing browser" failure). Gated
  `TOOL_PROGRESSIVE_DISCLOSURE` (default OFF; **ON under `POLYROB_LOCAL`**).
- **S1 `<tool-catalog>` foundation block** (`tools/tool_disclosure.py`, pinned as a
  `TOOL_CATALOG`-origin control message like skills): one line per known tool with
  an HONEST status — `loaded`, `loadable — load_tool("<id>")`, or `gated:<reason>`
  with the remedy channel (`money` explicit-grant-only / `leaf-blocked` /
  `unavailable-on-this-deploy` naming the missing config). Pure render over the
  existing SSOTs (`tools/descriptors.py` + `core/tool_capabilities.py` + the
  container); the system prompt stays byte-stable/cacheable.
- **S2 `load_tool(tool_id)` action**: materializes a `loadable` tool mid-session
  through the SAME `load_tools_from_container` path session creation uses — its
  schemas appear on the next step (registry cache self-busts). Money tools are
  NEVER loadable (explicit owner/goal grant only); delegate-blocked ids refused
  for leaf/sub-agent turns (honors the `DELEGATE_BLOCKED_TOOLS` env override);
  correspondent-taint/posture/approval gates unchanged — loading registers
  schemas, it grants no execution rights. Refusals are STRUCTURED
  (`gated:<reason>` + remedy), killing the silent-drop failure mode.
- S3 (lazy construction for `_CLI_INCOMPATIBLE` heavy tools, browser first) and
  S4 (`goal_create` stops writing narrow inferred `payload.tools`) shipped
  alongside — see the S3+S4 entry above.

### 2026-07-19 — Deliverable reachability: completions attach their files, console deep links, /kb + /files (proposal 021)

- **Goal/cron completion pushes now carry their deliverables.** The owner push is
  built from the run's artifact registry (`agents/task/goals/deliverables.py`):
  files attach to the Telegram message as documents/photos (screened + capped),
  everything else is listed honestly as `server-only: <path> (<reason>)` — never
  again a bare filename the owner can't open. Attaching is gated
  `DELIVERABLES_ATTACH_ENABLED` (ON under `POLYROB_LOCAL`), capped by
  `DELIVERABLES_ATTACH_MAX_MB` (10) / `DELIVERABLES_ATTACH_MAX_FILES` (3).
- **One shared attach-eligibility seam** (`core/surfaces/attachments.py`):
  workspace confinement (relocated from the `message` tool), per-file size cap,
  secret-shaped-filename refusal, bounded prompt-injection threat scan
  (fail-closed on scanner error). The `message` tool's `media_paths` now rides
  the same screen; the delivery rail (`deliver_user_message`) and the
  out-of-band `TelegramBotSink` gained media transport (per-entry fail-open —
  a media fault never takes the text down).
- **Console deep links** (`WEBVIEW_PUBLIC_URL`): completions append the
  owner-auth webview `/session/<id>` link; the daily digest appends a console
  line. Unset ⇒ byte-identical.
- **Webview browses the agent's ACTUAL workspace root:** with
  `POLYROB_PROJECT_DIR` set the agent runs pm() in project-root mode but the
  webview's own process didn't — its file browser showed the EMPTY per-session
  dir while artifacts sat in the project dir. Startup now applies the same
  mode, single-tenant postures only.
- **Telegram `/kb <query>` and `/files [n]` owner verbs** — the phone-first
  owner's read path into the knowledge base and the artifact registry
  (previously CLI-only; "ingested into KB" was write-only theatre from chat).
- **Goal-run prompt teaches attachment:** report a produced file to the owner
  WITH `message(media_paths=[...])`; oversized/refused files get the full
  server path instead.
- **Same-day review fix wave** (two independent review passes):
  layering-ratchet repair (threat scanner dependency-injected out of core),
  content-level secret refusal via `core/secret_scrub` (text AND binary heads),
  write-attribution widened to all ledger output kinds + unattributed files
  listed (never dropped), attached lines carry the absolute server path (text-only
  re-deliveries stay reachable) + `attachments` attrs on `user_delivery` events,
  `MESSAGE_MEDIA_MAX_MB` (45) decouples the explicit `message`-tool cap from the
  10 MB auto-attach cap, cross-tenant media guard in `_notify_owner_done`,
  webview file endpoints refuse credential-shaped files, sink caption truncation,
  `/files` episode window scaling.

### 2026-07-19 — Avatar pipeline: one-time random setup, native headless renderer, voice surfaced

- **Avatar setup is now a ONE-TIME flow: draft → randomize → keep.**
  `pfp generate` mints a RANDOM DRAFT identity (fresh shuffle variant → new face +
  voice per instance) instead of silently freezing the committed stock face every
  install; `pfp randomize [face|voice]` re-rolls the draft (everything / face-only /
  voice-only, studio shuffle semantics); `pfp keep` (or `pfp pick`'s save) accepts it
  and locks the identity PERMANENTLY. A kept identity cannot be changed by any verb —
  `modules/pfp/store.py` raises `PfpLockedError` on any identity-changing write
  (pixels-only re-render of the same identity stays allowed); `pfp push` requires a
  kept identity; a pre-lock-era `pfp.json` is treated as kept. REPL: `/pfp generate` /
  `/pfp randomize [face|voice]` / `/pfp keep`. `--stock` reproduces the committed
  identity, `--seed`/`--variant` pin a specific roll, `--config` keeps the
  frozen-blob path.
- **Native headless renderer (`modules/pfp/still.py`):** the Mindprint dot pass
  ported to Pillow/numpy over the parity-tested field port. Render chain is now
  Chromium (exact engine) → native mesh renderer (same face, no browser) →
  committed reference (STOCK identity only). A randomized identity can no longer
  be silently replaced by the reference PNG's pixels.
- **Setup lets you SEE and HEAR the identity on both surfaces.** CLI/REPL: every
  setup step renders the face inline (truecolor TTY) and the new
  `polyrob pfp say [text]` / `/pfp say` speaks the voice signature through the
  native TTS engine (`modules/pfp/voice.py` — macOS `say` with timbre→clear-voice
  mapping + semitone pitch shift, `espeak-ng`/`espeak`, Windows SAPI SSML;
  fail-open with web pointers when no engine exists). Web: the webview /identity
  page now runs the full setup — live face, DRAFT/KEPT state, 🔊 hear-voice
  (browser speechSynthesis, studio timbre mapping), and draft-only
  re-roll/keep controls over `POST /api/pfp/{generate,randomize,keep}`
  (403 read-only; store-enforced lock contract). Config-shuffle helpers moved to
  `modules/pfp/identity.py` (CLI re-exports them unchanged).
- **`pfp pick` freezes into the instance identity home** (renders png + meta that
  the webview /identity page, invoice cards, and `pfp push` actually read) instead
  of writing the repo's `avatar/config/rob.json` (read-only under pip installs;
  changes never propagated without a manual `generate --force`). `--out` exports
  the chosen config JSON.
- **Generate/randomize now report the full identity + next steps** (face traits,
  the voice signature, and view/re-roll/push/console pointers) instead of a bare
  PNG path; `/pfp status` shows traits + voice too.
- **`pfp push --discord` (flag `PFP_PUSH_DISCORD`, default OFF):** sets the Discord
  bot avatar live via `PATCH /users/@me` (`DISCORD_BOT_TOKEN`, hash-idempotent,
  fail-open) — Discord was the one API-capable surface with no avatar push.

## [0.8.0] — 2026-07-19

### 2026-07-18 — Proposal wave 010A/012/015/016/019-cap: outage honesty + delivery-cap starvation + acceptance gap

- **`LLM_OUTAGE_NOTICE` (default ON, 015 #2):** an owner chat turn that dies on
  total LLM-provider exhaustion (the live OpenRouter-402 shape) now gets one
  static, LLM-free ⚠️ notice over the originating surface (30-min per-chat
  cooldown, fail-open, never for goal/cron runs) instead of pure silence.
- **`llm_provider_exhausted` failure marker (015 #3):** dispatcher failure
  classification now distinguishes a provider outage from a genuine
  refusal/no-op in `goals.last_failure_error`; `intel_scorecard.py` surfaces
  it as a dedicated red flag.
- **Honest episode stats on failure (012 #1):** all dispatcher
  failure-classification paths thread the real `RunOutcome`
  steps/spend/artifacts into `finalize_episode` (previously always 0/0,
  corrupting `noop_ratio` and every consumer of `episodes.outcome`).
- **Self-evolution notifier batching + durable capped record (019-cap #1+#2):**
  `maybe_notify_owner_pending` fingerprints the pending set and re-notifies
  only on change (it had burned 29/30 daily proactive-delivery slots,
  starving the daily digest); a `capped` delivery now writes a durable
  `owner_notice` instead of dropping content irrecoverably.
- **`file_contains` acceptance check (016 #1+#2):** the check type the goal
  planner kept inventing now exists (workspace-relative, bounded read,
  all/any modes); planner + `goal_create` prompts state the exact closed
  type set.
- **`EMAIL_AUTONOMY_RUNTIME` (default OFF, 010 A):** the email process no
  longer runs the goal/cron autonomy runtime, eliminating the coin-flip
  claim of telegram-outbound goals by a process that structurally cannot
  send them.
- **`preferences` explain UX:** field-level schema descriptions + a
  self-correcting missing-`key` error (a goal run had burned its retries
  passing `text=` to explain).

### 2026-07-19 — 019 revalidation fix wave (adversarial 3-reviewer pass over P0–P5)

- **Critical (OpenAI batch tools):** the P5 request-builder extraction left a
  stale `formatted_messages` reference in `_generate_with_tools`'s debug log —
  an unconditional `NameError` (f-strings evaluate eagerly) that broke EVERY
  OpenAI native tool call on the default (non-streaming) path. Fixed +
  regression test that drives the real batch method over a fake SDK.
- **Telegram:** an `act_on_inbound` raise (e.g. `create_session` on exhausted
  credits) unwound past all cleanup — leaking the progress tracker in the
  module registry forever, orphaning the `⚙️ Working…` bubble, and giving the
  user silence. The dispatch is now wrapped: tracker closed, bubble deleted,
  error breadcrumb sent.
- **CLI pairing:** a printed `→` start line could be left unclosed when
  `_should_show_tool` flipped mid-flight (synchronous `delegate_task` sets
  `last_step_sub_agent=True` before its own completion). A PAIRED completion
  now always prints its result line.
- **RunActivity:** eviction is now least-recently-UPDATED (was FIFO-by-first-
  insertion — a long-lived busy session could be evicted by 512 newcomers);
  the snapshot fold now runs AFTER the feed write succeeds, honoring the
  documented "never disagrees with the feed" invariant.
- **Token-streaming brain guard hardened:** fenced ```` ```json ```` starts now
  suppress live deltas too, and a TRAILING brain-state block after prose mutes
  the live stream at the `"current_state"` marker (remainder rides the final
  chunk whole, where the downstream brain scrub works). Content reconstruction
  stays exact; new tests for both shapes.
- **Webview /pending:** the auto-refresh no longer dies permanently after the
  first "Show full" click (visibility tracked separately from the fetch cache).
- **Sub-agent mirror:** `subagent_started` now emits inside the try that
  guarantees its paired `finished`, so a cancellation while queued for a slot
  can't strand the parent phase at `delegating`.
- **Deps:** `openai>=1.26.0` (floor for `stream_options`); anthropic floor
  already adequate (`messages.stream` predates it).
- Also fixed a foreign test's process-wide env leak
  (`test_email_autonomy_gate.py` drove the real `_run_email`, whose
  `CORRESPONDENT_ACCESS_ENABLED` setdefault flipped 6 unrelated telegram
  routing tests to DENIED in full-suite runs). Full suite: 8399 passed / 0
  failed.

### 2026-07-18 — Live run-state observability P5 (proposal 019): true token streaming

- **`LLM_TOKEN_STREAMING` (default OFF):** when ON and the provider client
  implements the new `astream_agent_response` (Anthropic + OpenAI),
  `LLMClientAdapter.astream` yields REAL per-token deltas instead of the
  legacy one-blob chunk — the CLI ResponseBox / webview `stream_chunk` /
  Telegram partials fill as the model writes. OFF = byte-identical legacy.
- **Safety of the stream:** deltas run through a per-call
  `StreamingThinkScrubber` (a `<think>` block split across delta boundaries
  never leaks); a completion starting with `{` (brain-state JSON) suppresses
  live deltas entirely so raw JSON never streams to the user (final chunk
  then carries the whole content — exact legacy shape). Tool calls, usage
  metadata, and the per-call provider-response billing id ride the final
  chunk, so token accounting and billing dedup are unchanged.
- **Provider plumbing:** the batch request builders were extracted
  (`_build_tool_api_params` / `_build_tool_request_params`) so streaming
  issues byte-identical requests; Anthropic streams SDK `text_delta` events
  then parses `get_final_message()` with the same block parser; OpenAI uses
  `stream=True` + `stream_options.include_usage` with by-index tool-call
  fragment assembly. A pre-first-chunk failure falls back to single-chunk;
  mid-stream failures propagate (a silent fallback would double the text).
- The agent loop, `stream_output` funnel, and both stream consumers were
  already N-chunk-safe — no changes there. Not yet live-smoke-tested against
  a real provider (no key on the dev box); flag stays OFF until the owner
  flips it.

### 2026-07-18 — Live run-state observability P4 (proposal 019): machine surfaces

- **A2A:** an approval wait now streams as A2A's NATIVE `input-required` task
  state (back to `working` on resolution) with the action name in the status
  message; `tasks/get` responses carry `metadata.current_activity` (the same
  RunActivity snapshot as the session-status API).
- **OpenAI-compat:** `stream: true` stays buffered (P5 is the token-streaming
  upgrade) but the agent turn now runs concurrently with the SSE body,
  emitting spec-legal `: keep-alive` comment frames every ~15s so long turns
  no longer hit client/proxy idle timeouts; a failure after headers surfaces
  as an error chunk + `[DONE]` instead of a dead socket. Documented honestly
  in `docs/guide/api.md`.
- Proposal 019 status → IMPLEMENTED (P0–P4); P5 (true token streaming)
  remains deferred pending separate owner approval.

### 2026-07-18 — Live run-state observability P3 (proposal 019): webview truthfulness

- **Per-session state banner:** a page-level banner (visible on every tab)
  driven by live feed events — `⏸ Awaiting your approval: <action>` with
  inline Approve/Deny (reusing the webgate pending actions; hidden on a
  read-only console, ambiguity falls back to `/pending`), `↻ retrying`,
  `📦 compacting`. Cleared by the matching resolution/progress events.
- **First-class feed cards** for every 019 kind (`tool_started` shows
  "running…" the moment a tool dispatches; approval/retry/compaction/
  sub-agent/delegation render as compact one-liners instead of raw-JSON
  generic cards).
- **`/pending` + `/autonomy` auto-refresh** (5s/10s, visibility-gated;
  pending skips refresh mid-action or while a body is expanded) — a newly
  blocked approval or a goal that starts running shows without a manual
  reload.
- **Session-list activity badge:** `[● tool: navigate]` / `[⏸
  awaiting_approval]` etc. from the in-process RunActivity snapshot
  (honest absence when another process owns the session).

### 2026-07-18 — Live run-state observability P2 (proposal 019): Telegram progress

- **Live progress bubble** (`TELEGRAM_PROGRESS_EDITS`, default ON; per-owner
  pref `progress.telegram`): the static `⚙️ Working…` Telegram bubble becomes a
  feed-driven live status line — `⚙️ step 3 · → navigate · 2 tools · 45s ·
  $0.02` — edited in place at most once per 2.5s. Wait states override
  immediately: `⏸ Waiting for your approval — /pending`, `↻ rate_limit —
  retrying in 8s`, `📦 Compacting context…`; a still-blocked approval gets ONE
  reminder edit after 10 min (never a new message). Built on a new
  surface-agnostic `TurnProgressTracker`
  (`agents/task/telemetry/live_progress.py`) + a multi-subscriber feed-callback
  seam (`ProductTelemetry.add_feed_subscriber` — the CLI's single
  `_on_feed_entry` slot is no longer the only consumer, so the gateway's
  one-process surfaces can't clobber each other).
- **Autonomous run START notice** (`AUTONOMY_START_NOTICE`, ON under
  `AUTONOMY_POSTURE=full`/autonomous, else OFF): `▶ goal started: <title>` /
  `▶ cron run started: <task>` pushed via the one owner-delivery rail
  (dedup + caps) at dispatch time — the owner no longer learns of autonomous
  runs only at completion or in the daily digest. Digest and $0 gated ticks
  never notify.
- Deferred within P2: the email finalized-turn summary footer (needs
  RunOutcome→OutboundMessage plumbing; email stays buffered and unchanged).

### 2026-07-18 — Live run-state observability P1 (proposal 019): full vocabulary + snapshot

- **Vocabulary completed** (same `RUN_EVENTS_ENABLED` gate, fail-open):
  `compaction_started/finished` (emergency prune + LLM compaction),
  `retry_wait` (all five backoff sleeps in the step-error handler, with
  reason/delay/attempt/provider), `subagent_started/finished` (mirrored into
  the PARENT session's feed with goal preview + duration), and
  `delegation_dispatched/completed` (background delegation lifecycle — the
  dispatch-to-terminal invisibility gap). Provider failover events
  (`provider_failure`/`provider_fallback_success`) gained CLI + `/activity`
  renderings (they reached only the webview before).
- **CLI:** sub-agent + delegation + provider-failover lines render in the
  default view; compaction/retry are bar-visible states (`✱ compacting (llm)`,
  `✱ retry (rate_limit) 8s`) with trace lines under `/verbose` — all via the
  EventSpec registry seam.
- **`RunActivity` snapshot:** a per-session phase machine (`idle | thinking |
  tool | awaiting_approval | compacting | retrying | delegating | done`)
  derived at the ONE feed choke point (never at emit sites), exposed as
  `current_activity` (phase/detail/seconds_in_state/step/call_id) on
  `GET /api/task/sessions/{id}`; `null` for unknown/remote sessions.
- **`polyrob session tail <id> --follow`:** the command's docstring finally
  tells the truth — keeps streaming new feed events live (ordered,
  dependency-free seq-file poll), so a second terminal can watch any running
  session, including goal/cron runs.

### 2026-07-18 — Live run-state observability P0 (proposal 019): no more dead air

- **Span/wait feed events** (gated `RUN_EVENTS_ENABLED`, default ON, fail-open):
  `tool_started` fires the moment a tool is DISPATCHED (`multi_act`),
  `llm_started` the moment an LLM call begins, and
  `awaiting_approval`/`approval_resolved` bracket the approval-provider wait —
  so a long tool, LLM latency, or a blocked approval is visible live instead of
  silent. Tool spans join start→completion via a new `call_id` on both events
  (LLM tool-call id, else a synthesized per-batch id).
- **CLI:** the `→ name(args)` line now prints at dispatch time (paired
  completion prints only the `✓/✗` result line; unpaired completions keep the
  legacy two-line form byte-identically). The status bar's tool segment became
  a live current-activity segment with a ticking clock (`→navigate 43s`,
  `✱ thinking 8s`, `⏸ approval: send_email /pending`); a blocked approval also
  prints a full-width notice (never muted by `/quiet`). `polyrob run`'s live
  activity line shows the in-flight tool.
- **Loud degradation:** when TelemetryManager init fails, the orchestrator's
  no-op fallback now (a) swallows ANY capture method (`__getattr__` — no more
  AttributeError for newer captures), (b) pushes a visible error line through
  the CLI feed callback ("live activity unavailable"), and (c) sets
  `telemetry_degraded`. `polyrob doctor` gained a live-activity pipeline check.
- **Webview:** `/activity` summarize() branches for all four kinds (per-session
  view renders them automatically); a no-dark-kinds contract test pins CLI +
  webview + formatter coverage for every run-event kind.

### 2026-07-18 — Config control plane (proposal 018, P0–P5)

- **Honest `/config` panel:** unconfigured keys show the real built-in default
  (flags-catalog + posture-aware) instead of a wall of `None (default)`;
  advisory keys (`style.*`) are labeled; every enforced pref key is
  ratchet-tested to have a real enforcement-site consumer.
- **4 dead pref keys wired:** `goals.notify_on_done`, `autonomy.self_wake`,
  `autonomy.background_review`, `outbound.max_new_recipients_per_day` now
  actually enforce (tighten-only merges preserved).
- **`digest.quiet_hours` enforced:** proactive sends inside the window are
  durably held and released at window-end (5-min autonomy-runtime ticker);
  interactive replies unaffected.
- **`core/config_service.py`:** one describe/explain/search/set control plane
  over prefs + the ~409-flag catalog; provenance chains
  (`git config --show-origin` style); secrets never readable back; writes
  route to the existing stores only.
- **CLI:** `/config explain KEY`, `/config search QUERY`, full argument
  completion (subcommands/keys/enum values), and bare `/config` opens an
  interactive settings picker (reuses the /model ReplPicker; Enter seeds a
  ready-to-send `set` command, bools pre-toggled).
- **Webview:** `GET/PATCH /api/webgate/config*` — search/explain/set for both
  namespaces; env-flag writes gated to local/own_ops owner postures.
- **Agent self-awareness:** `<environment>` block shows the CLAMPED autonomy
  mode + both axes + the loaded-tool list; `agent_status` gains `mode=`; the
  `preferences` action gains read-only `explain`; `self_env` hard-denies
  `core/config_policy/` source.
- **Hardening:** `core/env.float_env`; import-frozen numeric flags no longer
  crash the process on a stray `none` value; raw numeric-parse ratchet (65,
  shrink-only).

### Structural F-2: god-file split — webview read-services + ratchets (2026-07-17)

- **Changed:** the webview console now sources a new session's default tools from
  the `/api/task/capabilities` `default_tools` payload (the
  `agents/task/tool_defaults.py` SSOT) instead of the hardcoded
  `['browser','filesystem']` in `chat.js`; the static list is kept only as a
  last-resort fallback if the endpoint is unreachable, so session creation never
  breaks (R-6).
- **Changed (refactor, no behavior change):** the four pure feed-reading handlers
  `api_agents` (multi-agent roster), `api_services`, `api_task` and `api_skills`
  moved out of `webview/server.py` into shared read-services in the agents tier
  (`agents/task/telemetry/agent_graph.py::build_session_agents`,
  `feed_reads.py::build_session_{services,task,skills}`) so the console, CLI and
  HTTP API can reuse them; the routes are thin wrappers, logic copied verbatim,
  with 20 new characterization tests. `webview/server.py` 4316 → 3758.
- **Added:** `tests/test_file_size_ratchet.py` — a shrink-only line-count ceiling
  for the five F-2 god-files + `policy.py`; new behaviour must go in a new module,
  not grow these, and a split lowers its row.

### Structural F-3: fresh-eyes final sweep (2026-07-17)

- **Removed:** dead code — `SubAgentManager.get_file_lock`/`get_api_limiter` +
  their class dicts (zero callers anywhere); the deprecated
  `SessionOrchestrator.get_workspace_dir()` async shim (zero production callers;
  the sync `workspace_dir` property is the one accessor); two zero-importer
  re-export shims (`agents/task/flag_defaults.py`, `surfaces/email/seed.py`).
- **Fixed:** the PydanticDeprecatedSince20 warning cluster — core/config.py's 7
  inert `Field(env=)` → `alias=` (loading was already by field-name matching;
  byte-identical), 10 v1 `@validator` → `@field_validator`
  (tools/mcp/config.py, api/mcp_models.py; parity verified), `class Config` →
  `ConfigDict` (modules/llm/adapters.py, api/a2a/agent_card.py), deprecated
  `json_encoders` dropped (api/models.py, modules/memory/models.py).
  `BotConfig()` now constructs with zero deprecation warnings.
- **Changed:** the four `_int_env` re-implementations now delegate to the ONE
  parser `core.env.int_env` (verified behaviorally identical first).
- **Adjudicated (no code change):** the H-MEM "~650 dead LOC" claim retired
  (subsystem is production-wired); the P4 async-initialize refactor is
  won't-fix (AGENTS.md updated); the `agents/task/constants.py` mass shim-flip
  is won't-fix (hybrid module, 78 production importers of task-tier symbols);
  the `core/config_policy/policy.py` 9-submodule split is scoped and deferred
  to its own session; prod sidecar relocation VERIFIED live (`db_relocated`
  2026-07-17 14:19Z) — the legacy read-both fallback is removable one release
  later.

### Structural F-1: rate limiters consolidated onto core/rate_limit.py (2026-07-17)

- **Changed:** the six in-process rate-limiter forks (three algorithms) are now
  configured instances of ONE canonical module, `core/rate_limit.py`:
  `SlidingWindowLimiter` (MCP exec — `tools/mcp/rate_limit.py` is a back-compat
  shim; user MCP admin; the public x402 invoice throttle; the webview
  connection/event throttles; `RateLimitManager`'s internals), `TokenBucket`
  (moved from `core/surfaces/rate_bucket.py`, now a re-export shim; the api
  middleware burst gate), and `FixedWindowCounter` (the api middleware's
  minute/hour windows). Decision semantics are pinned by characterization tests
  written FIRST against the legacy implementations (27 tests across the three
  previously untested forks + RateLimitManager) and pass unchanged after the
  consolidation. `surfaces/telegram/rate_limit.py` stays separate by design (a
  RetryAfter penalty tracker, not a request-budget limiter).
- **Fixed:** the webview per-IP connection tracker no longer grows one key per
  client IP forever — it now shares the same bounded-LRU key space
  (`max_keys=5000`) the per-session event limiter already had (E5/WS-4
  precedent; rate-limit semantics for active keys unchanged).
- **Added:** `tests/test_rate_limiter_ratchet.py` — a shrink-only allowlist scan
  that fails on any NEW limiter-shaped definition outside the canonical module.

### Structural remainder R-2: DB locations are honest (2026-07-17)

- **Fixed:** `DB_PATH` is real. It was a decoy — config anchored it, created its
  parent directory, and `polyrob update` snapshots trusted it, but the app always
  opened the hardcoded `<data_dir>/database/bot.db`. The default now matches
  reality (`data/database/bot.db`) and `database_manager` honors an explicit
  `DB_PATH` behind a refuse-to-guess guard: if the real database still sits at
  the derived location and the configured path doesn't exist, startup raises
  with the exact move instructions instead of silently opening a fresh empty DB.
- **Fixed:** `telemetry_events.db` and the opt-in `messages.db` mirror moved to
  the data-home axis (`core.runtime_paths.sidecar_db_path`, read-both/write-new).
  They previously lived under the SESSION artifact tree (`<data_home>/sessions/`
  on prod-shaped installs) while the backup manifest expected `<data_home>/<name>`
  — so `polyrob update` snapshots silently missed the live files. Snapshots also
  capture the legacy files explicitly until relocation.
- **Added:** a one-shot, clobber-proof boot relocation
  (`core/sidecar_relocate.py`) moves an existing legacy file to the data home on
  the first telemetry touch per process, audited via the new `db_relocated`
  telemetry event kind. Fail-open — any error keeps the read-both fallback.

### Structural remainder R-4: core/security promotion + layering inversions (2026-07-17)

- **Added:** `core/security/` — the tier-0 home for the security primitives:
  `secret_guard` (secret/credential path detection), `untrusted_wrap`
  (prompt-injection DATA framing), and `forged_turns` (forged-turn kind
  constants). The old `agents/task/agent/core/` paths remain as re-export
  shims; tools/controller importers now use the core home. Importing these
  modules pulls zero upper-tier code (pinned by tests).
- **Changed:** `core/surfaces/inbound_webhook.py` no longer imports the surface
  tier — `core/surfaces/act.py` owns `InboundResult` + an actor-registration
  seam; `surfaces/telegram/harness.py` registers the shared dispatch at import.
- **Changed:** `modules/x402/middleware.py` no longer imports `api.auth_state` —
  `api/app.py` installs the auth-state writer at mount
  (`install_auth_state_writer`).
- **Added:** a 5-tier import-boundary ratchet (`tests/test_layering_ratchet.py`)
  seeded with the 126 existing upward edges, shrink-only; the core→agents
  allowlist tightened 35→34.

### Structural remainder R-1: one canonical .env precedence (2026-07-17)

- **Added:** `core.paths.env_file_candidates()` — the single source of truth for
  which `.env` files configure the process and in which precedence order.
  `load_env`, `/config check`, the CLI first-run guard, and the `polyrob update`
  snapshot all derive from it now (layering behavior unchanged).
- **Changed:** `polyrob update` snapshots additionally capture the legacy
  `~/.rob/.env` transition fallback (it can still hold live keys via the
  read-only fallback layer), so a rollback restores the whole user env state.

### Money ledger: two statements, never summed (2026-07-16)

The daily digest and the `accounting`/`/status` views used to merge the owner's LLM/API
bill into the agent's own wallet spend before computing one "net" figure — on
2026-07-16 this told the owner "earned $0.00, spent $2.47, net $-2.47" while the
agent's wallet sat untouched at $10 USDC (the $2.47 was 100% API cost, none of it the
agent's own spend). The ledger now shows two statements that are never added together:

- **Treasury** — the agent's own money (USDC): income, spend, pending invoices, and
  `net = income − spend`. Runtime/API cost never enters this figure.
- **Runtime cost** — the owner's money (compute): window + lifetime spend and call
  counts. It has no "net" — there is nothing to net compute cost against.

"Earned" is retired in favor of income/spend, and the old merged fields are gone with
no fallback — a caller still reading the merged figure will error instead of silently
showing a wrong number again. A balance is only ever shown when the provider actually
exposes one; unknown now renders as omitted, never a misleading `$0.00`.

- **Removed:** the autonomy budget gate — `AUTONOMY_BUDGET_USD`,
  `AUTONOMY_BUDGET_WINDOW_DAYS`, `BUDGET_AWARE_AUTONOMY`, and the
  `budget.autonomy_daily_usd` preference/onboarding prompt. It was a $10/day *rate*
  ceiling that can't protect a finite balance — the agent was under budget every single
  day while the provider balance ran to zero — and it gated on the merged figure above,
  so an x402 wallet payment could eat into the agent's compute budget. Every
  wallet-spend cap (`WALLET_DAILY_CAP_USD`, venue caps, payment approval mode,
  correspondent-taint, x402 invoice caps) is untouched. Nothing now throttles burn rate
  on its own — the provider's 402 and the credit sentinel are the backstop.
- **Fixed:** the credit-death sentinel could only trip from a cron or goal run, so an
  interactive chat that hit a real provider 402 — the one place the owner would
  actually notice — never latched it. There is now one universal trip site reached
  from every run path, interactive included.
- **Fixed:** a failed Telegram-bound run used to go silent — the "already delivered
  live" skip assumed the run had succeeded. A failed run now always tells the owner.

### Structural-wave verification & completion pass (2026-07-16)

Cleanup-and-completion over the WS-1..WS-7 wave; zero behavior change except
where marked.

- **refactor(core):** the data-home resolver trio is ONE rule — verified
  `POLYROB_PROJECT_DIR` never changed the data-home value (workspace placement only), so
  `core.bootstrap._resolve_cli_data_home` and `core.runtime_config.get_data_root` now
  delegate to `core.runtime_paths.resolve_data_home` (three-way parity test added).
- **refactor(core):** `core/tool_catalog.py`'s second hand-classification folded into the
  WS-2 capability module — `TOOL_PERMISSIONS` lives beside the capability rows and the
  catalog risk tiers are DERIVED (high = external-write permission, medium = high_impact
  without one), memberships parity-pinned. `VALID_TOOL_IDS` (skill_manager) is now derived
  from the capability table (verified set-equal first); the T12 vocabulary test, made
  tautological by that derivation, now checks gate ids against the independent
  registry-side vocabulary.
- **refactor(config):** WS-1 phases 3–4 landed — the 32 core-adjacent consumer files
  (tools/, cron/, modules/) import from `core.config_policy` directly (only the five
  shim-tail-symbol imports remain on `agents/task/constants`); the `flag_defaults` bridge
  moved to `core/config_policy/flag_defaults.py` (old path re-exports); all 15 core lazy
  config_policy imports promoted to top level with their fail-open/fail-closed guards kept
  on the calls; 12 never-referenced underscore re-exports + a dead `import logging`
  trimmed from the shim; stale pre-wave comments corrected across core/tools/modules.
- **fix(paths):** cli/commands' remaining 22 `or "data"` fallbacks (a latent CWD write
  when no container/config is present) now route through `data_dir_or_home()`; the path
  ratchet's single-quote blind spot closed (5 hidden `action_registration.py` sites fixed,
  patterns extended); 13 ratchet baseline rows deleted, `handlers.py` 7→2.
- **docs(flags):** `DELEGATE_BLOCKED_TOOLS` catalog row fixed (11 → 15 ids; derivation +
  live anchors noted); flags catalog + user-guide refs regenerated. AGENTS.md now
  describes the derived gate sets and `core/tool_capabilities.py`.
- **fix(tests):** the 4 order-dependent failures are gone — TWO root causes: (1) tests
  building a CLI container imported the dev box operator's REAL env files
  (`~/.polyrob/.env` keys, legacy `~/.rob/.env` provider pins, `config/.env.production`
  backfill) into `os.environ`; the suite now disables the backfill session-wide and a
  narrow per-test guard restores the operator-var set (provider pins, owner binding, API
  keys, and the frozen-security flags `polyrob init` applies in-process) — deliberately
  NOT a home redirect, which shadowed tests that isolate via `Path.home`.
  (2) `test_ledger_error_fails_closed` asserted outside its broken-ledger patch and
  depended on a fresh-process ledger failure; the assertion moved inside the patch.

### Revalidation fixes — pre-existing main failures (2026-07-16)

Found by the full-suite revalidation pass after the WS wave; each verified pre-existing
at 370843bd before fixing.

- **fix(llm):** `modules/llm/adapters.py` imported the four provider client modules at top
  level, so EVERY entry-point import (each `polyrob` CLI invocation, every uvicorn worker
  boot) eagerly loaded the anthropic + openai + google.generativeai SDKs. Now
  TYPE_CHECKING-only (the classes were annotation-only there); `cli.polyrob` imports zero
  heavy SDKs and `test_import_layering` is green.
- **fix(docs):** recovered 10 lost plan/review docs from git history (referenced by
  committed docs but only ever present in the shared working tree); the three never
  committed anywhere are grandfathered with an audit trail. `test_doc_consistency` green.
- **fix(tests):** the nginx deploy guards read the retired `deploy_unified.sh` tombstone;
  they now encode the same invariants against the live deploy surface
  (`deploy_webview.sh` installs the ownops vhost; no live script installs the demoted
  proxy `nginx.conf`; `/opt/polyrob` anchor).
- **Known-not-fixed (documented):** 4–5 order-dependent test failures (`test_identity`,
  `test_budget_gate`, `test_goal_dispatcher` child-tools, `chat_resolver_parity`,
  `protected_config_guard`) — all pass in isolation; root cause is `load_env` importing
  the dev box's real `~/.polyrob/.env` into `os.environ` mid-suite. Needs a suite-wide
  env-sandbox fixture (own change, own blast radius).

### Structural upgrade WS-2..WS-7 slices (2026-07-16)

Same-day continuation of the WS-1 wave; every item ratchet- or parity-tested.

- **WS-2 (tool capabilities):** ONE per-tool capability table (`core/tool_capabilities.py`;
  orthogonal dimensions `money`/`high_impact`/`delegate_blocked`/`exec`/
  `readable_while_tainted`). `MONEY_TOOLS`, `DELEGATE_BLOCKED_TOOLS` (env override kept) and
  `HIGH_IMPACT_TOOL_IDS` are now derivations, byte-identical memberships parity-pinned;
  `register_optional_tool` refuses an unclassified tool, so a new tool can never silently
  skip every gate. Verb-level sets stay hand-curated at their gates (T12 keeps them in sync).
- **WS-3 (paths):** `core/runtime_paths.py` gains `data_dir_or_home()` /
  `goals_db_path()` / `cron_db_path()`; ~25 sites that fell back to a relative `"data"`
  (a latent CWD/install-tree write) now resolve the data home, incl. `skill_usage`'s
  repo-root anchor, both browser screenshot fallbacks (+ session-id cleaning, also in the
  trace filename), and 9 operator scripts' hardcoded `data/*.db` / `/var/lib/polyrob/*`
  argparse defaults. New ratchet `tests/test_path_ratchet.py` freezes the remaining
  constructions per-file, shrink-only. Deferred with notes: `bot.db`/`messages.db`/
  `telemetry_events.db` location moves (need a data migration) and the `.env`-candidates
  helper. Tenant-dir conventions documented (`core/instance.py::self_tier_root`) — two
  deliberate grammars, one per path axis, not to be unified on disk.
- **WS-4 (rate limiting):** two real leaks fixed — `api.middleware.RateLimiter`'s
  `_cleanup_old_buckets` was never called (per-user dict grew for the process lifetime on a
  network-facing surface; now amortized-swept), and the canonical
  `core/surfaces/rate_bucket.TokenBucket` now prunes fully-refilled idle keys
  (exact-semantics eviction). Full 6-fork consolidation deferred: it changes throttling
  shape (token bucket vs sliding window) and three forks have no characterization tests.
- **WS-5 (layering edges):** `cli/gitignore.py` → `core/gitignore.py` (shim kept), killing
  core→cli; `core/initialization.py`'s dead top-level `agents.personality` imports deleted
  (layering-ratchet allowlist tightened 37→35).
- **WS-7 (SSOT tail):** `api/openai_compat/model_map.py` resolves a bare registered model
  slug via registry membership (grok/glm/kimi no longer misroute to the env default);
  `scripts/seed_goal.py` and the telegram owner-interactive toolset now source from named
  `TOOLSETS` entries (`earn`, `owner_interactive`).

### WS-1 — config-layer relocation: core↔agents.task cycle broken (2026-07-16)

Deep structural wave following T1–T12.

- **refactor(core):** relocated the cross-cutting autonomy/mode/posture/payment-policy cluster +
  `AutonomyConfig` (≈1230 lines) from `agents/task/constants.py` into the new core-tier package
  `core/config_policy/` (`policy.py`). `agents/task/constants.py` re-exports every public and
  externally-referenced private symbol unchanged, so all ~126 importers are byte-compatible; new
  code should import from `core.config_policy`. Added a `reset_autonomy_mode_warnings()` test seam.
- **refactor(core):** flipped all 15 `core/ → agents.task.constants` back-edges to
  `core.config_policy`, so `import core.config_policy` pulls zero `agents.*` modules — the
  `core ↔ agents.task.constants` cycle is one-directional (`agents → core`) at last.
- **test(core):** added `tests/test_layering_ratchet.py` — bans `core/` imports of
  `agents.task.constants` and enforces that the remaining `core→agents.*` edges (WS-1 phases 3–4 +
  WS-5 targets) may only shrink.

### Structural cleanup wave T1–T12 (2026-07-16)

Twelve fixes from the 2026-07-16 four-way structural audit (duplication / path handling /
layering / sources-of-truth).

- **T1 (data-loss fix):** 10 sidecar DBs (`slack/signal/discord/x` dedup, `wa_window`,
  `group_allowlist`, `conversations`, `outbox`, `surface_state`, `deployed_apps`) registered in
  `core/db_manifest.py` — `polyrob update` backup/rollback silently skipped them. Grep-based
  completeness contract test added.
- **T2:** `/capabilities` no longer advertises the deprecated `x-ai/grok-4.1-fast` — default
  model now comes from `llm_client_registry.get_default_model` (env-overridable).
- **T3:** `credit_sentinel`'s fallback path resolution follows `resolve_data_home` (dropped its
  unique `DATA_ROOT` precedence — the spend/halt gate could latch in the wrong tree).
- **T4:** the fail-CLOSED identity-scan write gate is ONE base-class implementation for all
  three identity-doc writers (self/contract/owner) — was copy-pasted ×3 (security-drift hazard).
- **T5:** `VALID_TOOL_IDS` covers all registrable tools (`shell`, `process`, `self_env`,
  `hf_deploy`, `github`, `x402_pay`, `alchemy`, `collabland` were rejected as invalid);
  registry-parity contract test added.
- **T6:** one canonical telegram recipient resolver
  (`user_delivery.resolve_telegram_recipient`); cron delivery delegates and its no-sink case now
  leaves a durable `owner_notice` instead of a silent drop.
- **T7:** deleted dead `modules/database/connection_pool.py` (zero importers, divergent PRAGMAs).
- **T8:** `core/activity_evidence.py` — one ledger/episodes evidence layer shared by the owner
  digest and `polyrob recap` (numbers can no longer diverge).
- **T9:** `core/event_kinds.py` — SSOT for all 33 durable event-log `kind` strings + a producer
  contract test; activity feed / spend rollup / digest consume the constants.
- **T10:** a bare `PathManager()` routes through `resolve_session_data_root` (closes the RC-1
  "two session trees" landmine — `DATA_ROOT`-only default).
- **T11:** runtime logs resolve to `<data_home>/logs` (new `POLYROB_LOG_DIR` override) instead
  of the install tree; packaged/read-only installs can log.
- **T12:** cross-consistency contract tests for the six dangerous-tool gate sets (money ⊆
  delegate-blocked, correspondent-gate coverage incl. namespaced trade verbs, gate ids ⊆ tool
  vocabulary, verb-substring sync).

### AUTONOMY_MODE — single-owner capable-by-default master switch (2026-07-16)

Proposal 013 (owner directive 2026-07-15): the recurring "session has no web_fetch/twitter",
"planner: REAL BLOCKER tool unavailable", and "can't approve emails to addresses we don't know"
stalls were all one disease — the framework is deny-by-default and treats missing-permission as
a hard wall. One master switch, `AUTONOMY_MODE=supervised|autonomous` (never "yolo"/"unleashed"
in code, flags, or docs), makes a genuinely single-owner instance capable-by-default without
touching money-spend, host access, or secrets. `supervised` (default/unset) is byte-identical to
pre-013 behavior; `autonomous` is only effective on a single-owner deployment (`POLYROB_LOCAL` +
a bound owner principal via `POLYROB_OWNER_USER_ID`/`_TELEGRAM_ID`/`_EMAIL`) — otherwise it
clamps back to `supervised` with a one-time WARN, so a multi-tenant server can never drift into
it.

- **Capability-flag groups default ON** under effective autonomous mode
  (`_mode_capability_default`): `TWITTER_ENABLED`, `MCP_ENABLED`, `GROUP_CHAT_ENABLED`,
  `EMAIL_SURFACE_ENABLED`, `X402_INVOICE_ENABLED` (receive-side only),
  `MESSAGE_AUTONOMOUS_ALLOWLISTED`, `CORRESPONDENT_ACCESS_ENABLED`,
  `CORRESPONDENT_REPLY_ENABLED` — wired at every consumer seam (`core/config.py`'s MCP gate,
  `modules/eip8004/registration.py`, `core/surfaces/access.py`'s group gate,
  `modules/x402/invoicing.py` + `core/autonomy_runtime.py` for the invoice tool/settlement
  watcher pair), plus `CORRESPONDENT_REQUIRE_APPROVAL` inverted (defaults OFF under autonomous).
  An explicit per-flag env always wins.
- **Autonomous toolset** — `AUTONOMOUS_MODE_TOOLS` (never money-spend/host) is granted to a bare
  session, the goal dispatcher's default toolset, the planner's session-tools (`+web_fetch`), and
  the Telegram interactive toolset, all gated on `full_autonomy_enabled()`; `VALID_TOOL_IDS`
  gained the vocabulary needed to express the grant.
- **Two-lane approvals** — a new `auto_notify` provider (allow + `tool_auto_approved` audit event
  + post-hoc owner notification — "act-and-report") becomes the default under autonomous mode for
  an unset/`auto`/`interactive_cli` `APPROVAL_PROVIDER`. A fixed always-owner-queued lane
  (`_ALWAYS_GATED_VERBS`: the four `self_env_*` verbs, `mcp_install`, plus the aspirational
  `self_modify`/`tool_manage`) never moves to `auto_notify` regardless of mode. `hf_deploy`'s
  first-publish maps `auto_notify → owner_queue` (a public HF Space is not something to
  act-and-report after the fact).
- **Outbound policy ladder** — `OUTBOUND_POLICY` (`open|domains|allowlist|off`, default
  `allowlist`, `open` under autonomous mode) replaces the per-address ACL with a policy+cap model
  (`resolve_outbound_policy`, fail-closed), enforced at the send gates (cap → seed → send →
  record → report), `OUTBOUND_DAILY_SEND_CAP` (default 30) as the first live reader of
  `outbound_count_surface_since`, and a first-contact report that fires only for open-tier sends.
  **Deviation from the original plan:** `outbound.domains` merges via a new `narrow_list` kind
  (the pref can only *intersect* a non-empty operator `OUTBOUND_DOMAINS` env, or define the set
  from scratch when the env is empty) — the plan's specified `union` merge would have let a
  tenant pref *widen* past an operator-set domain allowlist, inverting its polarity;
  `narrow_list` is the corrected, allowlist-safe behavior.
- **Receive-side auto payments, spend stays gated** — `PAYMENT_APPROVAL_MODE` defaults to `auto`
  under autonomous mode, but **only** for `PAYMENT_RECEIVE_APPROVAL_TOOLS = ("x402_request",)`.
  **Deliberate hard line:** the four live-trade spend verbs
  (`hyperliquid_place_limit_order`/`_market_order`, `polymarket_place_limit_order`/
  `_market_order`) keep `owner_queue` pre-approval under **both** modes, including an *explicit*
  `auto` — trading is never act-and-report, closing a gap the initial cut left open (013 T7
  review).
- **Tool-availability transparency** — `TOOL_AVAILABILITY_HINT` (default ON) injects a
  `<tool-availability>` prompt block (`GATED_TOOL_REGISTRY`) disclosing every
  known-but-not-loaded tool with its gate + remedy, so a missing capability is always named
  instead of guessed at or used as an excuse; the goal planner's "TOOL GROUND TRUTH" block reuses
  the same registry.
- **Artifact-existence stamping** — goal/planner prompts now stamp titles/bodies/acceptance
  criteria/past-failure text with `[present, N bytes]`/`[MISSING on disk]` against what's
  actually on the workspace (containment-safe, symlink/traversal-safe boundary lookbehind), plus
  a planner escalate-once instruction after repeated identical blockers.
- **`/config` + visibility** — a Telegram `/config` command (guarded-set → owner-approval
  proposal), `autonomy_mode_display()` surfaced in `/status`, `polyrob owner show`, and
  `polyrob doctor`, matching pending-review parity in the webview, and an autonomy/prefs section
  in `polyrob config show`.

Money-spend, host access (`AGENT_COMPUTE_POSTURE`), and secrets are untouched by this mode in
either direction — see `docs/CONFIGURATION.md`'s `AUTONOMY_MODE` section for the full flag
table. Rollout to prod (T12) is owner-gated and not part of this wave.

### Capability completion — exec everywhere it should be, agent knows where it lives (2026-07-16)

Proposal 014 (from an incident investigation): closes the
session-entry toolset gaps 013 left, makes the posture≥1 dev sandbox Node-capable, and
gives the agent an in-context answer to "where do I live". Everything is gated — a
deployment with nothing set is byte-identical.

- **`default_session_tools()` SSOT** (`agents/task/tool_defaults.py`) — the three drifting
  `['browser','filesystem','task']` literals in `task_agent_lite.py` now route through one
  helper; under effective `AUTONOMY_MODE=autonomous` a bare session gets the ambient
  autonomous grant (never money-spend/compute — those are structurally absent).
- **Telegram interactive toolset is mode- and posture-aware**
  (`surfaces/telegram/interactive_tools.py`) — the owner chat under autonomous mode gets
  the full `AUTONOMOUS_MODE_TOOLS` grant (keeping `goal`/`cronjob`), plus
  `code_execution`/`shell`/`coding` at `AGENT_COMPUTE_POSTURE>=1` via the new
  `with_compute_tools()` SSOT (the goal dispatcher now shares it). `INTERACTIVE_TOOL_IDS`
  still always wins; supervised default unchanged.
- **`CODE_EXEC_DEV_IMAGE`** (default `nikolaik/python-nodejs:python3.11-nodejs20`) — the
  posture≥1 persistent dev container defaults to a python+node image so npm/npx toolchains
  work; the confined ephemeral sandbox keeps `python:3.12-slim`; explicit
  `CODE_EXEC_DOCKER_IMAGE` wins everywhere.
- **`run_code(packages=)` honors the effective sandbox network** — the gate now probes
  `DockerBackend.effective_setup_network()` instead of the raw env, so a dev container
  that auto-bridged (env unset) is no longer wrongly refused pip installs; explicit
  `CODE_EXEC_NETWORK=none` still refuses.
- **Dev-mode exec timeout ceiling is 120s** (was silently 30s) — aligns the backend clamp
  with the shell tool's foreground contract; explicit `CODE_EXEC_MAX_TIMEOUT_SEC` wins;
  confined default stays 30s.
- **`<environment>` foundation block** (`agents/task/agent/core/env_context.py`, flag
  `ENV_CONTEXT_BLOCK` default ON) — instance, platform, data dir, absolute workspace path
  with explicit persistence semantics, posture/mode axes, and a host-executable probe,
  pinned after runtime identity. Emits only under `POLYROB_LOCAL` or effective
  `AUTONOMY_MODE=autonomous`; multi-tenant server sessions unchanged.

### Wallet / crypto security hardening wave (2026-07-15)

A 7-way security + UX review of the wallet/x402/trading stack (2026-07-15)
followed by a same-day fix wave: 2 Critical and all
14 High findings closed, plus most Medium/Low.

- **C1 — pay-side fund-drain closed:** the x402 payment gate now authorizes at the
  tool-call `max_amount_usd` (not the advisory quote) AND re-checks `PolicyGate`
  against the REAL challenge amount before signing; the reserve is held across the
  whole check→pay→record span.
- **C2 — wallet CLI reads the right env:** `polyrob wallet`/`set-cap` load the local
  env before reading the wallet (no more acting on a phantom empty wallet).
- **Owner kill-switch exists (H5/H6):** `polyrob owner halt`/`resume` — a structural
  halt enforced inside `PolicyGate.check`, invoice minting, renewals, and live-trade
  gates; the halt probe fails CLOSED.
- **Turn-origin money gates (H10/H11):** live orders and NAMESPACED crypto trade
  verbs are blocked from forged/autonomous/correspondent-tainted turns; every
  mutating trade verb is origin- and halt-gated.
- **Custody hardening (H1–H3):** the wallet derivation scheme is pinned alongside the
  seed (a legacy wallet can never be silently re-derived), `resolve_scheme` fails
  fast on corrupt meta, wallet policy files are credential-guarded, and the audit
  sink is tamper-evident.
- **Approval integrity (H4):** one approval = exactly one execution; a forged
  approval probe fails closed.
- **Settlement/invoicing hardening (H7–H9 + M-class):** shared-DB and
  settlement-watcher races closed; snapshots now capture the wallet dir and deny
  renamed env copies (M1/M2); unpriceable/non-finite order values fail closed (M10).
- **Owner-facing money truth (H12–H14):** the agent's money self-knowledge corrected,
  `polyrob finance` works standalone, wallet view/export fail friendly on a bad
  seed, and `polyrob doctor` verifies the wallet actually works before reporting
  "on".

### Onboarding finalization — wallet, avatar, identity (2026-07-14)

Closes out the onboarding-finalization wave: the agent wallet is now a one-command,
portable, exportable thing instead of a bare env var, and the avatar/setup surfaces
catch up to it.

- **`polyrob wallet init`** — generates a fresh 24-word BIP-39 mnemonic (shown once) or
  imports one (`--from-mnemonic`) or a legacy raw seed (`--from-seed`); writes
  `AGENT_WALLET_ENABLED`/`AGENT_WALLET_MASTER_SEED` to `~/.polyrob/.env` (chmod 600) and
  offers to point `X402_PAYMENT_RECIPIENT` at the new treasury address so earnings settle
  somewhere spendable. Testnet prints faucet guidance; mainnet prints USDC-on-Base
  guidance.
- **`polyrob wallet export [--venue]`** — TTY-only, typed-`EXPORT` confirmation reveal of
  the mnemonic (bip44) or per-venue `0x`-hex private keys; never agent-callable.
- **Versioned key derivation** — a wallet's scheme (`legacy` PBKDF2 or `bip44` BIP-44,
  `m/44'/60'/0'/0/{treasury,x402,polymarket,hyperliquid}`) is recorded write-once in
  `data/wallet/meta.json` by `wallet init`/import; a pre-existing wallet with no meta file
  is legacy FOREVER — addresses never change. New wallets get `bip44` (mnemonic imports
  cleanly into MetaMask/Rabby). `AGENT_WALLET_DERIVATION` is a recovery-hatch env override
  for a corrupted/missing meta file only.
- **`/pfp` REPL command** (alias `/avatar`; `status|generate [force]|show`) — the avatar
  stays fully optional (nothing auto-generates it); this makes generating/inspecting it
  discoverable without leaving the chat REPL.
- **`polyrob init` bridges from the inline key wizard** — after the first-run key prompt
  saves a usable key, it now offers "Finish full setup now (model, persona, autonomy —
  ~1 min)?" and runs `init --skip-keys` on accept instead of leaving the operator with a
  bare key and nothing else configured; `init` also gained an optional agent-wallet
  opt-in step (default No) and a "Next steps" block (wallet / avatar / surfaces / identity
  / doctor).
- **`polyrob doctor` setup-completeness lines** — wallet/avatar/surfaces/SOUL status,
  gateway-gate-accurate (a flag-on-but-uncredentialed surface reads as configured-but-
  incomplete, not silently "off").
- **`ui.show_avatar` preference** — a per-owner toggle for whether the webview identity
  page renders the avatar.
- **`/model set-default` SSOT fix (G11)** — now keeps `DEFAULT_PROVIDER`/`DEFAULT_MODEL`
  env pins in lockstep with the CLI preference store, instead of drifting apart.
- **`polyrob soul init` (O10)** — scaffolds the operator-authored SOUL identity docs
  (`identity/identity.md` + `identity/operating.md`) so authoring the richer identity
  layer has a discoverable onboarding path instead of requiring hand-authored files.
- **`polyrob-user-guide` skill v2** — adds `references/wallet-and-identity.md` and
  regenerates `references/configuration.md` from the current `docs/CONFIGURATION.md`
  (also absorbs the `AGENT_WALLET_DERIVATION` row and `MESSAGE_AUTONOMOUS_ALLOWLISTED`
  from a concurrent change).
- **Docs** — `docs/CONFIGURATION.md` gains the `AGENT_WALLET_DERIVATION` row;
  `docs/guide/payments.md` gains "Create the wallet in one command",
  "Portability, backup & export" (with the snapshot-contains-seed caveat), and
  "Migrating to a new install" sections; `docs/guide/getting-started.md` documents the
  inline first-run key wizard + its full-setup bridge, fixes the stale ASCII-box
  "Example Session" banner to the real two-line banner, and completes the config-layers
  table (`config/.env.*`, legacy `~/.rob/.env`).

### Update/infra/onboarding hardening — Wave 3 (2026-07-14)

Completes Wave 3 of the 2026-07-14 review (all remaining P2s except the owner-action
secret rotation).

- **`polyrob gateway` launches every surface (H2)** — Discord/Slack/Signal/X now start
  under the gateway when their flags are on (previously silently ignored); an enabled
  surface with missing credentials is WARNED about and skipped. `SurfaceConfig` gains
  the four flag helpers; stale gateway caveats removed from the migration guide.
- **Inline-schema == migration-HEAD contract (U4)** — a new CI test builds a fresh DB
  through the real component creators, stamps at HEAD via the real boot path, and
  requires every shipped migration's `verify()` to pass. It immediately caught a real
  drift: `billing_failures` (v1_3_0) had no inline creator — fresh installs never
  created it and billing-failure records silently failed to insert (now mirrored into
  `AuthTables`). The dead legacy schema initializers in `connection.py` (home of the
  singular `schema_version` table) and the orphan `scripts/migrate_*` one-offs are
  deleted (U11).
- **Doctor env checks (U10/O6)** — Python ≥3.11 floor, `[server]`-extra presence,
  Playwright chromium probe, and a DB-schema-vs-code line (also printed by
  `polyrob update`).
- **`backup_database.sh` (U7)** — now a WAL-safe all-DB snapshot via the update
  engine's Online-Backup path (was: `cp` of a live WAL DB at a path that no longer
  exists), restorable via `polyrob update --rollback`.
- **In-use guard portable (U8)** — the process scan is `/proc` → psutil → `ps`, so
  macOS `--apply`/`--rollback` no longer bypasses the guard silently.
- **Prod venv rebuild procedure (D12)** — DEPLOYMENT.md documents the
  requirements.txt-first rebuild (bare `pip install -e .` loses the extras-only
  tweepy/eth-account that run on prod); a guard test pins both in requirements.txt.
- **Init polish (O2/O3/O4)** — consistent 1/6..6/6 wizard numbering; the closing
  "no usable key" check reads every env layer `polyrob run` honors; DeepSeek's key
  prompt says it can't bootstrap alone.
- **Pairing approvable (O5)** — `polyrob owner pair {pending,approve,revoke}` ships;
  `core/pairing.py` no longer documents a phantom command.
- **Portal workflow self-gating (D11)** — skips deploy (green) when Cloudflare
  secrets are unconfigured, for whenever Actions is re-enabled.

### Update / infra / migration-guide / onboarding fix wave (2026-07-14)

Implements Waves 1–2 (+ selected Wave-3 hardening) of a 2026-07-14 internal
review — the connective
tissue around the good engines: the migration runner, deploy paths, updater safety net,
first-run toolset, and the flagship docs.

- **Migration runner survives self-recording migrations (U1, P0)** — `migrations.migrate
  upgrade` no longer double-records the schema version (`IntegrityError` → exit 1),
  which deterministically rolled back any `polyrob update --apply` containing a
  self-recording migration. The upgrade loop is extracted to a testable
  `apply_pending_migrations()` with recording guarded by `is_version_applied`.
- **`polyrob init` no longer degrades the first run (O1, P0)** — `resolve_toolset
  ("default")` now resolves to the true dynamic default (web_fetch + coding/anysite
  additions), identical to an unset `POLYROB_AGENT_TOOLSET`; accepting the wizard
  default used to silently drop `web_fetch`, breaking the documented first task.
- **Deploy paths migrate the DB and the docs tell the truth (D1/D2/D3, P0/P1)** —
  `deploy_unified.sh` (destructive, dead api+webgate shape) is retired to a hard-exit
  tombstone (legacy body preserved at `deployment/legacy/`); `scripts/deploy_prod.sh` /
  `deploy_from_local.sh` now run `migrations.migrate upgrade` before restart;
  `start_autonomy` schedules `run_boot_migrations` so every posture (telegram/REPL/
  email/gateway) self-heals schema like the API lifespan; `polyrob-email.service` is
  committed and restarted by both deploy scripts. AGENTS.md/DEPLOYMENT.md/
  `deployment/README.md` rewritten around the real headless shape (guard tests keep
  them honest).
- **Updater rollback restores what it promises (U2/U9, P1)** — snapshots carry a
  `scope` (`full`|`db_only`); bare `--rollback` prefers the newest FULL snapshot
  instead of the DB-only pre-migrate one; `--apply` takes ONE snapshot (migrate_guarded
  reuses it); same-second snapshot dirs no longer clobber. Systemd manual steps are
  posture-aware (detect `polyrob*` units + `daemon-reload`; no more phantom
  `polyrob-api`), and the stale "automated apply not wired yet" messaging now
  advertises `--apply` (U3/U6).
- **Migration guide un-staled (H1/H2, P1)** — Discord/Slack/Signal/X marked
  shipped (with honest validation status), compute-posture ladder replaces "not
  supported", learning-loop claim fixed, and every `polyrob gateway` mention carries
  the "doesn't launch Discord/Slack/Signal/X yet" caveat. `tests/test_doc_consistency.py`
  guards the platform claims against re-diverging.
- **Provenance process fix (H4, P1)** — the lost cross-agent-parity design record
  was reconstructed; the 7 still-recoverable referenced
  plan docs are committed; a contract test now requires any internal plan/review doc
  referenced from a committed doc to be committed itself (21 already-lost files
  grandfathered by name).
- **Onboarding docs (O8/O9/O10, P1/P2)** — getting-started.md gains an "Updating"
  section; instances.md corrects the SOUL doc location (flat `identity/*.md`, NOT the
  nested per-user dir — files placed there never loaded) and adds a SOUL authoring
  guide.
- **Hardening (Wave-3 picks)** — trajectory capture runs off-loop
  (`asyncio.to_thread`); bulk datagen export reaches legacy `data/auto/*/sessions/*`
  sessions; `deploy_webview.sh` takes its target from `~/.polyrob/ops.env` instead of
  a hardcoded host; stale ops-script headers corrected (D7/D9/D10/H5/H6).

### Built-in ecommerce / payments finalization (2026-07-14)

The four separate money organs (x402 receive middleware, agent pay-side wallet, agent
invoicing, platform credits) are finalized into one coherent, owner-legible built-in
ecommerce capability: the agent can quote, invoice (text + branded QR image), get paid
(USDC, auto-detected), meter, deliver, and account for itself. Landed as 17 reviewed
tasks; **all new behavior is behind default-OFF flags** (a deployment that enables none
is byte-identical to before). Full reference: `docs/guide/payments.md`.

- **Truth & safety (P0).** Metering now persists on a headless single-owner deploy —
  an owner `user_profiles` row is seeded at startup (`ensure_owner_profile`), closing the
  FK failure that made spend read a false `$0`. The autonomy budget gate and cron ticks
  fail *closed* (a ledger error or `autonomy_halted()` holds dispatch, not runs). Pay-side
  hardening: the wallet PolicyGate runs unconditionally, payment asset is **pinned to the
  canonical USDC** for the configured network (defeats a decimals-spoof cap inflation),
  network binding is fail-closed (V1 names + CAIP-2), `success=false` settlements are
  treated as unpaid, and the kill-switch probe fails closed. Billing correctness: one
  cost entry point (cache-write surcharge preserved), a real `usage_records.request_id`
  column keyed on the provider response id for reachable retry dedup.
- **Invoicing as a product.** Branded QR invoice **cards** (`modules/pfp/cards.py`, pure
  Pillow + `qrcode` + a shipped OFL font; `INVOICE_CARD_ENABLED`, `INVOICE_QR_STYLE`);
  an outbound **media leg** (Telegram photos, email attachments, `message(media_paths)`,
  workspace-confined); free-form **`payer_contact`** ("billed to"); **approval modes**
  (`PAYMENT_APPROVAL_MODE` = `approve` via a durable, remotely-approvable `owner_queue`
  provider with Telegram `tap-` verbs | `auto` within-caps); non-payment **expiry
  escalation**.
- **Facilitator-free settlement.** On-chain USDC **settlement detection**
  (`X402_SETTLE_ONCHAIN_DETECT`): the watcher scans treasury transfers, matches by exact
  atomic amount oldest-first, and settles — with a `transaction_hash` partial-unique
  index + CAS against double-settle, amount-jitter for same-amount disambiguation, and a
  `payment_unmatched` owner notice. A payment-aware cron wake-gate leg.
- **Watchtower subscriptions** (`SUBSCRIPTIONS_ENABLED`, `WATCHTOWER_PRICE_USD` = $10/mo):
  prepaid periods + renewal invoices on the settlement-watcher tick (atomic idempotent
  `apply_settlement`, cron `subscription_lapsed` gate, `polyrob owner sub list/cancel`).
- **Metering→invoice bridge** (`USAGE_INVOICE_BRIDGE_ENABLED`): a tenant-scoped
  `usage_rollup` + `usage_summary` action drafts an invoice from measured cost (never
  auto-sends).
- **ERC-8004 payment-backed reputation** (`EIP8004_PAYMENT_FEEDBACK`): a settled invoice
  offers the payer a `ProofOfPayment`-backed verified-purchase feedback authorization
  (settled + treasury-toAddress + txHash replay guard + agent-id binding; local
  simulation, not on-chain yet).
- **Machine-payer middleware fixes:** exact `(method,path)` route gating (free reads no
  longer paywalled), a shared 402 challenge, `has_other_auth`-gated 503 on a missing
  facilitator, and an **un-spoofable** rate limit on the public invoice endpoints
  (`get_trusted_client_ip` trusted-proxy resolution; `X402_PUBLIC_RATE_*`). Removed the
  dead `x402_access_log` table and the discontinued Google-Charts QR URL.

### CLI candy polish wave (2026-07-14)

A pure visual/UX polish of the `rob` terminal REPL — the current implementation,
made its best self (no renderer changes, no new rendering abstractions):

- Slash commands highlight live while typing (known command / prefix / unknown /
  args each styled distinctly); the completion menu is now actually visible — a
  stock prompt_toolkit palette with per-command descriptions, opening while a
  `/command` is typed and on Tab.
- The hint line under the input is context-aware: mid-turn it shows `^C stop`,
  typing a known `/command` shows that command's usage, idle shows the key hints
  plus one gentle rotating tip (and it stays quiet while the `/model` picker is open).
- Every functional view (`/goals`, `/subagents`, `/todos`, `/pending`,
  `/autonomy`, `/status`, `/usage`, `/tools`, `/toolset`, `/persona`, `/sessions`,
  `/history`, `/session`, `/telemetry`, `/finance`, `/journey`, `/skills`,
  `/cron`, `/config`, `/kb`, `/mcp`, `/learn`, `/self`, `/approve`, `/context`)
  now shares ONE visual grammar: a 2-space gutter, one table style, aligned
  label/value rows, one empty-state phrasing with actionable hints, and one
  status-glyph vocabulary (`●`/`✓`/`✗`/`○`/`⚠` from the theme) — the per-view
  emoji vocab (🟢🟡✅🔴⬜⚪⏱️) is retired.
- Sub-agent steps render on a quiet tree-prefixed lane (`  └ researcher · step 3`).
- Status bar: ctx% turns yellow at 80% and red at 90%; the in-flight verb rotates
  on long turns (`cooking… → thinking… → …`); the previously-dark autonomy line
  (goals/cron counts) is now populated by a slow fail-open background poll.
- New shared modules: `cli/ui/candy.py` (plain-string view grammar helpers),
  `cli/ui/slash_highlight.py`, `cli/ui/hints.py`, `cli/ui/autonomy_poll.py`;
  glyph/style vocabulary consolidated in `cli/ui/theme.py`. No new env flags.

### Owner-UX Phase 4 — surface parity: recap core, Telegram owner verbs, Preferences page (2026-07-14)

Closes out the owner-UX usability wave's Phase 4 (surface parity): the CLI/REPL,
every chat surface (Telegram — and everything sharing its dispatch: WhatsApp,
Discord, Slack, Signal, X, Email), and the web Console now expose the same
read-only situational-awareness verbs and the same typed-preference control
surface, instead of each surface having absorbed a different slice of the
recent agent waves.

- **Surface-neutral recap core** (`core/recap.py`) — the episodes/events/
  skills/ledger assembly behind `/journey` was extracted out of the CLI
  rendering layer into one pure, dependency-injected `build_recap`, so any
  surface reuses the exact same data-gathering instead of re-implementing it.
  Hardened this pass: `_parse_window` now rejects a window that parses to
  something unusable — non-finite (`nan`/`inf`, e.g. `"1e400d"` overflowing
  to `inf`, or `"nand"` parsing as `float("nan")` because the trailing char
  happens to be the `d` suffix) or absurdly large (> ~10 years, e.g. a
  30-digit day count) — with the same friendly `ValueError` as a malformed
  label, rather than relying on `int(nan)` coincidentally raising somewhere
  downstream. Telegram's `/recap [window]` exposes the raw label to chat
  input, so this is a real hardening, not just belt-and-suspenders.
- **Telegram owner verbs** (shared by every surface on the same dispatch):
  `/status` (bound-session state, goal counts, next cron run, cost over the
  trailing 24h), `/recap [window]` (alias `/journey`), `/goals` (board
  summary), `/prefs` (read-only effective preferences) — owner-gated by the
  resolved principal (the local-CLI bypass is never honored on a network
  surface), tenant-scoped, and backed by the SAME primitives `polyrob owner`
  and the REPL slash commands use.
- **Webview Preferences page** (`/preferences` + GET/PATCH
  `/api/webgate/preferences`) is real: schema-driven from
  `core.prefs.PREF_SCHEMA`, safe keys apply on write, guarded keys need an
  explicit `confirm:true` (409 without it), `WEBVIEW_READ_ONLY` blocks all
  writes. A same-wave review caught and fixed a confirm-bypass in the parallel
  T3 commit: a truthy non-boolean `confirm` (the string `"false"`, or `1`) was
  being accepted as confirmation — the PATCH handler now requires a literal
  JSON `true` and 400s on a malformed/non-dict body or a missing `value`.
- **Hardening**: `surfaces/telegram/harness.py` — `_status_reply`/
  `_goals_reply` now share the caller's already-open `GoalBoard` instead of
  each opening a second connection to the same `goals.db`; `/status`'s
  "Cost today" line is relabeled "Cost (24h)" (it's a rolling window, not a
  calendar-day figure).
- **Docs**: `docs/guide/architecture.md` gains a "Chat-surface owner
  commands" table documenting `/status`/`/recap`/`/goals`/`/prefs` (plus the
  pre-existing `/pending`/`/approve`/`/asks`/`/allow` verbs) for every chat
  surface; `console.md`'s Preferences section and `cli.md`'s REPL command
  table were already accurate for this wave.

## [0.7.0] — 2026-07-14

### Model registry refresh — current Anthropic lineup + new OpenRouter Grok/GLM (2026-07-14)

Brought the model registry (`modules/llm/model_registry.py`, the pricing/limits
SSOT) up to the current model landscape; the per-provider clients read from it, so
this is registry-scoped. Provider defaults are unchanged (`anthropic` stays
`claude-sonnet-4-5`, `openrouter` stays `z-ai/glm-5.2`).

- **Anthropic — added the modern lineup:** `claude-fable-5` ($10/$50),
  `claude-opus-4-8`, `claude-opus-4-7`, `claude-opus-4-6` ($5/$25),
  `claude-sonnet-5`, `claude-sonnet-4-6` ($3/$15) — all 1M-context native, 128K
  output, with dotted/short aliases. These use **adaptive** thinking, so the
  registry deliberately leaves `thinking_budget_tokens` unset (`budget_tokens` is
  rejected with a 400 on Fable 5 / Opus 4.7-4.8 / Sonnet 5) — `get_thinking_config`
  returns `{}`, byte-identical to prior behavior. The 4.5 family + Opus 4.1 stay as
  the active legacy tier.
- **OpenRouter — new Grok:** added `x-ai/grok-4.5` (500K ctx, $2/$6, newest xAI
  flagship) and `x-ai/grok-4.20` (2M ctx, $1.25/$2.50). The unknown-`grok`
  fallback now targets `grok-4.5` instead of the 404'd `grok-4.1-fast`.
- **OpenRouter — GLM tiers + drift fixes:** added `z-ai/glm-5.1`,
  `z-ai/glm-5-turbo`, `z-ai/glm-4.7-flash`; refreshed `z-ai/glm-5.2` to its live
  price/output-cap ($0.93/$3.00, 32K max out; was $1.20/$4.10, 256K) and
  `x-ai/grok-4.3` context to 1M (was 2M) with a cache-read price. All prices
  re-verified against the live OpenRouter models API on 2026-07-14 and pinned in
  `tests/unit/modules/llm/test_openrouter_pricing_verified_2026_07_14.py`.

### CLI rendering finalization — no more corrupted REPL frames (2026-07-13)

Root-caused and fixed the REPL screen corruption (ghost prompt frames, stranded
status rows, floating spinner glyphs, raw `INFO httpx:` lines in the transcript):

- **Logging containment (the root cause):** component-logger creation no longer
  bounces root/handler levels back to INFO; noisy library loggers (httpx et al.)
  are pinned at setup in every process; console and file sinks have independent
  levels (CLI terminal shows ERROR+ only while `bot.log` keeps INFO); the stderr
  handlers resolve `sys.stderr` at emit time so nothing can write past
  prompt_toolkit's `patch_stdout` coordination; httpx records no longer
  double-emit. On the server this newly activates the library-logger pinning
  too — the old `initialize_task_logging` path had been dead code (import of
  a nonexistent name), so prod `bot.log` no longer records per-request httpx
  INFO lines.
- **REPL hardening:** `Ctrl-L` clears + repaints (corruption recovery); the
  SIGINT handler schedules its notice onto the loop instead of painting from
  the signal frame; the per-event usage poll is throttled (0.5s).
- **Hygiene:** dead `cli/ui/pick.py` removed; doc anchors corrected.

### Correspondent conversation architecture fixes (2026-07-13)

Fixes across the correspondent conversation architecture — the
reply→origin-session loop now actually works, time-stretched conversations
survive session death, and multi-contact outreach stops hitting silent walls:

- **Ephemeral delivery rail fixed (default-on bugfixes):** a correspondent
  reply into a resident+completed session now WAKES the run loop (the
  pending-input probe sees ephemerals), unconsumed ephemerals survive
  eviction/restart (persisted in `message_history.json`), and the queue is
  bounded (`MAX_EPHEMERAL_MESSAGES`, default 30, drop-oldest).
- **Reply bindings on every outbound path:** the proactive `message` tool now
  seeds the correspondent registry itself (it previously used a synthetic
  session key no seed could resolve — first-contact replies were DENIED on
  every surface). Seeding runs BEFORE the send; a cap-refusal blocks the send
  instead of orphaning the reply; the cap exempts already-known addresses.
  The seed guardrail moved to `core/surfaces/seed.py` (email module re-exports).
- **Real email threading:** outbound email mints + returns its Message-ID,
  sets `In-Reply-To`/`References`, and each outbound is bound to its sending
  session via registry thread-anchor rows — two sessions emailing one address
  now each get their own replies. Same-tenant address-only ambiguity routes to
  the most recent conversation (`CORRESPONDENT_RESOLVE_LATEST`, default ON);
  cross-tenant stays denied.
- **ConversationStore (`core/surfaces/conversations.py`):** durable
  per-(tenant, surface, address) conversation container — bounded message log,
  context block prepended to every injected reply, `contact_history` action
  (per-address transcript or who-replied listing), and **session re-pointing**:
  a reply to a dead session resumes into a fresh session with full context
  (`CONVERSATION_RESUME_ENABLED`, default ON) instead of being silently
  dropped.
- **Scoped reply-while-tainted** (`CORRESPONDENT_REPLY_ENABLED`, default OFF):
  opt-in exemption letting a tainted session answer EXACTLY the correspondent
  who wrote in (1:1, no cc/bcc, `CORRESPONDENT_REPLY_MAX_ROUNDS`/24h,
  fail-closed) — unblocks autonomous multi-round exchanges when the operator
  chooses.
- **Owner UX + hygiene:** pending correspondents now appear in
  `polyrob owner pending` (+ `correspondent_pending` event on seed);
  `polyrob owner approve --all [<surface>]` bulk-approves;
  `CORRESPONDENT_TTL_DAYS` wires the never-called `purge_expired`; taint is
  lock-guarded and source-tracked; the shared inbound handler serializes
  per-chat (KeyedLock) so rapid cold messages can't double-create sessions.
- Cut per owner decision mid-wave: the planned OutreachStore campaign
  subsystem (the ConversationStore already answers who-was-contacted /
  who-replied; grouping is the agent's job via goals/notes).

### UI surface parity wave — webview control surfaces + CLI verb parity (2026-07-12)

Closes the gaps from the 2026-07-12 UI-surface review (each owner surface had
absorbed a different slice of the recent agent waves):

- **Webview Preferences page** (`/preferences` + GET/PATCH
  `/api/webgate/preferences`) — completes the half-done owner-UX Phase 4 T3:
  schema-driven view/edit of typed prefs over the same `core.prefs` seams the
  CLI/REPL/agent use; guarded keys confirm-gated (409 → `confirm:true`);
  `WEBVIEW_READ_ONLY` blocks writes.
- **Webview Pending-review queue** (`/pending` + `/api/webgate/pending*`) — a
  web-only owner can finally approve/reject quarantined proposals; same
  `core.self_evolution` aggregator as `polyrob owner`, REPL `/pending`,
  Telegram `/approve`.
- **`polyrob finance` + REPL `/finance`** — the unified-ledger balance sheet
  (earned/spent/pending/net) was webview-only; one shared renderer over
  `build_ledger`.
- **`polyrob cron`** (`schedule/list/show/cancel`) — cron jobs were creatable
  from NO human surface (agent-tool only); rides the same `CronService` +
  `cron.db` the ticker runs; warns when `CRON_ENABLED` is off.
- **One recap vocabulary** — REPL answers `/recap` (alias of `/journey`),
  Telegram answers `/journey` (alias of `/recap`).
- **One data-home resolver** — `core.runtime_paths.resolve_data_home()`
  replaces four byte-duplicated `_data_dir()` helpers (webview pages/activity,
  cli owner/surface); webview wrapper `webgate.data_dir()` keeps the
  standalone-deploy fallback.
- **Surface-parity contract test** (`tests/unit/test_surface_parity.py`) —
  pins the capability→surface matrix (CLI/REPL/webview/Telegram) like the
  flags catalog, so a rename/removal on any surface fails CI.
- **Hygiene**: dead `chat.js` handlers for never-emitted socket events
  removed (server emits only `stream_chunk`); `POLYROB_API_BASE` replaces
  three hardcoded `127.0.0.1:9000` proxy URLs; every direct Rich print in the
  REPL handlers now routes through the secret scrub (16 sites, source-pinned).
- **Docs**: `docs/guide/cli.md` caught up (12 undocumented commands, 4 slash
  rows, the real 16-verb `owner` group); `console.md` documents
  Finance/Preferences/Pending-review; READMEs refreshed; the executed webview
  rename/align handoff archived with a status banner.

## [0.6.0] — 2026-07-12

### Chat connectors Wave 5 — X (Twitter) DM surface + connector validation hardening (2026-07-12)

- **X (Twitter) DM surface** — `surfaces/x/`: polling inbound (`GET /2/dm_events` — the
  pay-per-use tier has no DM webhook; Account Activity is enterprise) with a durable
  since-id cursor (`x_cursor.json`, first run marks history seen instead of replaying up
  to 30 days), tweepy OAuth 1.0a user-context calls run off-loop (the SAME `TWITTER_*`
  creds the twitter tool uses — no second credential store), 429 backoff to the
  `x-rate-limit-reset` epoch, dm_event-id dedup, and outbound via
  `POST /2/dm_conversations/with/:participant_id/messages` (10,000-char DM cap, split).
  1:1 conversations only in v1 (group DM conversations are skipped — replying via
  `/with/:participant_id` would leak into a private thread). Run with `polyrob x`
  (`X_SURFACE_ENABLED`; poll cadence `X_DM_POLL_SEC`, default 90s — DM reads are
  15 req/15 min per user). The `message()` tool now lists `x` among its surfaces.
- **X capability completion (twitter tool)** — new always-on reads `twitter_get_dms`
  (recent DM events, optional 1:1 participant filter — the agent could previously SEND
  DMs but never read them from a task) and `twitter_get_timeline` (a user's recent
  tweets; the internal helper existed but was never exposed, and a tweet with no
  `created_at` no longer crashes the whole timeline read); new gated write
  `twitter_unmute` (mute existed with no undo). `twitter_block`'s description now warns
  it is Enterprise-only on X API v2 (403 on pay-per-use — mute instead). All inherit the
  existing approval/rate gates, correspondent-taint blocking (tool-id match), and
  untrusted-result wrapping.
- **datagen: exports actually see real sessions now** — three layout bugs made every
  real-deployment export empty or label-less: `read_agent_steps` read `<session>/history/`
  but the live ledger is written to `<session>/data/history/` (history_io via
  `pm().get_history_dir()`) so every record exported `steps=[]`; `iter_session_dirs` only
  globbed the legacy `*/sessions/*` shape and yielded NOTHING under the canonical direct
  layout (server `data/task/<user>/<sess>`, local `<home>/sessions/<user>/<sess>`); and
  episode-label lookups assumed `memory.db` at `pm().data_root` when it lives in
  `BotConfig.data_dir` (the parent, per `backend_factory`) so every record was
  `outcome=unknown` (`find_memory_db` now resolves it; `session export` too). The batch
  runner no longer lets an assemble/export exception escape `asyncio.gather` and abort the
  whole run (counted `failed` + logged), logs the no-session/refusal branch, and its tests
  now build the REAL session layout (the old fixtures matched the buggy paths, which is
  why CI was green while production exported nothing).
- **Connector hardening (validation pass over the Wave 3/4 surfaces)** —
  **Slack:** user-id (`U…`/`W…`) send targets now go through `conversations.open`
  (cached) — `chat.postMessage` only accepts conversation ids, so a `message()`-tool/sink
  DM target previously landed in Slackbot or failed (`open_dm` was dead code);
  `not_in_channel` errors carry an actionable `/invite` hint; the Socket Mode read loop
  dispatches agent turns as tasks instead of awaiting inline (a slow turn blocked the
  NEXT envelope's ACK past Slack's redelivery timer) and skips malformed frames instead
  of tearing down the socket. **Discord:** the gateway now detects a half-open socket
  (missed HEARTBEAT_ACK → force-reclose + reconnect), answers server heartbeat requests
  (op 1), honors RECONNECT (op 7), and waits before re-IDENTIFY on INVALID_SESSION
  (op 9, IDENTIFY-rate-limit protection). **Signal:** endpoints verified against the
  native `signal-cli daemon --http` docs (JSON-RPC `/api/v1/rpc` + SSE `/api/v1/events`
  are correct — NOT the bbernhard REST daemon); the SSE unwrap now also handles the
  stdio JSON-RPC notification shape (`params.envelope`, previously silently dropped),
  an empty `account` param is omitted, and `sourceUuid` senders parse when no number
  is present.
- **Group/channel ingress is now default-DENY at the dispatcher** — with
  `GROUP_CHAT_ENABLED` off (the default), a group/channel message is silently denied
  instead of falling through to the legacy obey-path. The fall-through meant a bot
  invited into a Discord/Slack channel would obey ANY room member (those surfaces have
  no sender allowlist of their own; only telegram has `ALLOWED_TELEGRAM_USER_IDS`).
  DMs are unchanged. Group-participant coverage now also exercises the REAL
  `bind_chat_surface` write path (the `SINGULAR_CHAT_ENABLED` gate the daemons set).

### Owner-UX Phase 3 — builtin user-guide skill, generated config reference, `agent_status` config section, init guardrails (2026-07-12)

Closes the "the agent doesn't know what it is" gap: a grounded, self-referential map of
POLYROB's own surfaces/config/autonomy/money model, plus the setup and introspection
surfaces that keep it honest at runtime.

- **`polyrob-user-guide` builtin skill** (`data/prompts/skills/polyrob-user-guide/`,
  priority-1 `auto_activate`) — the map of what POLYROB is, its surfaces, the four
  configuration layers (env flags / `preferences.toml` tighten-only / `contract.md` /
  SOUL-SELF-owner-facts), the conversational `preferences` action, autonomy and
  money/safety at a concept level, skills/learning, a verbatim anti-hallucination clause,
  and a live-grounding rule (never assert a *current* config value from this static skill —
  use `agent_status`/`preferences`/`polyrob doctor --flags`). Five `references/` files
  (autonomy, surfaces, skills-and-learning, money-and-safety, setup-interview) are loaded
  on demand via `load_skill`/`read_skill_resource`, not eagerly injected.
- **`scripts/gen_user_guide_refs.py`** generates `references/configuration.md` (a compact,
  human-facing per-group env-flag reference) from `docs/CONFIGURATION.md`, mirroring
  `scripts/gen_flags_catalog.py`'s doc-walk. A drift contract test
  (`tests/unit/core/test_user_guide_refs.py`) asserts the committed reference is exactly
  what the checked-in generator produces and that every `core/flags_catalog.py` flag is
  discoverable in it.
- **`agent_status` config section** — the read-only introspection action now reports
  resolved preference values/sources (`key = value (source)`, grouped), the three posture
  axes (`compute`/`autonomy`/`local`), and which autonomy loops are enabled, all run through
  the core secret-shape scrubber as a defensive backstop. Fails soft to an explicit
  `config: unavailable` line (not silent omission) so "what's my effective config" always
  gets an honest answer, independent of the other five sections (steps/tools/context/
  wallet/ledger) that stay available even under a full orchestrator/wallet/ledger blackout.
- **`polyrob init` — "5/5 Autonomy & guardrails"** wizard section (after Owner pairing,
  before the summary; skipped by `--quick`/non-interactive): enable local mode
  (`POLYROB_LOCAL=1`), an autonomy budget (`AUTONOMY_BUDGET_USD`), the recommended approval
  preset (`APPROVAL_REQUIRED_TOOLS` + `APPROVAL_PROVIDER=interactive_cli`), and a daily
  digest channel (written as a `digest.channel`/`digest.enabled` preference when an owner id
  is known, else an `OWNER_DIGEST_ENABLED` env note) — every prompt blank-to-skip. The
  final summary now points the owner at the new skill: `Ask me anything about myself —
  try "what can you do?"`.
- **Fixes**: `scripts/gen_flags_catalog.py` no longer lets the `AUTONOMY_POSTURE`/
  `AGENT_COMPUTE_POSTURE` single-flag mini-tables' own header row (`| \`NAME\` | Default |
  What it does | Code anchor |`) win the first-occurrence dedup over their real default row
  — `core/flags_catalog.py` now reports `silent`/`0` instead of the literal string
  `"Default"`. `docs/guide/cli.md`'s slash-command table gained `/pending`, `/approve`,
  `/config`, and `/context` (all pre-existing REPL commands the table had never listed).

### Owner-UX Phase 2 — conversational preferences, contract doc, approval ladder (2026-07-12)

Closes the "settable but not yet consumed" gap Phase 1 left open — the agent can now
read/change its own tenant preferences and propose durable operating rules from inside a
conversation, guarded changes ride one owner review queue, and the approval workflow gained a
real ladder + CLI/REPL parity.

- **`ContractWriter` + `contract.md` two-tier doc** (`core/contract_writer.py`, a thin
  `SelfContextWriter` subclass) — an owner-authored/agent-proposed `## Operating contract`
  block, injected each session alongside SOUL/owner-facts/SELF, gated `CONTRACT_DOC_ENABLED`
  (default **ON**). A deterministic one-line style summary from typed prefs
  (`core.prefs.render_style_line`, covering `style.verbosity`/`style.language`/`style.tone`/
  `digest.quiet_hours`) rides the same block — no file + no style prefs set stays
  byte-identical (`""`). `CONTRACT_DOC_REQUIRE_REVIEW` (default **ON**) gates whether a
  `contract_propose` write activates immediately or lands in `.pending/`; a forged/background
  author (self-wake, sub-agent/leaf, autonomous run) is quarantined unconditionally regardless
  of the flag.
- **Agent-callable `preferences` action** (`tools/controller/action_registration.py::
  _register_preferences_action`, gated `PREFS_TOOL_ENABLED`, default OFF / **ON under
  `POLYROB_LOCAL`**) — `list`/`get` read the typed schema (effective value/source/`applies`);
  `set` writes SAFE keys immediately or queues a guarded proposal for GUARDED keys (owner
  reviews via `/pending`); `contract_propose` proposes operating-contract text. `set` is
  refused outright for any forged/autonomous turn; a leaf/sub-agent never sees the tool
  (`delegation_exclusions_for_child`); a correspondent-tainted session is denied the whole
  action. Free-text display (`get`'s description echo) is re-scanned at read time so a
  hand-edited `preferences.toml` can't smuggle prompt injection into the agent's own reply.
- **One pending/approve pipeline for guarded pref changes** — `propose_pref_change` writes a
  `pref_change` kind row into the SAME quarantine queue skills/self-context already use;
  removals are tracked by **operation** (add vs. remove), not a stale full-set snapshot, so two
  concurrent proposals against `approvals.require` can't clobber each other on promote.
- **`/approve` REPL + `polyrob approvals`** (`cli/ui/commands/h_approve.py`,
  `cli/commands/approvals.py`) — both list/add/remove the approval-gated action set through the
  SAME `tools.controller.approval.effective_approval_state()` helper `Controller.__init__` uses
  to wire the hook, so displayed state can never drift from enforcement. `add` unions a gate in
  directly (tightening needs no review); `remove` of a pref-added entry queues a guarded
  `pref_change` proposal instead (an env/posture-added entry is explained, not removable).
- **Approval ladder** (`InteractiveCLIApprover`, owner-UX P2 T5) — the old y/n prompt is now
  `o`=once, `s`=session (in-memory per-action auto-approve), `a`=always (approves + queues a
  guarded `approvals.require` removal proposal when applicable), `d`=deny, `n`=never (appends to
  `approvals.deny` immediately — tightening a denylist is always safe, no review needed).
  Missing tenant context degrades `a`/`n` to `s`/`d` with a notice rather than crashing.
- **Real `/persona` and `/toolset` switches** — both REPL commands now persist
  `session.persona`/`session.toolset` (threat-scanned, validated against `TOOLSETS`) instead of
  only showing a read-only detail view, honestly labeled "applies next session" (the system
  prompt's `<identity>` block is built once at agent-creation time, so neither can retroactively
  change the CURRENT turn); `/persona` best-effort refreshes the live `_persona_block` for
  anything freshly created within the same session (e.g. a delegated sub-agent).
- **`polyrob wallet set-cap`** — guided, confirmed CLI for setting the wallet daily/per-tx
  money caps as the **env-authoritative** values (`WALLET_DAILY_CAP_USD` /
  `AGENT_WALLET_MAX_PER_TX_USD`, upserted into `~/.polyrob/.env` — NOT the preference-write
  path; a per-user preference may only tighten below the env cap, never raise it).
- **Docs/flags** — `PREFS_TOOL_ENABLED`/`CONTRACT_DOC_ENABLED` rows in
  `docs/CONFIGURATION.md` dropped their "(reserved)" language now that both are wired; added
  `CONTRACT_DOC_REQUIRE_REVIEW`. `PREF_SCHEMA` descriptions for `style.verbosity`/`style.tone`
  dropped their "not yet consumed" markers (now rendered into the style line);
  `goals.notify_on_done`, both `autonomy.*` keys, and `budget.wallet_per_tx_usd` remain
  genuinely unconsumed and are reworded "not yet consumed — future phase".

### Memory/knowledge finalization — repairs + the knowledge layer (2026-07-12)

Implements the validated portions of the 2026-07-11 memory/context/knowledge review.

**Repairs (Phase B):**
- **Provenance sidecar (D1/B2)** — `mem_provenance(mem_rowid, user_id, ts, kind,
  content_hash)` stamped on every cross-session memory write; recall lines now render
  `- [YYYY-MM-DD] …` (legacy stampless rows stay bare). Exact duplicates collapse at
  write (refresh ts, skip insert). `core/sqlite_util.execute_retry` gains
  `fetch="lastrowid"`.
- **Retention (D3/B3)** — `MEMORY_RETENTION_DAYS` (default 365, `<=0` off): age-based
  prune of stamped `memories` rows on the curator tick; the `local_vector` backend also
  sweeps its `mem_meta`/`mem_vec` sidecar. The store no longer grows forever.
- **Row cap (D8)** — `MEMORY_ROW_MAX_CHARS` (default 4000) caps auto-injected memory
  rows at the shared FTS/vector composition point.
- **Episode artifacts (D4/B4)** — `collect_provenance` now routes through the evidence
  pack's `collect_artifacts`; episode rows carry real artifact lists.
- **H-MEM selection (D5/D9/B5)** — when finding importance is flat (the sub-prune
  regime), selection falls back to recency: the newest findings win the display slice
  instead of the oldest 15.
- **Continuity re-injection (D6)** — `_maybe_inject_autonomous_continuity` gains the
  once-per-session bootstrap guard (self-wake re-entries no longer re-inject).
- **DB manifest (D11/B6)** — `telemetry_events.db`, `surfaces.db`, `pairing.db`,
  `messages.db`, `wa_dedup.db`, `email_dedup.db` added; backup/rollback no longer
  silently skips them.
- **Dead code (B7)** — ~180 LOC of caller-less H-MEM helpers, the permanently-dead
  `knowledge_base` branch in `agents/base_agent.py`, and the dead `UserProfileManager`
  (527 LOC; its RBAC link called a method that never existed) deleted.

**Knowledge layer (Phase C):**
- **Notes substrate (C1)** — `curated_memory` promoted to first-class notes (additive
  columns: title/tags/`[[wikilinks]]`/source/timestamps/access_count/status/created_by);
  `memory` tool verbs extended to `create/update/archive/list/show` — writes threat-
  scanned fail-closed, forged/autonomous turns quarantine to `pending` and can never
  mutate active notes, note ops emit `self_modification` audit events.
- **`/knowledge` webview section (C2)** — read-only wiki: notes (incl. pending),
  episode browser (outcome/artifacts/spend), skill catalog + pending drafts, KB
  sources, and a changes tab over the durable event log.
- **Obsidian export (C3)** — `polyrob knowledge export [--out --since --user]` writes
  a markdown vault (notes with frontmatter + working wikilinks, daily episode logs,
  skills, identity docs, goals, index) — export-only projection, DBs stay SSOT.
- **Note consolidation (C4)** — `KNOWLEDGE_CURATOR_ENABLED` (local-ON): curator tick
  archives never-read agent-authored notes past `KNOWLEDGE_NOTE_STALE_DAYS` (90) and
  collapses exact duplicates. Archive-only, audited, LLM-free.

### Chat connectors Wave 3 — group-chat access + Discord surface (2026-07-12)

Chat connector wave — group-chat access model plus Discord/Slack/Signal surfaces.

- **Group-chat access model** — `GROUP_CHAT_ENABLED` (default OFF; since Wave 5 OFF ⇒
  group/channel messages are silently denied at the dispatcher, not legacy fall-through):
  only chats in a new default-DENY group allowlist (`polyrob owner groups
  allow|deny|list`, `core/surfaces/group_allowlist.py`) are served; the owner keeps the
  normal command/steer flow (mention-gated via `GROUP_REQUIRE_MENTION`, default ON); any
  other member becomes the new `GROUP_PARTICIPANT` tier whose @mentions route as untrusted
  DATA into the bound group session (the existing correspondent rail: `<correspondent-message>`
  wrap + capability taint) — participants can never command, steer, or start sessions.
  Group denials are **silent** (`RouteDecision.silent`) so channels never get auth spam.
  Fail-closed once enabled; the local-owner bypass is refused inside groups.
- **Discord surface** — `surfaces/discord/`: thin aiohttp REST client + hand-rolled
  Gateway-WS consumer (IDENTIFY/heartbeat/backoff-reconnect; GUILDS, GUILD_MESSAGES,
  DIRECT_MESSAGES, MESSAGE_CONTENT intents — no discord.py dependency), `Surface` impl
  with 2000-char splitting, dedup, typing indicator, and the shared
  `route_inbound`/`act_on_inbound` pipeline. Run with `polyrob discord`
  (`DISCORD_BOT_TOKEN`; `DISCORD_SURFACE_ENABLED`). The `message()` tool now lists
  discord among its surfaces.
- **Slack surface (Wave 4)** — `surfaces/slack/`: thin Web-API client + **Socket Mode**
  WS consumer (envelope ACKs, `disconnect` rotation, backoff — no slack-bolt, no public
  URL). DMs + channels (threads ride `thread_ts`), 4000-char split, edit support. Run
  with `polyrob slack` (`SLACK_BOT_TOKEN` + `SLACK_APP_TOKEN`; `SLACK_SURFACE_ENABLED`).
- **Signal surface (Wave 4)** — `surfaces/signal/`: thin client for a local
  `signal-cli daemon --http` (JSON-RPC send at `/api/v1/rpc`, SSE receive at
  `/api/v1/events`, backoff-reconnect), DM-first (groups parse and ride the W3 gating;
  Signal has no mentions, so mention-gated groups stay silent unless
  `GROUP_REQUIRE_MENTION=false`), min-interval send throttle. Run with `polyrob signal`
  (`SIGNAL_DAEMON_URL` + `SIGNAL_ACCOUNT`; `SIGNAL_SURFACE_ENABLED`).

### Training-data rig Wave 1 — trajectory export + capture (2026-07-11)

New top-level `datagen/` package assembles a session's persisted artifacts (message
history, step-level agent ledger, LLM usage, episode/RunOutcome labels) into a canonical
training record.

- **Formats** — `raw` (lossless), `sharegpt` (`from/value` with
  `<think>`/`<tool_call>`/`<tool_response>` conventions), `openai` (messages+tools JSONL);
  labels + provenance ride alongside so corpus filters never re-parse content.
- **Fail-closed scrub** — every export passes `core.secret_scrub` + a JSON-aware credential
  rule; a scrub failure REFUSES the record. Images stripped; sessions containing
  correspondent (third-party) messages are excluded by default.
- **CLI** — `polyrob session export --format raw|sharegpt|openai` (also fixes the exporter
  missing `memory/message_history.json`) and `polyrob datagen export --filter outcome=done`
  for bulk, label-filtered corpora (rejection-sampling-ready).
- **Opt-in capture** — `TRAJECTORY_CAPTURE` (default OFF, never in the local safe group)
  captures each finished run as a labeled record under `<data_root>/datagen/captured/`.
- **Batch rollout runner (Wave 2)** — `polyrob datagen run --tasks tasks.jsonl`: JSONL
  prompts → agent rollouts on the goals/cron session rail with per-task Bernoulli toolset
  sampling (`datagen/toolset_distributions.py`), bounded
  concurrency + per-rollout wall-clock cap, content-based checkpoint/resume, outcome-labeled
  `rollout_*.json` + merged sharegpt `corpus.jsonl` + `statistics.json` (incl. spend).
  Runner process forces trajectory hygiene (memory/project-context/autonomy off) via
  `os.environ.setdefault` so an explicit operator value still wins.

### Owner-UX Phase 1 — typed per-user preferences layer (2026-07-11)

A curated, schema-validated preferences layer so an owner/tenant can tune agent behavior
without touching env flags or restarting — `core/prefs.py` (`PREF_SCHEMA`/`validate_pref`),
stored per-tenant at `identity/{instance_id}/user_{uid}/preferences.toml`, gated
`PREFS_ENABLED` (default **ON**; inert with no `preferences.toml` present — byte-identical
legacy behavior).

- **Schema + storage + resolver** — 21 curated keys across `approvals.*`/`budget.*`/
  `goals.*`/`digest.*`/`delivery.*`/`style.*`/`session.*`/`autonomy.*`, each typed
  (bool/int/float/str/list/enum) and merge-tagged. Resolution is `pref > env > default`
  EXCEPT **guarded** keys (approvals, budgets, `autonomy.self_wake`/`background_review`),
  which merge most-restrictive (`min`/`union`/`and`/`stricter_provider`) so a preference can
  only **tighten** operator policy, never widen it. No secret-typed keys, ever. Atomic
  temp+replace writes; malformed/missing TOML fails open to `{}` (never breaks the agent).
- **Read-site threading** — goal daily-quota/concurrency, the autonomy spend budget, the
  wallet daily cap, delivery rate/daily caps, digest enabled/channel, session default
  toolset/persona, and the approval-required/deny/provider union are all resolved through
  the pref layer at their existing read sites; `approvals.provider` never lets a custom
  (non-standard) `APPROVAL_PROVIDER` be overridden by a pref. Wallet-budget note: only the
  read-side helper (`core.wallet.config.effective_daily_cap_usd`) shipped — wiring it into
  the `PolicyGate` spend-enforcement path itself is still pending. **Settable but NOT YET
  consumed anywhere** (the value round-trips through `preferences.toml`/`/config`, but no
  read site threads it): `digest.quiet_hours`, `goals.notify_on_done`, every `style.*` key,
  both `autonomy.*` keys (`self_wake`/`background_review`), and `budget.wallet_per_tx_usd`.
  Those consumers land in Phase 2.
- **Secret-guard + write-gate enforcement** — `preferences.toml` and the (Phase-2) two-tier
  `contract.md` are hard-denied to every agent file-write surface (filesystem/coding tools)
  under any `identity/` path segment, case- and segment-robust
  (`agents/task/agent/core/secret_guard.py::is_protected_config_path`); writable ONLY through
  the gated `write_preference` seam.
- **`/config` REPL + `polyrob config`** — `/config list|get|set|check` and the validated
  `polyrob config set` route a KEY to secret / per-user preference / catalog-checked env flag,
  hard-rejecting an undocumented key unless `--force`; `polyrob config check` cross-validates
  env files and a tenant's `preferences.toml` against the flags catalog, never printing a
  secret value.
- **`/context`** — a context-assembly breakdown REPL command showing what's actually being
  injected into the running session, one line per populated foundation slot with token
  count + % of context: system prompt, runtime identity, SELF_CONTEXT (SOUL/SELF),
  PROJECT_CONTEXT, initial task, skills, and conversation history.
- **Flags** — `PREFS_ENABLED` (ON), `PREFS_TOOL_ENABLED` / `CONTRACT_DOC_ENABLED` (reserved,
  ship with the Phase-2 agent-callable pref-write action and owner-facing contract doc); see
  the new "Preferences (owner UX)" group in `docs/CONFIGURATION.md`. Also closed a
  doc/catalog gap for four real, actively-read legacy env vars (`DEFAULT_MODEL`/
  `DEFAULT_PROVIDER`/`CHAT_MODEL`/`CHAT_PROVIDER`) that had no catalog row.

### `hf_deploy` — publish the workspace as a Hugging Face Space (2026-07-10)

New optional agent tool (`tools/hf_deploy/`): `deploy`/`undeploy`/`list_deployments`
publish the session workspace as a
Hugging Face Space (Docker SDK). OFF by default (`HF_DEPLOY_ENABLED`), never in
default tool_ids, gated `compute_posture_allows(ctx, 2)` (self-maintenance
tier) + owner tenant + not leaf/sub-agent/forged-turn.

- **Ship==tested acceptance-contract leg** (`tools/hf_deploy/digest.py`) — every deploy is
  refused unless the session's action ledger shows a green `run_tests` with no code-edit action
  since (reuses `agents.task.runtime.edit_verify.edited_since_last_test`); the deployed tree's
  sha256 digest is recorded alongside the live row.
- **Tenant-scoped registry** (`tools/hf_deploy/registry.py`, `deployed_apps.db` via
  `core/sqlite_util` WAL+jitter) tracks pending/approved/live/failed/undeployed per
  `(app_name, user_id)`, a per-tenant deploy-attempt ledger (`HF_DEPLOY_DAILY_MAX`,
  `HF_DEPLOY_MIN_INTERVAL_SEC`), and feeds a fail-open boot-time reconcile sweep
  (`tools/hf_deploy/reconcile.py`, wired into `core/autonomy_runtime.py`) that re-health-checks
  `live` rows and flips a dead Space to `failed`.
- **Token custody** (`tools/hf_deploy/broker.py::HFSpacesBroker`) — `HF_TOKEN` is read+stripped
  at call time and flows ONLY into the injected/lazy `huggingface_hub.HfApi`; never a param,
  result, or log line (errors are token-scrubbed). `huggingface_hub` is an OPTIONAL lazy import —
  its absence surfaces as a clear tool error, not an ImportError.
- **First-publish approval vs. approved-app redeploy** — the FIRST publish of a new app name is
  gated by a real approving provider: the tool resolves the SAME interactive-default provider the
  Controller uses at posture≥2 (`resolve_gated_actions`), so an unattended/headless run cannot
  first-publish a new PUBLIC app (`interactive_cli` fail-closes to deny). Once approved
  (registry-backed), a redeploy of that SAME app runs unattended within the caps (it skips the
  approver). `deploy` is deliberately NOT in the Controller's `APPROVAL_REQUIRED_TOOLS` sets — a
  blanket Controller gate can't tell first publish from redeploy, so the tool owns that distinction.
- Added to `DELEGATE_BLOCKED_TOOLS` and the correspondent-gate high-impact set (never delegated,
  never reachable from a correspondent-tainted session). Registered via the CLI optional-tool
  registrar (`core/bootstrap.py::_CLI_OPTIONAL_REGISTRARS`) and attached to autonomous goal runs
  only at posture>=2 (`agents/task/goals/dispatcher.py::default_goal_tools`).
- Owner runbook: `docs/guide/self-hosting.md` ("Letting the agent deploy to Hugging Face Spaces").

### Intelligence stack — outcome integrity, agent-owned communication, durable goals (2026-07-10)

Implements the approved intelligence-stack finalization (P0, v2):

- **RunOutcome envelope (§2)** — one canonical, typed outcome object assembled once at run end
  (`agents/task/runtime/run_outcome.py`); the done() text comes from the ACTION LEDGER, never from
  message-history strings. Fixes the live corruption class where an honest
  `done("OUTCOME: BLOCKED — …")` was recorded as ✅ success `"Processing actions"`; placeholder and
  generic status strings are now unrepresentable as results. Root-cause fix in
  `_extract_chat_reply` (read `agent.history`, not the nonexistent `agent.state.history`).
- **Mechanical evidence pack (§4.1) + invariants (§4.2)** — action ledger, workspace/ledger artifact
  diff (populates `episodes.artifacts`, empty in all 230+ episodes ever), final-step errors,
  ids/urls from successful results. NEW invariant: done() where every substantive action errored →
  failure.
- **One user-delivery rail (§3.1–3.2)** — `core/surfaces/user_delivery.py`: agent `send_message`
  from autonomous sessions now reaches the session's own principal (it used to die in the session
  feed); cron delivery and `push_owner_message` ride the same rail, which adds per-tenant
  content-hash dedup (24h), rate limit + daily cap, and a durable `owner_notice` fallback.
  Flags: `SEND_MESSAGE_USER_DELIVERY` (ON), `USER_DELIVERY_{DEDUP_HOURS,RATE_PER_HOUR,DAILY_CAP}`.
- **Activation fixes (§6)** — headless/CLI containers register `database_manager` (x402
  payment-request store worked never on headless); metering-only usage tracker (records real
  api_cost_usd without a credit system — ends `NO BILLING`/`Spend: $0` while $68.89 burned);
  fail-closed gate: money-enabled autonomous runs refuse to start unmetered; provider-credit
  sentinel (`CREDIT_SENTINEL_ENABLED`, ON) — one notice + dispatch/LLM-cron pause + auto-release on
  402 credit death (was: 465× 402/day, zero signal).
- **Evidence-grounded completion review (§4.3)** — `GOAL_COMPLETION_JUDGE` now defaults ON and
  judges the CLAIM against the evidence pack (no acceptance prose required): unmet → failure with
  the gap; met → verified (earns ✅ + self-wake); unclear → done (unverified), excluded from the
  learning loops (no self-wake, no inline skill distillation from autonomous sessions).
- **Typed acceptance checks (§4.4)** — optional, framework-executed, fail-closed when present
  (`artifact_glob`, `http_ok`, `register_check_type` for instance verticals); producers:
  `goal_create.acceptance_checks`, `seed_goal --check`, planner prompt. NO create gate.
- **Communication contract (§3.3) + demoted notices (§3.4)** — autonomous sessions carry a
  cache-stable `<communication-contract>` prompt block; the completion push fires only when the
  agent said nothing during the run (✅ only when verified), the blocker-escalation push only when
  the agent didn't report the block (the durable ask is always created).
- **Durable goal stewardship (§5.1–5.4)** — cold-start sweep re-queues `running` goals on boot
  without a failure increment (two deploys mid-goal used to silently block it); per-attempt ledger
  in `payload.attempts` + previous-attempt block in retry prompts (retries are no longer amnesiac);
  `goal_show` exposes acceptance/outcome/attempts; new `goal_unblock` verb (rationale-logged);
  ancient blocked goals age out visibly (`GOAL_BLOCKED_MAX_AGE_DAYS`, 14); quota exhaustion pauses
  runs, not planning.
- §5.0 (goal-module extraction behind the three contracts) is deliberately deferred until the
  contracts are live-validated and the judge's LLM provisioning stops reaching into the agent.

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
