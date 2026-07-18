# AGENTS.md

This file is the canonical guidance for AI coding agents (Claude Code, Codex,
Cursor, and any tool that reads `AGENTS.md`) working in this repository. The
repo-root `CLAUDE.md` is a thin pointer that imports this file, so Claude Code
loads it automatically.

## ⚠️ Branching policy — WORK DIRECTLY ON `main`

**This repo is worked by many Claude Code sessions running in parallel. They ALL commit to
`main`. Do NOT create per-session feature branches or worktrees.** This instruction OVERRIDES
the harness default ("if on the default branch, branch first") and the superpowers
`using-git-worktrees` / `finishing-a-development-branch` skills.

Rules for every session:
- **Stay on `main`.** Do not `git checkout -b`, do not create a worktree, do not branch "for
  isolation." If you are already on `main`, commit there. If you find yourself on a feature
  branch, that is fine to finish, but default new work to `main`.
- **Commit as you deliver — don't wait to be asked.** As soon as a self-contained change is
  complete and its tests pass, commit it to `main`. Keep each commit small and focused so
  parallel sessions rebase cleanly. (This supersedes the old "commit only when the user asks"
  rule.)
- **⚠️ Commit with an explicit pathspec — NEVER `git add -A` / `git commit -a`.** Parallel
  sessions share ONE working tree and index, so a blanket add sweeps another session's in-flight
  files into your commit. Always `git commit -- <your files>` (or `git add -- <files>` first),
  and check `git diff --cached --name-only` before committing. See the shared-tree commit hazard
  in the auto-memory.
- **Pull before you push.** Because many sessions share `main`, always `git pull --rebase`
  before pushing and re-run the affected tests after a rebase. Resolve conflicts in your own
  changes; never discard another session's commits.
- No PRs/merge-branches for normal work — `main` is the integration branch.

> Why: the old "never touch main" rule was superseded on 2026-06-10 (see core/server-split
> memory). Per-session branches fragment parallel work and create merge backlog; a shared
> `main` with rebase-before-push keeps every session integrated.

## Project Overview

POLYROB is a sophisticated enterprise-grade AI automation platform with advanced automation capabilities, multi-agent coordination, and comprehensive service integrations. The system provides REST API endpoints (FastAPI), a terminal-native CLI (`rob`), and a real-time WebView interface (Socket.IO) for autonomous task execution, browser automation, and AI-driven workflows.

## Documentation map

| Doc | Role | Update policy |
|-----|------|---------------|
| **`AGENTS.md`** (this file) | Durable architecture, invariants, landmines, contracts | Keep present-tense ("how it works now"); don't add dated change-stories here |
| **`docs/CONFIGURATION.md`** | Env-flag SSOT (default + meaning + code anchor) | Update when a flag/default changes; trust the code anchor over prose. ⚠️ Adding/renaming a flag row REQUIRES `python scripts/gen_flags_catalog.py` (regenerates `core/flags_catalog.py`; the contract test in `tests/unit/core/test_flags.py` fails otherwise). Runtime view: `polyrob doctor --flags` |
| **`CHANGELOG.md`** | Dated, point-in-time change history | Append-only |
| **`README.md`** | Public-facing feature/setup overview | Keep accurate; avoid pinning exact model names |
| **`docs/guide/`** | User-facing guide (getting-started, cli, api, architecture, configuration, console, skills, self-hosting, deployment-postures, instances, migration, **payments** = full wallet/x402/invoicing/subscriptions/8004/credits/trading reference) | Keep accurate; this is the published documentation |
| **`docs/comparison.md`**, **`docs/examples.md`** | Public comparison + usage examples | Keep accurate; linked from README |
| **`docs/SKILL_AUTHORING_STANDARD.md`** | Skill-authoring conventions | Update when skill format/safety rules change |

When a default flips or a feature lands, update `docs/CONFIGURATION.md` + `CHANGELOG.md` — not the
prose in this file. This file should describe the current architecture, not narrate the diff.

## Core Architecture

The application follows a modular architecture with clear separation of concerns:

- **Core Framework** (`core/`): Dependency injection container, configuration management, component lifecycle, permission system, agent wallet, instance/identity (`core/instance.py`), the shared autonomy runtime (`core/autonomy_runtime.py`), and the cross-cutting config/policy surface (`core/config_policy/` — `AutonomyConfig`, the compute/autonomy posture ladders, `full_autonomy_enabled`/`_mode_capability_default`, the `_bool_env`/`_int_env` flag parsers). WS-1 (2026-07-16) relocated that cluster here from `agents/task/constants.py` to break the core↔`agents.task` import cycle; `agents/task/constants.py` re-exports every symbol for back-compat, but **new code imports from `core.config_policy`**. The layering ratchet (`tests/test_layering_ratchet.py`) forbids any `core/` import of `agents.task.constants` and only lets the remaining `core→agents.*` edges shrink. WS-2 added `core/tool_capabilities.py` — the ONE per-tool capability table (dimensions `money`/`high_impact`/`delegate_blocked`/`exec`/`readable_while_tainted`, plus the catalog permissions/risk tiers): `MONEY_TOOLS`, `DELEGATE_BLOCKED_TOOLS`, `HIGH_IMPACT_TOOL_IDS` and `VALID_TOOL_IDS` are all derivations, and `register_optional_tool` refuses an unclassified tool.
- **Modules** (`modules/`): Database management, LLM integration (multi-provider), and memory management
- **Agents** (`agents/`): The Task automation agent (the single front-door agent; chat routes through `TaskAgent.chat_once`) plus personality/prompt support
- **Tools** (`tools/`): External integrations (browser automation, document processing, social media APIs, email, blockchain, MCP, code-exec, cron/goal tools)
- **API** (`api/`): FastAPI HTTP endpoints for programmatic access — including the A2A protocol (`api/a2a/`) and the OpenAI-compatible `/v1` surface (`api/openai_compat/`)
- **CLI** (`cli/`): The terminal-native `rob` agent (`cli/rob.py`, `polyrob run`/`polyrob doctor`, REPL) — a first-class surface that runs the same Task agent
- **Surfaces** (`surfaces/`): Chat-surface adapters (e.g. `surfaces/telegram/`) implementing one inbound/outbound contract
- **WebView** (`webview/`): Real-time web interface with Socket.IO for session monitoring and interaction
- **Cron / Autonomy** (`cron/`): Durable scheduled agent runs + the goal board/dispatcher (see Scheduler/cron and Autonomy loops below)
- **Utils** (`utils/`): Cross-cutting concerns like rate limiting, message formatting, and telemetry

## Key Components

### Task System (`agents/task/`)
The advanced automation framework with:

**Core Components:**
- **SessionOrchestrator** (`agent/orchestrator.py`): Session lifecycle + agent creation. Squatter concerns are composed in via mixins under `agent/../session/`: `browser_pool.py` (BrowserPoolMixin), `multi_agent.py` (MultiAgentMixin), `feed.py` (FeedMixin), `workspace.py` (WorkspaceMixin), and `hooks.py` (SessionHooksMixin — fail-open session + subagent start/end lifecycle hooks: `register_session_start/end_hook`, `register_subagent_start/end_hook`)
- **Agent** (`agent/service.py`): Task execution loop (`step`/`_step_impl`), step-by-step automation. Composes mixins under `agent/core/`: `llm_runner.py` (LLMRunnerMixin — LLM invocation + provider fallback), `memory_writer.py` (MemoryWriterMixin — H-MEM writes + summaries)
- **MessageManager** (`agent/message_manager/service.py`): Message storage + retrieval. Composes mixins under `agent/messages/`: `token_counter.py` (TokenCounterMixin), `compactor.py` (CompactorMixin — context compaction incl. LLM compaction = synthesis), `persistence.py` (PersistenceMixin — checkpoint/disk), `filters.py` (FiltersMixin — sensitive-data scrub, tool-sequence repair)
- **ToolCallTracker** (`agent/tool_call_tracker.py`): Tool call ID lifecycle management - SINGLE source of truth. MCP schema-injection policy lives separately in `tools/mcp/validation_tracker.py` (MCPValidationTracker), delegated to
- **TaskAgent** (`agents/task_agent_lite.py`): Session manager that creates orchestrators. Active orchestrators are held behind **SessionRegistry** (`agent/../session_registry.py`); use `get_orchestrator`/`register_orchestrator`/`remove_orchestrator`, never reach the dict directly
- **Controller** (`tools/controller/service.py`): Action execution coordination. **Decomposed (UP-11,
  2026-06-16): 2369→387-line core.** The hot path + concern clusters are extracted to focused mixins that
  `Controller` composes via MRO — `tools/controller/execution.py` (`ExecutionMixin` — `multi_act`/`act` +
  retry/telemetry), `tool_management.py` (`ToolManagementMixin` — load/add/configure/remove/get/list),
  `introspection.py` (`IntrospectionMixin` — registry accessors + MCP prompt builders),
  `action_registration.py` (`ActionRegistrationMixin` — `send_message`/`done`/`load_skill`/`session_search`/
  `memory`/delegation closures + back-compat aliases), `_helpers.py` (`observe`/`ToolInfo`/
  `make_denylist_hook`/`build_load_skill_result`, re-exported by `service.py` so existing imports work).
  Two pre-existing concerns stay extracted (Item 7E/7H): `hooks.py` (`HookPipeline` — pre/post/transform
  fail-mode engine) and `mcp_registrar.py` (`MCPActionRegistrar`). `service.py` keeps `__init__`, the
  MCP/hook delegation shims, and `_ensure_normalize_path_exists`. NOTE: the mixin modules deliberately do
  **NOT** use `from __future__ import annotations` — it stringizes the action closures' first-param
  annotations, which the Registry introspects (`registry/service.py` `issubclass(first_anno, BaseModel)`)
  to route the validated param model.
- **Registry** (`tools/controller/registry/service.py`): Action registration and validation
- **Session Management** (`agent/session.py`): Session tracking and metadata
- **Path Management** (`path.py`): Centralized path manager for file operations

**Memory Flow (Native Tools):**
1. LLM creates response with brain state as JSON in text content (`{"current_state": {"memory": ..., "next_goal": ..., "reasoning": ..., "evaluation_previous_goal": ...}}`)
2. Agent preserves original content (NEVER synthesizes)
3. Brain state extracted from preserved content via `utils_json.extract_brain_state_from_json()` (called from `agent/core/next_action_internal.py`)
4. Memory flows to next step via `AgentBrain` in `add_state_message()`

**Tool Call Flow (Native Tools):**
1. LLM returns `response.tool_calls` + `response.content`
2. `ToolCallBuilder.normalize_tool_call()` - Format normalization (OpenAI → standard)
3. `ToolCallTracker.register_tool_calls()` - Track IDs (single source of truth)
4. `Registry.tool_calls_to_actions()` - Validate parameters using Pydantic
5. `Controller.multi_act()` - Execute actions
6. `MessageManager.add_tool_response()` - Add results
7. `ToolCallTracker.complete_step()` - Clear for next step

**Parsing Utilities:**
- `utils_json.extract_json_from_model_output()` - Parse JSON from text (fallback mode)
- `utils_json.normalize_action_schema()` - Normalize field names
- `utils_json.apply_action_field_corrections()` - Fix common mistakes (message→text, file_name→file_path)
- `tool_call_builder.normalize_tool_call()` - Format normalization (OpenAI→standard)
- `tool_call_builder.repair_and_normalize()` - Fix message sequences

**Responsibility Boundaries:**
- **ToolCallBuilder:** Format normalization ONLY (no field corrections)
- **utils_json:** Field corrections ONLY (no format handling)
- **ToolCallTracker:** ID tracking ONLY (MCP schema-injection policy → `tools/mcp/validation_tracker.py`)
- **MessageManager:** Message storage + retrieval. Token math, compaction (synthesis), persistence, and filters live in the `messages/` mixins, not inline
- **SessionRegistry:** the only seam to the active session→orchestrator map. Default in-process dict (`session_registry.py`). P6 adds a drop-in SQLite-backed variant (`agents/task/sqlite_session_registry.py`, WAL+jitter via `core/sqlite_util.py`) that mirrors session metadata cross-process (`exists`/`owner_pid`/`global_session_ids`) so `workers>1` can route instead of false-404; opt-in `SESSION_REGISTRY_BACKEND=sqlite`. The live orchestrator object still can't cross processes. **Item 6** closes the false-404 gap honestly: the API now calls `route()` via `api/session_routing.py` (`guard_remote`/`route_to_http`) so a `REMOTE` session returns an honest **409 + `owner_pid` + `Retry-After`** (not a false-404); `LOCAL`/`MISSING` are unchanged (resumable-from-DB paths still work). With `SESSION_REGISTRY_BACKEND=sqlite` **+ sticky load-balancer routing** (route a session to the worker that owns it), `workers>1` is safe for session affinity. True cross-worker method forwarding (IPC/shared serializable orchestrator) remains out of scope; `UVICORN_WORKERS=1` stays the default until you opt into sticky routing.
- **Registry:** Parameter validation ONLY (uses utils_json for corrections)

**Decomposition note (god-file split):** new behavior gets its own file/mixin rather
than growing `service.py`/`orchestrator.py`/`message_manager`. All four large classes —
`Agent`, `SessionOrchestrator`, `MessageManager`, and (UP-11, 2026-06-16) `Controller` —
compose focused mixins (see Core Components); the Controller split was the last
outstanding god-file (`tools/controller/service.py` 2369→387 lines). `_step_impl` is now split into phases in
`agent/core/step.py` (`_prepare_step` → `_call_llm` → `_validate_and_intervene` →
`_execute_actions` → `_process_action_results` → `_record_step` → `_finalize_step`), and
`Agent.__init__(self, config: AgentConfig, deps: AgentDeps)` has replaced the 31-param
constructor (use `Agent.from_params(**kwargs)` for the legacy kwarg form). The sync
`_create_llm_from_config` no longer inlines a per-call thread/fresh-loop hack — it delegates
to `core/async_bridge.py::run_coroutine_sync` (P4), one persistent background loop reused for
every sync-from-async call (kills thread churn + the httpx "loop closed" GC bug; live-verified
via `polyrob run`). The P4 tail is CLOSED as won't-fix (F-3e adjudication, 2026-07-17): removing
the wrapper by building the LLM in an async `initialize()` would move the LLM-dependent back half
of the Agent constructor (MessageManager SSOT build, native-tools reconciliation, aux
provisioning) behind an `await` — a high-risk lifecycle re-slice for purely cosmetic gain, since
the bug the bridge exists for is already fixed and the async twins
(`_create_llm_from_config_async`/`_provision_aux_llm_async`) already serve the async call sites.
The old `get_workspace_dir()` async shim was deleted (the sync `workspace_dir` property is the
one accessor).

**Key Principles:**
- Native tools preferred (OpenAI, Anthropic, Gemini support it)
- Preserve LLM content, NEVER synthesize
- Single source of truth per concern
- No temporary attributes - use ToolCallTracker
- Fail fast with clear errors - don't hide problems with 15 fallbacks

**Flow-efficiency mechanisms (runtime knobs):**
- **Prompt caching** — `cache_control: ephemeral` on the stable system prefix via
  `_build_cached_system_param` (`modules/llm/anthropic_client.py`); toggle `ANTHROPIC_PROMPT_CACHE`.
  **Anthropic-only** — see the 2026-06-14 review (`docs/KIMI_RUNTIME_AND_PROMPT_CONTEXT_REVIEW_2026-06.md`)
  for the provider-global caching gap; OpenAI now sets a stable `prompt_cache_key`
  (`openai_client._stable_prompt_cache_key`) to exploit server-side prefix caching. The dead
  `response_cache` decoy was removed from `llm_client.py`.
  **Caching breadth (UP-08, 2026-06-16):** `cache_hints.provider_cache_strategy()` is the per-provider
  policy seam, stamped onto `llm_client.cache_strategy` once at `create_chat_model`. Cached-token
  **metrics** now surface for DeepSeek (`prompt_cache_hit_tokens`), OpenRouter/NIM
  (`prompt_tokens_details.cached_tokens`) and Gemini (`cached_content_token_count`) → `cached_tokens` →
  billed at `cached_input_price` (read-only, safe-on). **Gemini explicit `cachedContents`** is opt-in
  (`GEMINI_PROMPT_CACHE`, default OFF; `GEMINI_CACHE_TTL_MIN` default 10): created once/session, busted
  on tool-set change, **deleted on `cleanup()`** (never orphan a billed object), fail-open, scoped to the
  non-Gemini-3 tools path. **OpenRouter tools-block breakpoint** rides `OPENROUTER_PROMPT_CACHE` (default
  OFF). NIM KV-reuse stays operator-side (`NIM_ENABLE_KV_CACHE_REUSE`, no client change). SDK note:
  `google-generativeai>=0.8.0` (deprecated; `caching.CachedContent` present).
- **Reasoning-token scrubber + thinking config** (UP-07, 2026-06-16) — `modules/llm/think_scrubber.py`
  (ported Reference `StreamingThinkScrubber`, pure) strips leaked `<think>`/`<thinking>`/`<reasoning>`/
  `<thought>`/`<REASONING_SCRATCHPAD>` blocks at the single content→`AIMessage` seam
  (`adapters._scrub_content`, all 3 sites) so reasoning prose never reaches history/brain-state/the user
  stream. Boundary-gated (prose mentioning `<think>` is kept), no-`<` fast path, str-only,
  `tool_calls`/usage untouched, runs AFTER Kimi recovery. Gated `THINK_SCRUBBER_ENABLED` (default **ON**,
  fail-open; OFF = byte-identical). The legacy `utils_json.py` regex strip stays for the fallback
  JSON-from-text path. **Thinking config:** `ModelCapabilities.thinking_budget_tokens`/`reasoning_effort`
  + `model_registry.get_thinking_config()` (the dead `EXTENDED_THINKING_MODELS` dict was migrated here and
  deleted). Per-provider consumption (Anthropic `thinking` block w/ `max_tokens>budget` + temp=1; DeepSeek
  `max_cot_tokens` from registry; OpenAI `reasoning_effort`) is gated `THINKING_CONFIG_ENABLED` (default
  **OFF** — enabling extended thinking is a real behavior change; the registry remains SSOT for budgets).
- **Conversational exit** (R1, 2026-06-14) — `agent/core/conversational_exit.py`: the run loop ends a
  turn after **2 consecutive reply-only steps** (every result a non-blocking `send_message`, tagged
  `metadata.conversational_reply`; no tool ran). Any productive step resets the run, so a real task is
  never cut short; sub-agents are exempt. Stops a chat/greeting reply from looping and re-greeting
  (`done()`/blocking-send still end via `is_done`). `cli/ui/rich_renderer.py` also suppresses a
  byte-identical repeat bubble within a turn (R2 backstop).
- **Per-model-family guidance** (P-1) — `_get_model_specific_instructions` (`prompts.py`) appends a
  terse operational note for Kimi/Gemini/GPT families (`MODEL_FAMILY_INSTRUCTIONS` in `constants.py`)
  alongside the existing Grok block. Anthropic/Claude gets none.
- **Tool-schema memoization** — `Registry.get_all_actions_for_provider` caches the generated schema
  list per `(provider, action-set, exclusions)` (`tools/controller/registry/service.py`); self-busts
  on any registration change. Stops regenerating ~3.7k of identical tool defs every step.
- **Skills as a user message** (PR13) — skills are pinned as a `SKILL`-origin foundation
  message (`MessageManager.set_skill_message` + `get_messages_for_llm`), NOT embedded in the
  system prompt (keeps it cache-stable). The system prompt is built once per session.
  **Progressive disclosure** (S-1, 2026-06-14, `SKILL_PROGRESSIVE_DISCLOSURE`, **default ON** —
  `agents/task/constants.py`; doc corrected UP-03): when on,
  only a compact `<skill-catalog>` (`SkillManager.format_skill_catalog`) is injected and the agent
  pulls a skill's full body on demand via the `load_skill(skill_id)` tool
  (`Controller.build_load_skill_result`, wired in `core/construction.py`). Off = legacy eager
  full-body injection, byte-identical.
- **Typed message origin** — injected control content (interventions, H-MEM, skills) carries a
  `MessageOrigin` + envelope (`modules/llm/messages.py` `make_control_message`), distinguishable
  from genuine user turns. Don't cram new system-injected content into a bare `HumanMessage`.
- **Bounded planning turn** — `ALLOWED_REASONING_TURNS` (default 1) lets the first tool-free
  response be a planning turn; the 3-empty thinking-loop escalation is the backstop. `=0` restores strict.
- **Compaction cooldown** — `COMPACTION_COOLDOWN_STEPS` (default 3) stops `llm_compact_history`
  (an extra LLM call) re-firing every step in the 85–95% band; ≥95% emergency prune still runs every step.
- **Memory-prefetch cadence** (Item 8) — `MEMORY_PREFETCH_CADENCE` (default `0` = prefetch on the
  first step only = legacy). `N>0` ALSO prefetches every N steps in `_maybe_prefetch_memory`. Inert
  until an external `MemoryProvider` is registered.
- **Intelligence finalization** (2026-06, all flag-gated OFF by default unless noted, fail-open) —
  closes the agent-intelligence gaps vs reference agents:
  - `REFLECTION_LLM_ENABLED` — H-MEM phase consolidation via the compaction aux model instead of
    `"; ".join` concat (`TaskContextManager._llm_consolidate`, wired in `construction.py`; reuses
    `_provision_compaction_llm`, blocks the loop thread ≤30s per reflection — infrequent).
    **UP-09 (2026-06-16): now default ON and the gate is REPAIRED.** It was permanently broken — the
    runtime guard read `BotConfig.get("REFLECTION_LLM_ENABLED", False)` (= `getattr`, no such attr → always
    `False`) while `construction.py` read `os.getenv`, so reflection never fired even when the env was set.
    Both sites now read one helper `constants.reflection_llm_enabled_default()` (default ON, falsey-disable
    `none/off/false/0`). Disable with `REFLECTION_LLM_ENABLED=off`; fail-open to concat unchanged.
    `event=reflection_consolidate`/`reflection_fallback` breadcrumbs make aux cost measurable. Sub-agents
    (`NullTaskContextManager`) never reflect.
  - `MEMORY_THREAT_SCAN` — opt-in prompt-injection scan (`modules/memory/task/threat_scan.py`) that
    rejects obviously-injected findings in `HierarchicalMemory.add_finding_to_phase`.
  - `AUX_MODEL_JUDGE` + `AUX_AUTO` (+ `AUX_PROVIDER`) — per-task aux-model router
    (`constants.resolve_aux_model` + `_provision_aux_llm(task)`); compaction still routes through it
    (`COMPACTION_MODEL`/`COMPACTION_AUTO_AUX`/`COMPACTION_PROVIDER` preserved). **UP-10 2.1 (2026-06-16):**
    the `judge` task is now **wired** into output validation — `_validate_output`
    (`agent/core/output_validation.py`) routes through a lazily-provisioned `self._judge_llm`
    (`construction.py`), fail-open to the main model; default-off (no model unless `AUX_MODEL_JUDGE`/
    `AUX_AUTO` resolves one). The dead `planner`/`vision` rows (no call sites) were **removed** from
    `_AUX_TASK_ENV`, so `AUX_MODEL_PLANNER`/`AUX_MODEL_VISION` no longer exist. Note (LOW-8): global
    `AUX_AUTO=true` also enables compaction auto-aux (union with `COMPACTION_AUTO_AUX`).
  - `MEMORY_BACKEND=sqlite` — concrete cross-session `MemoryProvider` (SQLite FTS5 keyword recall,
    `modules/memory/sqlite_memory_provider.py`), registered via `backend_factory` through the existing
    one-external-provider seam. **Default ON** (`MEMORY_BACKEND` defaults to `sqlite` on the
    server and to `local_vector` under `POLYROB_LOCAL` — `backend_factory.py`; an explicit
    `MEMORY_BACKEND` always wins); set `MEMORY_BACKEND=none/off/''` for
    `NullMemoryProvider`. Recall is **tenant-scoped** (FTS filter `AND user_id = ?`), and empty/anonymous
    `user_id` recall I/O is **refused** by default via `MEMORY_REQUIRE_USER_ID` (default true, UP-03) so
    default-on is multi-tenant-safe; set `MEMORY_REQUIRE_USER_ID=false` for single-user/local to restore
    the shared-`""` bucket. **UP-09 (2026-06-16):** the agent-callable `session_search` action is now
    **multi-shape** — `{query, limit (1-20), sort}` over a new `SqliteMemoryProvider.search()` (discover
    when `query` set, **browse most-recent** when empty); `prefetch` delegates, legacy shape unchanged.
    Routed via `memory_search` registry fn; tenant scoping is the provider's job (`_anon_blocked`), no new
    controller flag. Plus an **opt-in bounded `memory` tool** (`read/add/remove` over a curated per-tenant
    table in the same `memory.db`), gated `MEMORY_TOOL_ENABLED` (default false) AND external provider;
    per-tenant entry/char caps (`MEMORY_TOOL_MAX_ENTRIES`/`MEMORY_TOOL_MAX_CHARS`).
  - `BILLING_FAILOVER_ENABLED` — a billing/`insufficient_quota`/402 error attempts provider fallback
    before the permanent-halt in `error_recovery._handle_step_error` (Reference treats 402 ≈ credit-429).
  - `MESSAGE_STORE_BACKEND=sqlite` — additive durable mirror of the JSON message history
    (`agents/task/agent/messages/sqlite_persistence.py`); JSON stays the source of truth. **UP-10 2.3
    (2026-06-16): write-mirror ONLY** — `load_from_disk` reads only `message_history.json`, never the DB,
    so the mirror can silently diverge; enabling the flag now emits a one-time WARN making the write-only
    contract explicit. A real read path needs payload parity (`tool_call_id`+metadata) + reconciliation →
    deferred to a dedicated durable-store proposal.
  - Phase-0 bug sweep (historical): `load_from_disk` `subdir_name=` kwarg, flat-layout session
    rediscovery on restart, `MAX_FINDINGS_PER_PHASE` default 500→60 (forgetting now engages), and a
    latent `AgentError` import crash in `_handle_step_error` (now imports from
    `agents.task.agent.views`). **UP-10 2.2 (2026-06-16):** the dormant `RewindMixin` (zero non-test
    callers, a data-loss-shaped primitive with no deliberate trigger) was **deleted**; cheap to
    reintroduce under a dedicated recovery/checkpoint proposal if a real consumer appears.
- **Tool hook trio** (P2) — `Controller` exposes the full Reference plugin surface, all fail-open / no-op
  by default: `register_pre_tool_call_hook` (veto before execution; env `POLYROB_TOOL_DENYLIST`),
  `register_post_tool_call_hook` (observe the result — billing/metrics/audit), and
  `register_transform_tool_result_hook` (rewrite the `ActionResult`; hooks chain in order). Pre runs
  before `act()`; transform then post run on the result in `multi_act` before it's appended.
  Each `register_*` takes `fail_mode="open"|"closed"` (WS-B2): `open` (default, legacy) swallows a
  raising hook; `closed` makes a crashing guardrail DENY (pre) / error-result (transform) / propagate
  (post). All hook exceptions emit a `hook.error` log. The `POLYROB_TOOL_DENYLIST` guardrail is `closed`.
  The pre/post/transform lists + fail-mode engine were **extracted to
  `tools/controller/hooks.py::HookPipeline`** (Item 7E/7H); `Controller` keeps the same public
  `register_*`/`_run_*` surface as thin delegators (its `_*_hooks` attributes proxy into the pipeline).
  **Async pipeline (UP-04, 2026-06-16):** `HookPipeline.run_pre/run_transform/run_post` and the
  `Controller._run_*` delegators are now `async def` and `await`ed at the `multi_act` call sites; each
  hook is run through `_maybe_await` (sync hooks run unchanged; async hooks yield the loop cooperatively).
  Reentrant-safe — the runners hold no per-call state, so the `share_controller=True` sub-agent path
  (one shared pipeline, parallel `run_parallel_subtasks`) is fine.
- **Untrusted tool-result wrapping** (UP-06, 2026-06-16) — `agents/task/agent/core/untrusted_wrap.py`:
  string content from untrusted tools (registered `tool` in `{mcp, browser, perplexity, twitter,
  email}`, or `mcp_`/`browser_`/`web_` prefixes, or web/fetch names) is framed in
  `<untrusted_tool_result source="…">…</untrusted_tool_result>` delimiters at the single
  result→`ToolMessage` choke point (`result_processing.py::_pair_results_to_calls`, via a
  `source_for` resolver that reads `controller.get_action_details(name).tool`) so indirect
  prompt-injection in fetched content is read as DATA, not instructions. Paired with a static
  `<security>` system-prompt block (`prompts.py::_get_security_content`, cache-stable). Skips
  non-str/`<32`-char/already-wrapped content; wraps the string only (never mutates the
  `ActionResult`, so memory previews stay clean). Gated `UNTRUSTED_TOOL_RESULT_WRAP` (default
  **ON**; OFF = byte-identical legacy). Covers the **native** tool path (the default); the legacy
  non-native path is not wrapped.
- **Approval gating** (Item 7E) — `tools/controller/approval.py`: an `ApprovalProvider` ABC
  (`AutoApprover` default = allow, `DenyByDefaultApprover`) + `make_approval_hook` pre-hook factory.
  Wire via env `APPROVAL_REQUIRED_TOOLS` (comma list, default empty = no-op) + `APPROVAL_PROVIDER`
  (`auto`|`deny`|custom). Registered `fail_mode="closed"` so denial/timeout/error blocks the action.
  Mechanism only — no UI. ✅ **UP-04 (2026-06-16):** the hook is now `async` and `await`s the provider
  **directly** (no `run_coroutine_sync` bridge), bounded by `asyncio.wait_for(..., APPROVAL_TIMEOUT_SEC)`
  — a slow/interactive/network provider yields the loop instead of freezing it. Timeout **cancels**
  `provider.request`, so a real provider MUST be cancellation-safe (release held resources in
  `finally`/`except asyncio.CancelledError`). This unblocks an interactive provider in UP-12.
- **Code execution** (Item 3) — `tools/code_exec/`: an `ExecutionBackend` ABC + registry
  (mirrors `MemoryProviderRegistry`) with TWO backends: `LocalSubprocessBackend` (hard timeout via
  process-group kill, output cap, env allowlist that NEVER inherits `*_API_KEY`/secrets) and
  `DockerBackend` (`tools/code_exec/backends/docker.py` — hardened container: all caps dropped,
  no-new-privileges, read-only rootfs, workspace-only bind mount, PID/memory/CPU caps,
  network-deny-by-default; opt-in persistent per-session mode via `CODE_EXEC_DOCKER_PERSISTENT`,
  default OFF). Exposed as the `code_execution` tool's `run_code(language, code, stdin?, timeout?)`
  action. Gated `CODE_EXEC_ENABLED` (default **false**) + `CODE_EXEC_BACKEND` (`local_subprocess`
  default; `docker` for the hardened backend) + `CODE_EXEC_MAX_TIMEOUT_SEC` (30); never in default
  `tool_ids`. ⚠️ The `local_subprocess` backend is a convenience, **NOT a security sandbox** —
  single-user/local only; for multi-tenant prod use `CODE_EXEC_BACKEND=docker` (see
  `tools/code_exec/SANDBOX_SECURITY.md`) or keep `CODE_EXEC_ENABLED` OFF.
- **Coding tool** — `tools/coding/`: editor actions (`str_replace`/`apply_patch`/`run_tests`/grep) over
  the workspace. Gated `CODING_TOOLS_ENABLED` (default OFF; **ON** under `POLYROB_LOCAL` via the safe group),
  and blocked for delegated leaf children (in `DELEGATE_BLOCKED_TOOLS`).
- **Compute posture** (`AGENT_COMPUTE_POSTURE`, 0–3, default 0, computer-use parity) — a THIRD
  orthogonal capability axis (beside `POLYROB_LOCAL` trust and `AUTONOMY_POSTURE` loops; a FOURTH,
  `AUTONOMY_MODE`, governs capability/approval defaults rather than host access — see "Autonomy &
  continuous-learning loops" below): how much
  host/compute capability the agent has. Resolver + the single gate predicate live in
  `agents/task/constants.py` (`compute_posture()` frozen at import; `compute_posture_allows(ctx, N)`
  = posture≥N AND owner tenant AND not-leaf/sub-agent AND not a forged self-wake/delegation-result
  turn). Default (unset) is byte-identical to a plain server. Postures:
  - **0 `confined`** — today's ephemeral docker sandbox, no persistent shell.
  - **1 `sandbox-dev`** — an installable, stateful, HTTP-testable sandbox for an entitled session:
    `run_code`/`run_tests` run in **dev mode** (writable `/install` bind, `python -s` +
    `PYTHONPATH=/install` instead of the env-ignoring `python -I`, `HOME`/`PIP_TARGET` set) so
    `pip install --target=/install` imports; `run_code` gains `env`+`packages` (declarative,
    network-gated). A persistent **`shell`** tool (`tools/shell/` — `shell_run`, cwd/env persist
    across calls via a pure snapshot-replay model, foreground/background discipline) and a
    **`process`** job manager (`process_list/poll/log/kill`) run inside the session's ONE persistent
    dev container (shared via `tools/shell/backend_pool.py` — pids are per-container). The container
    publishes ports to host loopback (`CODE_EXEC_PUBLISH_PORTS`); a narrow allowlist
    (`tools/shell/loopback_allow.py`) lets the browser/`web_fetch` reach exactly those ports (never
    RFC1918/metadata) so the agent HTTP-tests its own server. Dev containers default to `bridge`
    network. `SHELL_TOOLS_ENABLED` defaults ON at posture≥1.
  - **2 `self-maintain`** — posture 1 + the **`self_env`** tool (`tools/self_env/`): distinct
    approvable verbs (never raw bash) — `install_dep` (own venv, pinned), `read_source`/`patch_source`
    (install-tree-confined, env/config hard-denied), `git_pull` (ff-only, ext:: rejected),
    `restart_service` (supervised only). Every verb is `compute_posture_allows(ctx,2)`- AND
    approval-gated (the Controller UNIONs `shell_run`+`self_env_*` into the gated set and defaults the
    provider to interactive at posture≥2), and emits a `self_modification` audit event.
    `SELF_ENV_ENABLED` defaults ON at posture≥2.
  - **3 `host`** — full host access; requires `POLYROB_LOCAL`, single-tenant box only
    (refused on network surfaces). Not yet wired as a distinct backend.
  Security: `AGENT_COMPUTE_POSTURE` + the approval flags are frozen at import; the env/config files
  that hold them are hard-denied to every agent-writable surface (`secret_guard`'s `is_credential_file`
  now catches `*.env`/`polyrob.env`, plus `is_protected_config_path` for `/etc/polyrob`).
  `shell`/`process`/`self_env` are in `DELEGATE_BLOCKED_TOOLS` and the correspondent-taint high-impact
  set (never a leaf/forged/correspondent turn), and never in the default `tool_ids`. Autonomous
  goal/cron runs are provisioned with the compute toolset (`code_execution`+`shell`+`coding`) only at
  posture≥1 (`goals/dispatcher.py::default_goal_tools`, `cron/runner.py::default_cron_tools`).
- **OAuth manager** (Item 4, library-only) — `tools/oauth/`: an `OAuthProvider` ABC + `OAuthManager`
  registry on the EXISTING Fernet store (`tools/mcp/security.py::MCPEncryption`), keyed by
  `(user_id, provider)`; `get_token` returns a cached valid token and auto-refreshes on expiry. One
  `GenericOAuth2Provider` (auth-code + refresh, config-dict driven, injectable HTTP for tests). No
  global flag and no tool is migrated yet — provider config comes from `config/.env.*` per provider.
- **Schema-error policy** (WS-B1) — `TOOL_SCHEMA_ERROR_POLICY` (default `DROP_TOOL`): an invalid
  native tool schema is excluded from the provider tools list rather than silently shipped
  (`RAISE`/`WARN` also available). `get_schema_generator` warns once on provider fallback (WS-B4).
- **Schema sanitizer** (UP-10 2.5, 2026-06-16) — `tools/controller/registry/schema_sanitizer.py`
  (ported from Reference) `sanitize_emitted_tools(tools, provider)` walks the FINAL emitted tools list and
  fixes known-hostile JSON-Schema constructs **before** they reach the provider: collapses nullable
  `anyOf`/`oneOf` unions (Anthropic/Kimi reject the null branch), strips `$ref`-sibling `default`
  (Fireworks/draft-07-strict), strips top-level combinators, normalizes bare-string / `[X,"null"]`
  array types, prunes dangling `required`. Format-aware over all four emitted shapes
  (OpenAI/Anthropic/Gemini/JSON-fallback) by shape detection. Wired once at
  `Registry.get_all_actions_for_provider` (before the schema cache write, downstream of `DROP_TOOL` so it
  turns "drop the whole tool" into "fix the field"), gated `TOOL_SCHEMA_SANITIZE` (default **on**;
  `=false` restores pre-port bytes), fail-open. The reactive `strip_pattern_and_format`/`strip_slash_enum`
  helpers are ported but **unwired** (no llama.cpp/xAI-Responses 400-recovery path in POLYROB).
- **MCP exec rate limit** (WS-B3) — `tools/mcp/rate_limit.py::MCPExecRateLimiter` guards
  `MCPTool.execute_tool` per `(user_id, server)`; `MCP_EXEC_RATE_PER_WINDOW` (20) /
  `MCP_EXEC_RATE_WINDOW_SEC` (60). Since F-1 (2026-07-17) it is a back-compat shim over the
  canonical rate-limit primitives in `core/rate_limit.py` (`SlidingWindowLimiter`/`TokenBucket`/
  `FixedWindowCounter`) — every in-process limiter (api middleware, user MCP admin, x402 public
  endpoints, webview throttles, `RateLimitManager`) is a configured instance of those, ratcheted
  by `tests/test_rate_limiter_ratchet.py`; the one documented exception is the telegram
  RetryAfter penalty tracker. MCP secrets are `${VAR}` placeholders in `config/mcp_config.json`
  (`MCP_GATEWAY_TOKEN`, `ANYSITE_JWT`), resolved by `tools/mcp/config.py` (WS-A2).
- **MCP resource subscriptions** (Item 7F) — `resources/subscribe`/`unsubscribe` are live:
  `MCPClient.subscribe_resource`/`unsubscribe_resource` (`tools/mcp/protocol.py`), incoming
  `notifications/resources/updated` is **processed** (not just logged) in the message loop
  (`_handle_notification`) and routed to `MCPServerManager.handle_resource_updated` →
  `tools/mcp/subscriptions.py::ResourceSubscriptionRegistry` (callbacks keyed by `(server, uri)`,
  fail-open dispatch; default callback invalidates cache + emits telemetry). Surfaced via the
  `subscribe_resource`/`unsubscribe_resource` MCP actions. Rides existing MCP enablement (no new flag).
  **Deferred (with rationale):** prompts & roots (low demand, additive later) and **sampling**
  (server-initiated LLM calls have billing + prompt-injection/security implications under
  multi-tenant — default-deny until a budget/consent gate exists).

**Agent delegation (roadmap P1):**
- **`delegate_task` tool** — the **single** Reference-style delegation verb: one focused `goal` (→
  `SubAgentManager.run_subtask`) XOR 2-5 parallel `tasks` (→ `run_parallel_subtasks`). Registered in
  `tools/controller/action_registration.py` (UP-11), gated by `TimeoutConfig.get_sub_agents_enabled()`
  (`SUB_AGENTS_ENABLED`). **ON by default** (C-DELEG, 2026-06) with conservative caps — depth=1
  (`MAX_SUB_AGENT_DEPTH`), max 3 concurrent (`MAX_CONCURRENT_SUB_AGENTS`), 600s/900s timeouts; set
  `SUB_AGENTS_ENABLED=false` to opt out.
  **Consolidation (UP-10 2.4, 2026-06-16):** all three verbs (`subtask`/`parallel_subtasks`/
  `delegate_task`) now route through ONE gated `_delegate` core that runs `evaluate_delegation`
  (role+depth) before dispatch — closing the bypass where a `leaf` could delegate via the legacy
  `subtask`/`parallel_subtasks` (which only did an inline enabled/depth check, no role concept).
  `subtask`/`parallel_subtasks` are kept as **deprecation-aliased shims**; the prompt teaches only
  `delegate_task`; the `multi_act` timeout selector now also routes `delegate_task` to the sub-agent
  timeout (was a latent bug — sync `delegate_task` got the wrong `default` timeout).
  **Durability (UP-12, 2026-06-16):** sync by default (blocks the turn); `background=true` (goal shape
  only) detaches the child and returns a `delegation_id` immediately, with the result re-entering the
  session as a NEW turn via `submit_user_message(kind="delegation_result")` →
  `AsyncDelegationRegistry` (`agents/task/agent/async_delegation.py`). Durable **across the turn** but
  NOT across a process restart (single-worker scope; for restart-durable scheduled work use cron, P5).
  Background slots are bounded by `MAX_ASYNC_SUB_AGENTS` (default 2, clamped ≤ `MAX_CONCURRENT_SUB_AGENTS`);
  at capacity the dispatch is **rejected**, not queued. Default-off → zero behaviour change.
- **Role/depth gate** — `tools/controller/delegation.py::evaluate_delegation` is the pure policy:
  a `leaf` agent cannot delegate; the main agent is `orchestrator`, sub-agents are spawned `leaf`;
  `MAX_SUB_AGENT_DEPTH` is the independent backstop. `role` rides on `AgentConfig.role` →
  `Agent._role` → `ActionExecutionContext.role`.
- **Least-privilege child toolset** (UP-05, 2026-06-16) — a delegated sub-agent gets a **dedicated
  child Controller** (not the shared parent one), built in `SubAgentManager._build_child_controller`.
  Its toolset is narrowed via two levers (since POLYROB registers tools two ways):
  `narrow_child_tools` drops **blocked container tool_ids** (`DELEGATE_BLOCKED_TOOLS` —
  derived from the per-tool capability table `core/tool_capabilities.py` as
  `ids_with("delegate_blocked")`, 15 ids incl. exec/self-mod/money/receivables tools;
  env-overridable via `DELEGATE_BLOCKED_TOOLS`); and the **delegation
  *actions*** (`subtask`/`parallel_subtasks`/`delegate_task` — NOT tool_ids; registered unconditionally
  in `_register_default_actions`) are suppressed on the leaf child via the Registry `exclude_actions`
  seam (`delegation_exclusions_for_child`). NOTE: `task` is the **TODO** tool, not delegation — never
  blocked. The child Controller is injected via `AgentDeps.controller` (→ `construction.py`:
  `injected_controller or self.orchestrator.controller`); per-Controller registry = no shared-cache
  poisoning. Heavyweight `browser`/`mcp` resolve to the SAME container singletons (no second browser
  context/MCP client); the child shares the parent orchestrator (same session_id/user_id), and its
  Controller is GC'd with the sub-agent (no shared-resource teardown). Gated `SUBAGENT_LEAST_PRIVILEGE`
  (default **ON**; OFF = shared parent controller, byte-identical to pre-UP-05).

**Scheduler / cron (roadmap P5, opt-in via `CRON_ENABLED`):** the `cron/` package — durable
scheduled agent runs (the home for work `delegate_task` can't do, since it's not durable).
- `cron/schedule.py` — pure schedule parser: duration (`30m`), `every monday 09:00`, 5-field
  cron, ISO one-shot. `cron/jobs.py` — SQLite `cron_jobs` store (WAL + jittered retry, the same
  pattern P6 will use for the session registry). `cron/service.py` — `CronService` (schedule/list/
  cancel). `cron/scheduler.py` — tick: file-`TickLock` (safe under `workers>1`), per-run **3-min
  hard cap**, one-shot→done / recurring→reschedule. `cron/runner.py` — `CronTicker` loop + the live
  `make_agent_runner` (runs jobs with `skip_memory=True`; live path, needs a real run to verify).
- **Wired into the API lifespan** via the shared autonomy runtime (`core/autonomy_runtime.py::start_autonomy`,
  see "Shared autonomy runtime" below), which `api/app.py` starts on startup and stops on shutdown.
  Off by default; no cron ticker runs unless `CRON_ENABLED=true`.
- Tool surface: `tools/cronjob_tools.py::CronJobTool` (`cronjob_schedule/list/cancel`). NOT in
  default `tool_ids`. **Registered (UP-02)** when `CRON_ENABLED=true` via
  `register_cronjob_tool()` (mirrors `register_code_exec_tool`; descriptor inserted into
  `TOOL_DESCRIPTORS` first, then `register_tool_class` — called from `tools/__init__.py`), so an
  agent that opts into `tool_ids=['cronjob']` can actually reach schedule/list/cancel. The tool's
  `CronService` shares the same `data_dir/cron.db` the lifespan ticker uses, so both see the same jobs.

**Autonomy & continuous-learning loops (2026-06-16, Fusion-validated `opus4.8-4.8`):** the four
Reference-parity "live and learn" loops POLYROB lacked — all behind **default-OFF, fail-open** flags via
`agents/task/constants.py::AutonomyConfig` (+ `_bool_env`), plan in
`docs/plans/autonomy-loops-FINALIZED-2026-06-16.md`. **Built on existing seams — no parallel
mechanisms.**
- **W3 cron run-loop FIX (live bug, default ON):** `cron/runner.py` historically called
  `create_session` (builds the orchestrator) and returned `bool(session_info)` — it **never ran
  `run_session`**, so every cron job created an idle session and did nothing. Now runs the loop
  (gated `CRON_RUN_LOOP`, default ON) and delivers the result out-of-band (`cron/delivery.py`:
  allowlist `{telegram,email,twitter}`, `[SILENT]` suppress, tenant-scoped recipient, fail-open,
  **inside the scheduler `wait_for` budget**). The recipient is resolved owner-scoped: **email** via a
  registered `user_directory` service else the `POLYROB_OWNER_EMAIL`/`BOT_OWNER_EMAIL` fallback
  (`core/instance.py::resolve_owner_email`), **telegram** via `resolve_owner_telegram_id` — so a
  single-owner headless deploy can deliver without a user store. `CronScheduleAction` gained **typed**
  `deliver`/`deliver_target` fields (it is `extra="forbid"`). Gated `CRON_DELIVERY_ENABLED` (default OFF).
- **W1 self-wake rail** (`agents/task/agent/core/self_wake.py`): re-enters an idle session as a forged
  turn. **Reuses UP-12's existing rail** (`orchestrator._deliver_async_delegation` →
  `submit_user_message(kind="delegation_result")` → HITL drain → `inject_user_guidance`) rather than a
  second queue — so the forged turn enters as a continuation user-guidance message (NOT a typed
  `SYSTEM_NOTE` control message; the **UP-06 untrusted-wrap on the text is the operative safety
  framing**, always applied). The genuinely-new pieces: `ReentryBudget` (per-session depth + idle-backoff
  cap UP-12 lacked → no ping-pong; also applied to UP-12's path) + `TaskAgent.deliver_self_wake(...)` —
  the public producer seam (resident-or-recreatable only, drop+audit if remote; forged text
  UP-06-wrapped; `kind="self_wake"`). Gated `SELF_WAKE_ENABLED`.
- **W2 writable skills + post-turn review fork:** `SkillManager` was read-only; `SkillWriterMixin`
  (`agents/task/agent/skill_writer.py`) adds `create/patch/delete/promote_pending` — tenant-confined
  (`user_{uid}/`), anon-blocked, `validate_skill_id` on EVERY write path (path-traversal guard),
  `validate_skill_content`→`is_suspicious` (fail-CLOSED on scan error)→atomic `os.replace`, `.pending/`
  quarantine, archive-never-delete. Active authored skills are written with `auto_activate:true` +
  derived keyword **triggers** so they are actually match-eligible (a `triggers`-less write was a dead
  write `get_skills_for_session` skipped). **A forged (self-wake/background) turn can NEVER auto-activate
  a skill, NEVER patch/delete an active one** — origin is detected via `execution_context.is_sub_agent`/
  `role=="leaf"` for a sub-agent/delegated worker, PLUS (SK-F10, closes a real gap) a **turn-kind stamp**
  for a self-wake or async-delegation-result re-entry into the MAIN agent, which otherwise looks identical
  to a genuine owner turn (`role="orchestrator"`, `is_sub_agent=False`). The stamp: `_drain_user_messages`
  (`agents/task/agent/core/user_ingress.py`) recomputes `orchestrator._forged_turn_kind` from every drained
  HITL batch's `kind` (forged if any message is `kind in ("self_wake", "delegation_result")`, cleared to
  `None` the moment a genuine-kind batch drains — recomputed per-drain, not "set once / clear at one call
  site", so it can't leak past the point a real message arrives on ANY producer path);
  `_build_execution_context` (`agents/task/agent/core/step_execution.py`) stamps it onto
  `execution_context.metadata["turn_kind"]`; `_is_forged_or_autonomous_turn`
  (`tools/controller/action_registration.py`) treats that turn_kind as forged alongside is_sub_agent/leaf/
  autonomous. So a sub-agent/leaf/forged-turn author → `created_by=background_review` → always `.pending/`;
  a normal main-agent turn follows `SKILLS_WRITABLE_REQUIRE_REVIEW` (default true). Agent-callable via
  `skill_manage` action (in `action_registration.py` — **no `from __future__`**, the registry-closure
  landmine), gated `SKILLS_WRITABLE`. `BackgroundReviewMixin`
  (`agent/core/background_review.py`, composed into `Agent`) forks a cheap aux-model reviewer every
  `BG_REVIEW_INTERVAL` productive turns at the run-loop turn boundary (non-blocking, leaf, fail-open),
  gated `BACKGROUND_REVIEW_ENABLED`. Provenance/reuse in `modules/skills/skill_usage.py`
  (`data/skill_usage.db`), `bump_load()` from the `load_skill` **closure** (has `execution_context`),
  not the pure `_helpers.build_load_skill_result`.
- **W4 durable goal board** (`agents/task/goals/board.py`, `data/goals.db` WAL+jitter via
  `core/sqlite_util`): cross-session backlog with **atomic CAS `claim`** (safe under `workers>1`),
  **circuit breaker** (`consecutive_failures>=max_retries → blocked`), tenant-scoped `AND user_id=?`.
  `dispatcher.py` (`GoalDispatcher`/`GoalTicker`) reuses `cron.scheduler.TickLock`, runs goals via
  `create_session`+`run_session` (`task=` kwarg), **feeds W1** (`deliver_self_wake`). Lifespan-wired
  via the shared autonomy runtime (`start_autonomy`/`autonomy_handles.stop()` in `api/app.py`,
  alongside cron + curator). Agent tool `tools/goal_tools.py::GoalTool` (`goal_create/list/show/cancel`),
  `register_goal_tool` mirrors `register_cronjob_tool`. Gated `GOALS_ENABLED`.
- **W5 curator** (`agent/core/curator.py`): Phase 1 (no LLM) stales→archives unused authored skills by
  age + reactivates on reuse (system/`user`-authored never touched). Phase 2 (LLM-merge) was a
  logged no-op with no concrete merge policy and was **removed** (`CURATOR_LLM_MERGE` no longer
  exists, 2026-06-29) — Phase 1 carries all the value today; re-add Phase 2 under its own flag if a
  real merge policy lands. Lifespan ticker gated `CURATOR_ENABLED`, interval-gated
  `CURATOR_INTERVAL_HOURS`.
- **W6 cross-session search** (already built): verified `session_search` (`action_registration.py:275`),
  added `memory_search` alias (gated `MEMORY_SEARCH_TOOL`, default ON) + UP-06-wrap on recalled content.
- **W7 trimmed tail:** `insights` read-only action (authored-skill reuse %, gated `INSIGHTS_TOOL`).
  Async/background delegation was **already shipped** (UP-12) and `AUX_MODEL_PLANNER/VISION` **already
  removed** (UP-10) — those predecessor-plan items were dropped. Cron suggestions + webhook triggers
  remain deferred (additive).
- **Wake change-gate:** a cron job with
  `payload.change_gated` skips the paid model call when the tenant's observable
  state fingerprint (goal board/events + other cron runs + newest episode,
  `cron/wake_gate.py`) is unchanged since the last tick — a $0 tick emitted as
  `cron_run skipped/no_change`, same shape as `wake_agent=false`. Baseline per
  job in `cron.db::wake_gate`, advanced on run AND skip. Delivery jobs
  (`payload.deliver`) are never gated; missing tables read neutral; any other
  fingerprint error fails open (the tick runs). Gated `WAKE_CHANGE_GATE`
  (default OFF; ON under `AUTONOMY_POSTURE=full`).
- **Restart-durable autonomy state:**
  `agents/task/agent/autonomy_state.py` (`autonomy_state.db`, WAL+jitter, in
  `core/db_manifest.py`), gated `AUTONOMY_STATE_DURABLE` (default ON, fail-open
  to the legacy volatile registries). Background delegations write dispatched/
  terminal rows (per-session id counter seeded past persisted history); a
  cold-start sweep in `core/autonomy_runtime.py` marks rows still `running` as
  `interrupted` and surfaces them to their session via the self-wake rail —
  honest recovery, NEVER a silent evaporation or a resume. The self-wake
  `ReentryBudget` hydrates persisted per-session depth (wall-clock timestamps,
  7-day staleness purge) so a wake storm can't reset itself by crashing.
  ⚠️ Tests: `tests/conftest.py` forces `AUTONOMY_STATE_DURABLE=off` per-test so
  unit runs never write the developer's real data home.
- **Continuity trio in posture:** `EPISODIC_MEMORY_ENABLED`,
  `EPISODIC_DIGEST_INJECT` (safe-local OR posture default) and
  `REFLECTION_ON_SESSION_CLOSE` (posture-only, via
  `AutonomyConfig.reflection_on_session_close`) are members of
  `_POSTURE_OWNER_VISIBLE_FLAGS` — the server is no longer continuity-dark when
  the operator asked for owner-visible autonomy.
- **`AUTONOMY_MODE` — the fourth axis, capability/approval master switch** (proposal 013):
  `supervised` (default, byte-identical) or `autonomous`, effective only on a genuinely
  single-owner deployment (`POLYROB_LOCAL` + a bound owner principal — `full_autonomy_enabled()`;
  otherwise it clamps back to `supervised` with a one-time WARN). `autonomous` moves the
  *defaults* of a fixed capability-flag group ON (`TWITTER_ENABLED`/`MCP_ENABLED`/
  `GROUP_CHAT_ENABLED`/`EMAIL_SURFACE_ENABLED`/`X402_INVOICE_ENABLED` receive-side/
  `MESSAGE_AUTONOMOUS_ALLOWLISTED`/`CORRESPONDENT_ACCESS_ENABLED`/`CORRESPONDENT_REPLY_ENABLED`),
  widens the autonomous goal/planner toolset (`AUTONOMOUS_MODE_TOOLS`), flips approvals to
  allow+audit+notify (`auto_notify`, with self-modification verbs and payment-SPEND verbs always
  staying on the durable `owner_queue` lane), replaces the outbound per-address ACL with a
  policy+cap ladder (`OUTBOUND_POLICY=open` + `OUTBOUND_DAILY_SEND_CAP`), and raises
  `AUTONOMY_POSTURE`'s own default to `full` so the loop axis needs no separate flip. It never
  moves money-SPEND, host access (`AGENT_COMPUTE_POSTURE`), or secrets — those keep their own
  gates under both modes; an explicit per-flag env always wins over the mode default.
  `agents/task/constants.py::autonomy_mode`/`full_autonomy_enabled`/`_mode_capability_default`;
  full flag table in `docs/CONFIGURATION.md`.

**Terminal-native consolidation (2026-06-17, Fusion `opus4.8-4.8`-reviewed; plan
`docs/plans/2026-06-17-terminal-native-consolidation.md`):** closes the "built but not wired" gap so
`rob` (the terminal-native agent) actually runs what the server runs. Three structural moves, all
additive/seam-level:
- **`POLYROB_LOCAL` local profile** (`agents/task/constants.py` `local_mode_enabled()` +
  `_SAFE_LOCAL_FLAGS`): for the single-user CLI, the *safe* autonomy flags
  (`SELF_WAKE`/`SKILLS_WRITABLE`/`SELF_CONTEXT_WRITABLE`/`BACKGROUND_REVIEW`/`GOALS`/`CURATOR`/
  `INSIGHTS`/`CODING_TOOLS_ENABLED`/`SKILL_CATALOG_INCLUDE_ALL`/`KB_ENABLED`/`KB_AUTO_PREFETCH`/
  `CONTEXT_REFERENCES_ENABLED`/`PROJECT_CONTEXT_AUTOLOAD`) default **ON as a
  group** instead of OFF — without touching multi-tenant server defaults (server never sets
  `POLYROB_LOCAL`). An explicit per-flag value still wins (only the default moves). `CODE_EXEC_ENABLED` and
  sub-agent caps are deliberately excluded. Set by `build_cli_container` (`os.environ.setdefault`).
- **Local vector RAG in the task agent** (`MEMORY_BACKEND=local_vector`): `LocalVectorMemoryProvider`
  (`modules/memory/local_vector_memory_provider.py`) does hybrid keyword+vector recall over a compact
  local sqlite-vec store in `memory.db` — Pinecone/Chroma were retired. `local_vector` is the
  **default under `POLYROB_LOCAL`**; elsewhere the default stays `sqlite` (FTS5 keyword recall).
  Only the default moves — an explicit `MEMORY_BACKEND` always wins. Recall is **tenant-scoped,
  not session-scoped** (filters by `user_id` only) so it is genuinely **cross-session**.
  `local_vector` needs apsw/sqlite-vec loadable and an embedder; if either is missing it degrades
  to FTS5 keyword recall with a loud warning (`backend_factory.py`).
- **Shared autonomy runtime** (`core/autonomy_runtime.py::start_autonomy` + `AutonomyHandles.stop()`):
  the cron/goal/curator tickers were API-lifespan-only, so `rob` never ran them. Now extracted to one
  fail-open, independently-gated runtime that BOTH `api/app.py` (lifespan) and `cli/commands/chat.py`
  (REPL, under `local_mode_enabled()`) start and stop. `stop()` is bounded
  (`asyncio.wait_for(_STOP_GRACE_SEC)` then force-cancel) so a long cron tick can't hang shutdown. The
  P6 session-registry reaper block is untouched. `register_cli_tools` registers the `cronjob`/`goal`
  tools when their flags are on, AND the REPL now **adds them to the session's loaded tool_ids** under
  local mode (registering a container service alone doesn't make a tool callable — Fusion-validation fix),
  so the agent can actually schedule goals/cron from chat. `cli_unavailable_tools` gives honest feedback
  when a requested tool isn't CLI-available. CLI provider/model resolution is consolidated into one
  key-aware `cli/config_store.py::resolve_provider_model` (auto-detects the provider whose API key is
  present instead of hard-defaulting to `gemini`); `/memory` shows the active memory provider.
- **Interactive idle-gate** (`core/interactive_gate.py`): because the REPL's interactive agent and the
  background goal/cron executors share ONE working directory (CWD), running an autonomous file-mutating
  goal *concurrently* with a live turn could corrupt files. The REPL marks itself busy for each turn
  (`interactive_turn()` around `convo.respond`), and `GoalDispatcher.dispatch_once` + `cron.scheduler.tick`
  **skip a tick while busy** (queued work runs on the next idle tick). Inert on the server (nothing marks
  busy there → zero impact). This is what makes "create a goal mid-chat, it runs when you pause" safe.

**Instance / identity / evolving-self (polyrob foundation, 2026-06-19, SHIPPED on `main`):** the
groundwork for "rob = one bot INSTANCE on the polyrob FRAMEWORK." All default-inert (instance_id
defaults `"rob"`; live behaviour byte-equivalent until an operator authors identity docs or binds
an owner). The framework rename + two-axis DB keying are NOT done (deferred by design) — see the
plan handoff `docs/plans/2026-06-21-polyrob-analyze-and-implement-HANDOFF.md`.
- `core/instance.py` — `resolve_instance_id` (default `"rob"`, env `POLYROB_INSTANCE_ID`/
  `BOT_INSTANCE_ID`), `resolve_owner_principal`/`is_owner`, `load_self_context` (SOUL — operator
  authored, frozen), `load_self_doc` (SELF — agent-writable, load-side scan guard + cap),
  `self_tier_root` (`identity/{instance_id}/user_{uid}/`), `is_safe_tenant_id`.
- `core/self_context_writer.py` (`SelfContextWriter` — `.pending` quarantine + atomic replace) +
  the `self_context_manage` agent action (`tools/controller/action_registration.py`, gated
  `SELF_CONTEXT_WRITABLE`, in the `POLYROB_LOCAL` safe group). `core/pairing.py` + `core/surfaces/
  dispatcher.py` — owner-allowlist ingress gate (fail-open, default off).
- SOUL + SELF are pinned as a frozen `SELF_CONTEXT` foundation message at session start
  (`agents/task/agent/core/construction.py`; `MessageOrigin.SELF_CONTEXT`).

**Project-context file (C9)** — `agents/task/agent/core/project_context.py`: on the local CLI (gated
`PROJECT_CONTEXT_AUTOLOAD`, default ON under `POLYROB_LOCAL`), the agent auto-loads a per-repo context
file and pins it as a frozen `PROJECT_CONTEXT` foundation message, so the running agent knows what
project it's operating in without the operator repeating it every session. Walks CWD up to the git
root and recognises five names **by precedence, not concatenated** — the highest-precedence name that
exists anywhere on the walk wins, most-local occurrence: `polyrob.md` (native, targets POLYROB without
touching other agents' files) > `POLYROB.md` > `AGENTS.md` (vendor-neutral, Codex/Cursor interop) >
`CLAUDE.md` (Claude Code interop) > `.cursorrules` (legacy). A repo with both `AGENTS.md` and
`CLAUDE.md` loads only `AGENTS.md`. Content is threat-scanned (`is_suspicious`, fail-open if the
scanner is unavailable, fail-CLOSED if it raises), secret-path-filtered, and capped to
`PROJECT_CONTEXT_MAX_TOKENS` (default 20000). Local = trusted/steering (returned unchanged); server
opt-in (`PROJECT_CONTEXT_SERVER_MODE`, default OFF, NOT flipped by `POLYROB_LOCAL`) searches only the
tenant session workspace (never the process CWD/install dir) and wraps the content
`<untrusted_tool_result>`-framed as DATA, not instructions. See `docs/CONFIGURATION.md` for the full
flag table.

### Browser Automation (`tools/browser/`)
- Playwright-based web automation with anti-detection features
- Managed browser contexts and sessions
- DOM manipulation and interaction capabilities

### MCP Integration (`tools/mcp/`)
- **Model Context Protocol** support for external service integration
- Single unified tool providing access to multiple MCP servers (AnySite, filesystem, search, etc.)
- Auto-discovery of server capabilities and tools
- Agent accesses ALL configured MCP servers through one tool interface
- Configuration in `config/mcp_config.json`
- Must be explicitly loaded: `tool_ids=['mcp', 'browser', ...]`
- NOT in default tool list - agents must request it
- See `tools/mcp/README.md` for detailed architecture

### LLM Integration (`modules/llm/`)
- Multi-provider support: OpenAI (GPT-5.x, o-series), Anthropic (Claude), Google (Gemini 3), DeepSeek, OpenRouter (grok/glm/qwen/etc.), NVIDIA NIM (see `modules/llm/model_registry.py` for the live model set)
- Automatic failover between providers
- Token counting and usage tracking
- Adapter pattern for consistent interfaces
- **Native LLM layer (no third-party agent framework).** The agent loop, message types
  (`modules/llm/messages.py`), per-provider adapters (`modules/llm/adapters.py` — `BaseChatModel` is
  POLYROB's own ABC), tool-calling, prompt-caching and token-counting are all native. The factory is
  `modules/llm/llm_factory.py::create_chat_model` (builds native adapters only; raises on an unknown
  provider — no silent provider fallback). LLMManager entry points: `get_chat_model` /
  `get_fallback_chat_model`. (The 2026-06 native migration removed LangChain and the Llama
  provider; the LLM layer is fully native.)
- **Provider base_url from profiles.** `AnthropicClient` now sources its `base_url` from the
  Anthropic `ProviderProfile` (`modules/llm/anthropic_client.py::_profile_base_url` → `None` = SDK
  default), mirroring `OpenRouterClient` — so a self-hosted gateway/proxy is configured once in
  `modules/llm/profiles.py`.

### Memory System (`modules/memory/`)
- **Local-first cross-session memory, default ON.** Pinecone/Chroma are retired. The backend is
  selected by `MEMORY_BACKEND` (`modules/memory/backend_factory.py`): `sqlite` (**server default**
  — FTS5 keyword recall, `SqliteMemoryProvider`), `local_vector` (**default under `POLYROB_LOCAL`**
  — hybrid keyword+vector via sqlite-vec, `LocalVectorMemoryProvider`; degrades to FTS5 keyword
  recall with a loud warning if apsw/sqlite-vec or the embedder is unavailable), or
  `none`/`off`/`''` (`NullMemoryProvider`). Recall is **tenant-scoped** and refused for
  empty/anonymous `user_id` by default (`MEMORY_REQUIRE_USER_ID`, default true).
- Context management with automatic summarization; conversation continuity across sessions;
  hierarchical memory organization.
- **Pluggable provider seam** — `provider.py::MemoryProvider` (ABC: lifecycle + stateless
  `get_tool_schemas`/`system_prompt_block` + stateful `prefetch`/`sync_turn` + optional hooks) and
  `MemoryProviderRegistry` enforcing the **one-external-provider** limit. The step loop calls the
  prefetch seam live — `Agent._maybe_prefetch_memory()` (`agent/core/memory_prefetch.py`) runs in
  `agent/core/step.py`, routing the query through `modules.memory.registry` and injecting recall as
  a `MEMORY`-origin message. Cadence via `MEMORY_PREFETCH_CADENCE` (default `0` = first-step-only).
  With `MEMORY_BACKEND=none` the registry holds `NullMemoryProvider` and no recall is injected.

### API & WebView
- **API** (`api/`): FastAPI HTTP endpoints for creating sessions, sending messages, retrieving status
- **WebView** (`webview/`): Real-time monitoring interface with Socket.IO for live session observation
- Both services run independently and can be deployed separately
- WebView provides visual feedback for automation tasks and browser interactions
- API enables programmatic integration with external systems

### A2A Protocol (`api/a2a/`)
Google's Agent-to-Agent protocol for AI agent interoperability:
- **Agent Card** (`/.well-known/agent.json`): Discovery endpoint with capabilities and auth options
- **JSON-RPC** (`/a2a/rpc`): Main protocol endpoint for task operations
- **Streaming** (`/a2a/message/stream`): SSE streaming for real-time updates
- **Authentication**: Three methods supported:
  - **API Key** (recommended): `X-API-KEY: rob_xxx...` - create via `/api/auth/api-keys`
  - **x402**: Pay-per-request with crypto signatures
  - **Bearer JWT**: From wallet SIWE authentication
- Implementation lives in `api/a2a/` (`agent_card.py`, `endpoints.py`, `streaming.py`, `task_handler.py`)

### OpenAI-compatible API (`api/openai_compat/`)
A drop-in OpenAI-style surface so existing OpenAI SDK clients can talk to POLYROB, gated by
`OPENAI_COMPAT_API_ENABLED` (default OFF). Mounted in `api/app.py` behind the flag.
- `POST /v1/chat/completions` — non-streaming chat over `TaskAgent.chat_once` (`router.py`)
- `GET /v1/models` — model listing
- An OpenAI model string (e.g. `gpt-4o`) is mapped to a POLYROB `(provider, model)` pair via
  `api/openai_compat/model_map.py`; request/response shapes in `models.py`.

### CLI surface (`cli/`)
The terminal-native `rob` agent runs the same Task agent as the API. Entry `cli/rob.py`;
commands under `cli/commands/` (`polyrob run`, `polyrob doctor`, the chat REPL with `/autonomy`,
`/memory`, etc.). Container built by `build_cli_container`; under `POLYROB_LOCAL` the safe autonomy
flags default ON (see "Terminal-native consolidation"). Provider/model auto-resolves from
whichever API key is present (`cli/config_store.py::resolve_provider_model`).

### Chat-surface access model (WS-A) + Email surface (WS-B)
**Three-tier inbound access** for multi-user surfaces, gated by the single flag
`CORRESPONDENT_ACCESS_ENABLED` (default OFF → byte-identical legacy routing). Resolved once
at the routing boundary (`core/surfaces/dispatcher.py::route_inbound` → `access.py::
resolve_access_tier`):
- **OWNER** — bound owner principal / paired user / the local operator → steers the agent.
  The single-user local-owner bypass (`is_owner(local=True)`) is **surface-scoped** to
  `{cli,local,repl}` (`access.py::_LOCAL_OWNER_SURFACES`) so a forgeable network sender
  (email/telegram) is NEVER auto-owned.
- **CORRESPONDENT** — a third party the agent INITIATED contact with (an ACTIVE row in the
  `core/surfaces/correspondents.py` registry, the SOLE routing authority, PK
  `(surface,address,thread_id,user_id)`). Its reply is **DATA**, delivered ONLY to the
  originating session as a `MessageOrigin.CORRESPONDENT` control message
  (`<correspondent-message>` envelope + inner `wrap_untrusted`), via
  `orchestrator.inject_correspondent_message` / `TaskAgent.deliver_correspondent_data` — NOT
  the user "obey" queue. A correspondent can never reach COMMAND/STEER/TASK_AGENT.
- **DENIED** — unknown / unverified / group (single-principal envelope → groups denied in v1).

Invariants: tier = **authenticated sender** (never thread membership); the dispatcher tier
block is **fail-CLOSED** once the model is on; the **capability gate**
(`agents/task/agent/core/correspondent_gate.py`, registered fail-closed in `construction.py`)
denies high-impact tools (money/comms/code-exec/delegation/browser) while a session is
correspondent-tainted (taint is lock-guarded + **source-tracked** — the orchestrator records
each tainting `(surface, address)`; set on inject, cleared on a genuine owner turn). An
opt-in scoped exemption (`CORRESPONDENT_REPLY_ENABLED`, default OFF) lets a tainted session
`message`/`send_email` EXACTLY the tainting party — 1:1 only, rounds-capped per 24h,
fail-closed — for autonomous multi-round exchanges.
**Owner-by-email is OFF in v1** (forgeable `From:`) — all email senders are correspondent or
denied. Auto-seed is approval-gated (`CORRESPONDENT_REQUIRE_APPROVAL`, default ON) + per-day
capped (NEW addresses only; re-seeds exempt) and runs **before** the send — a cap-refusal
blocks the outbound rather than orphaning the reply; a new pending binding emits a
`correspondent_pending` event and shows in `polyrob owner pending`. **Every** outbound path
seeds: the generic guardrail lives in `core/surfaces/seed.py`
and the proactive `message` tool seeds via `perform_message_send`.

**Conversation continuity (2026-07-13):** the durable per-correspondent container is
`core/surfaces/conversations.py::ConversationStore` (one row per
`(tenant, surface, address)`, bounded message log, registered by `install_surface_bus` as
`conversation_store`). Every send/reply is recorded; injected replies carry a prepended
context block (inside the untrusted frame); the read-only `contact_history` action shows the
per-address transcript or (address omitted) the who-replied listing. Email threading is
real: `EmailTool.send_email_ex` mints/returns the Message-ID and sets
`In-Reply-To`/`References` (from the conversation's `last_inbound_mid`); the surface binds
each outbound Message-ID to its SENDING session via registry **thread-anchor rows**
(`provenance='thread'`, state mirrors the base row, exempt from the cap), so multiple
sessions can converse with one address. Address-only resolution routes same-tenant
ambiguity to the most recent binding (`CORRESPONDENT_RESOLVE_LATEST`, default ON);
cross-tenant ambiguity always denies. A reply to a DEAD session is not dropped: it
**resumes** into a fresh session with the conversation context, re-pointing registry +
store (`CONVERSATION_RESUME_ENABLED`, default ON; `correspondent_resumed` event). Delivery
is wake-correct (ephemerals count as pending input), crash-safe (unconsumed ephemerals
persist), bounded (`MAX_EPHEMERAL_MESSAGES`), and per-chat serialized (KeyedLock in the
shared inbound handler). Idle bindings can expire via `CORRESPONDENT_TTL_DAYS` (default
0 = never).

**Email surface** (`surfaces/email/`,
`EMAIL_SURFACE_ENABLED`): IMAP poll (Message-ID/surrogate dedup, marks `\Seen`,
quoted-history truncation) inbound + buffered SMTP outbound, inheriting the base `Surface`
engine; run via `polyrob email`. Admin: `polyrob owner {show,correspondents,approve,invite}`
(`approve --all [<surface>]` bulk-approves pending correspondents).

### Payment & Billing (`modules/credits/`, `modules/x402/`)
- **Credit System**: Pre-purchased credits, deducted per LLM call
- **x402 Payments**: Pay-per-request cryptocurrency payments using USDC on Base/Ethereum
  - Uses [fastapi-x402](https://github.com/jordo1138/fastapi-x402) library
  - Coinbase facilitator for on-chain verification and settlement
  - Testnet (base-sepolia) uses free x402.org facilitator
  - Mainnet requires CDP credentials from https://portal.cdp.coinbase.com/
- **Fail-Fast Billing**: `InsufficientCreditsError` stops execution when credits depleted
- **Billing Failures**: Tracked in `billing_failures` table for admin reconciliation
- **Treasury**: Configurable via `X402_PAYMENT_RECIPIENT` env var
- See `modules/x402/README.md` for full x402 documentation
- **Agent financial agency (built-in ecommerce)** — the agent can quote, invoice, get
  paid, meter, and account for itself. All new behavior is behind default-OFF flags;
  a deployment that enables none of them is byte-identical to a plain server. Crypto is
  the only rail today (Stripe/fiat is a designed-for, deferred extension); a no-payments
  deploy degrades gracefully (invoice tool absent, not broken).
  - **Two ledgers, never summed** (`modules/credits/unified_ledger.py::build_ledger`) —
    agent finances stay separate from platform billing (`modules/credits`); the unified
    ledger only *joins* the two, read-only, into distinct blocks. `ledger["treasury"]`
    is the agent's own money (USDC): `income_usd`/`spend_usd`/`pending_usd`/
    `pending_count`/`balance_usd`/`net_usd`/`available`, where `net_usd = income − spend`
    and runtime/API cost never enters it. `ledger["runtime"]` is the owner's money
    (compute cost): `spend_window_usd`/`spend_total_usd`/`calls_window`/`calls_total`/
    `provider_balance_usd`/`available` — it has **no** `net` (there is nothing to net
    compute cost against). The two blocks are never summed into one figure; the legacy
    top-level `total_spend_usd`/`net_usd`/`earned_usd` fields are gone with **no**
    deprecation alias (deliberate — a surviving alias would let a straggler consumer
    keep silently reading the merge; deletion makes it fail loudly instead). Both
    `balance_usd` fields are display-only and opt-in (`include_balances=True` —
    `build_ledger` is also called from hot non-display paths, e.g. `core/recap.py`, so
    the network-read balance probes must never fire by default); unknown is `None`,
    never `$0.00`. Terminology is income/spend — "earned" is retired. There is no
    autonomy budget gate: the former rate-ceiling (`AUTONOMY_BUDGET_USD` et al.) gated
    the merged figure and could not protect a finite balance, so it was removed; every
    wallet-spend gate (`WALLET_DAILY_CAP_USD`, venue caps, `PAYMENT_APPROVAL_MODE`,
    `metering_gate`, correspondent-taint, x402 invoice caps) is unaffected and still
    live. The credit-death sentinel (`core/credit_sentinel.py`) is the backstop: ONE
    trip site in `agents/task/agent/core/error_recovery.py` covers interactive, cron,
    and goal runs alike (`looks_like_credit_death`, gated to the LLM exception family).
  - **Receivables (invoicing)** — gated `X402_INVOICE_ENABLED` (default OFF): the
    `x402_invoice` tool (`x402_request`/`x402_invoices`/`accounting`,
    `tools/x402/invoice_tool.py`; leaf-delegation-blocked) creates *pending*
    `x402_payment_requests` rows (`modules/x402/invoicing.py`, `metadata.kind=
    agent_invoice` + originating `session_id`; ceiling `X402_INVOICE_MAX_USD`,
    per-tenant daily cap `X402_INVOICE_DAILY_MAX`). The counterparty is carried two
    ways: a free-form `payer_contact` string (rendered "billed to") and a typed
    `correspondent_ref {surface,address,thread_id}` (routing). Tenant scoping uses
    `json_extract(metadata,'$.tenant_id')`, never `metadata LIKE`.
  - **Invoice as image + delivery** — gated `INVOICE_CARD_ENABLED` (ON under
    `POLYROB_LOCAL`): `modules/pfp/cards.py::render_invoice_card` composes a branded
    PNG (Mindprint face + amount + purpose + QR + "billed to") in pure Pillow (never
    Chromium), with a shipped OFL font under `assets/fonts/`; the QR payload
    (`modules/x402/artifact.py`, `INVOICE_QR_STYLE`: `address` default | `eip681`)
    prefers `eip681` when on-chain detection is on. Rendering is fail-open to
    text-only. The outbound-media leg (`OutboundMessage.media` +
    `SurfaceCapabilities.media_out`) delivers the card as a Telegram photo / email
    attachment / `message(media_paths=…)` (workspace-confined, symlink-guarded).
  - **Approval** — `PAYMENT_APPROVAL_MODE` (`approve` default | `auto`): `approve`
    routes every outward payment request through the durable, remotely-approvable
    `owner_queue` `ApprovalProvider` (`tools/controller/approval_queue.py` — reuses the
    goal-board asks store; Telegram `tap-` `/approve` verbs; one-shot post-timeout
    grant); `auto` auto-approves *within the caps* and notifies the owner after the
    fact. Correspondent-tainted / forged / leaf turns can never reach a money verb.
  - **Settlement** — no longer attestation-only. A pending invoice completes by owner
    attestation (`polyrob owner settle <id>`), a payer-driven facilitator `POST
    /api/x402/requests/{id}/pay`, OR — gated `X402_SETTLE_ONCHAIN_DETECT` (mainnet +
    treasury) — **facilitator-free on-chain USDC detection**: the settlement watcher
    (`modules/x402/settlement_watcher.py`, autonomy-runtime ticker) scans treasury
    `Transfer` logs (`modules/x402/onchain_probe.py`, `settlement_scan` checkpoint),
    exact-atomic oldest-first match, `transaction_hash` partial-unique-index +
    `claim_for_settlement` CAS against double-settle, `payment_unmatched` owner notice
    on no-match. Amount-jitter (`X402_INVOICE_AMOUNT_JITTER`, forced on with detection)
    keeps same-amount invoices distinguishable, disclosed at full precision on every
    payer-facing surface. On settlement the watcher re-enters the originating session
    via `deliver_self_wake` (correspondent-linked → DATA rail); on expiry it escalates
    to the session + a one-off owner notice. Events: `payment_requested`/`_settled`/
    `_expired`/`_unmatched`.
  - **Watchtower subscriptions** — gated `SUBSCRIPTIONS_ENABLED` (default OFF):
    prepaid periods + renewal invoices driven from the same watcher tick
    (`modules/x402/subscriptions.py` — atomic `apply_settlement` with a
    `subscription_applied_settlements` PK ledger + typed `SettlementResult`, idempotent
    and CancelledError-safe; partial-unique-index against duplicate renewals). A
    `cron/runner.py` job gates on subscription status (`subscription_lapsed` $0 skip);
    `polyrob owner sub list/cancel`. (Inert until a `create_subscription` caller is
    wired.)
  - **Metering → invoice bridge** — gated `USAGE_INVOICE_BRIDGE_ENABLED` (default OFF):
    a tenant-scoped `usage_rollup` (`modules/credits/usage_rollup.py`) + `usage_summary`
    read action drafts an invoice payload from measured `usage_records` cost — a
    *suggestion* the agent must still fire through the approval-gated `x402_request`;
    the bridge never mints a payment request itself.
  - **Payment-backed reputation (ERC-8004)** — gated `EIP8004_PAYMENT_FEEDBACK`
    (rides `EIP8004_ENABLED`, default OFF): on settlement of a correspondent-linked
    invoice, the agent offers a `ProofOfPayment`-backed feedback *authorization* (never
    auto-submits). `submit_feedback` verifies the proof against the settled invoice
    (exists + `toAddress`==treasury + txHash-only replay guard + `agent_id` bound to the
    EIP-712-signed `feedback_auth.agentId`). ERC-8004 is the trust layer, not a payment
    rail; the `ReputationManager` is a local simulation, not yet on-chain.
  - **Machine payer** — the x402 HTTP middleware (`modules/x402/middleware.py`) charges
    the A2A / OpenAI-compat surfaces per *billed* route (exact `(method,path)` gating,
    not prefix); the shared `build_x402_challenge` produces one challenge shape.

## Common Commands

### Development
```bash
# Run the agent locally (terminal-native)
polyrob run "your task"     # one-shot
polyrob                     # chat REPL

# Run the API server locally (FastAPI; this is what `python main.py` starts —
# it does NOT run the terminal agent)
python main.py

# Run all tests
pytest tests/

# Run specific test file
pytest tests/integration/test_task_conversation.py

# Run tests with coverage
pytest --cov=agents --cov=modules --cov=tools tests/

# Run tests with verbose output
pytest -v tests/

# Run tests matching a pattern
pytest -k "test_task" tests/
```

### Database Operations
```bash
# Initialize/migrate database (the canonical semver migration runner)
python -m migrations.migrate upgrade   # apply pending migrations
python -m migrations.migrate status    # show current schema version

# Access database (development)
sqlite3 data/database/bot.db

# Check database schema
sqlite3 data/database/bot.db ".schema"
```

### Browser
```bash
# Install Playwright browsers
python -m playwright install --with-deps chromium
```
(The old Xvfb scripts are legacy — the headless prod shape doesn't use a virtual
display; Playwright runs headless natively.)

### Deployment

> **Rob #1 prod shape (verified live):** the Hetzner VPS runs `polyrob.service`
> (headless agent = `polyrob telegram`, tree `/opt/polyrob`, env
> `/etc/polyrob/polyrob.env`, data `POLYROB_DATA_DIR=/var/lib/polyrob`),
> `polyrob-webview.service` (monitoring console, loopback :5050 behind nginx TLS) and
> `polyrob-email.service` (email surface). There is NO
> `polyrob-api.service`/`polyrob-webgate.service` on that box — that api+webgate shape
> is the OSS self-hosting posture (`docs/guide/self-hosting.md`), not prod.

```bash
# On the VPS (preferred), from the maintenance clone:
cd ~/rob_dev && bash scripts/deploy_prod.sh

# From a dev machine (HOST/KEY via ~/.polyrob/ops.env — no targets in the repo):
bash scripts/deploy_from_local.sh

# Webview console only:
bash scripts/deploy_webview.sh
```

Both deploy scripts share one safety envelope: clean tree via `git archive` → tar backup
of live code → rsync code dirs → DB migration (idempotent) → subprocess import-test
BEFORE restart → restart → verify → auto-rollback on failure. See `DEPLOYMENT.md` for
the full runbook.

**IMPORTANT: What NOT to do**
- ❌ NEVER run `deploy_unified.sh` — retired (targeted the dead api+webgate shape; it
  now exits 1). Legacy body: `deployment/legacy/deploy_unified.sh` (do not run).
- ❌ NEVER single-file scp CODE onto prod (2026-07-01 ImportError outage) — deploy a
  consistent tree via the scripts.
- ❌ NEVER rsync the working tree (other sessions' uncommitted files) — the scripts use
  `git archive HEAD` for exactly this reason.
- ❌ NEVER make direct edits on the server.
- ❌ NEVER deploy without the tar backup the scripts take for you.

## Environment Configuration

The project uses environment-specific configuration files:
- `config/.env.development` - Development environment
- `config/.env.production` - Production environment

Essential environment variables:
```bash
# LLM Providers (at least one required)
OPENAI_API_KEY=your_openai_key
ANTHROPIC_API_KEY=your_anthropic_key
GEMINI_API_KEY=your_gemini_key

# External Services (optional)
PERPLEXITY_API_KEY=your_perplexity_key  # For enhanced search
TWITTER_API_KEY=your_twitter_key  # For social media integration

# MCP Configuration (optional)
MCP_ENABLED=true  # Enable/disable entire MCP system
```

**Local vs Server Environments:**
- Local development uses `requirements.txt` and `.env` in the project root
- Server deployments use `requirements.txt` and `config/.env.{development|production}`

## Important Patterns

### Session Management
All Task sessions use a centralized SessionManager with consistent ID formatting. Session IDs are always cleaned using `pm().clean_session_id()` to ensure consistency.

### Path Management
The `agents.task.path.pm()` singleton manages all file system paths for sessions, ensuring consistent directory structures and preventing path-related issues.

**Best Practices:**
- Always use `pm()` for path operations
- Never construct paths manually with string concatenation
- Always clean session IDs with `pm().clean_session_id()`
- Use standard subdirectories: `workspace`, `feed`, `screenshots`, `logs`, `data`

**Directory Structure:**
```
data/auto/
└── {user_id}/
    └── sessions/
        └── {session_id}/
            ├── workspace/   # For files created by agent
            ├── feed/        # For message feed
            ├── screenshots/ # For saved screenshots
            ├── logs/        # For log files
            └── data/        # For structured data
                ├── telemetry/   # For telemetry events
                ├── history/     # For browser history
                └── llm_usage/   # For LLM usage tracking
```

### Agent Coordination
The SessionOrchestrator coordinates multiple agents within a session, managing their lifecycle and shared resources like browser instances.

### Tool Registration
Tools are registered with the orchestrator and made available to the controller, which registers appropriate actions based on tool capabilities.

### Error Handling
Comprehensive error handling with automatic recovery mechanisms. The system uses circuit breakers for external services and implements retry logic with exponential backoff.

## Testing Approach

- Unit tests for individual components (agents, tools, modules)
- Integration tests for end-to-end workflows
- Use pytest fixtures for test isolation
- Mock external services for deterministic testing

Test locations:
- `tests/` - Main test directory
- `agents/task/agent/message_manager/tests.py` - Message manager tests
- Component-specific test files throughout codebase

**Note:** Many test files in `tests/` have been deleted (see git status) - integration tests may need to be recreated

## Debugging & Analysis Guidelines

When investigating issues in the codebase:

**Key Principles:**
- Read code as written, not as documented
- Test assumptions with evidence - don't trust comments over implementation
- Follow data through transformations end-to-end
- Check all branches and edge cases
- Look for patterns across multiple issues

**Common Issue Patterns:**
- **State Issues:** Multiple sources of truth, stale data reused, state reset without cleanup
- **Logic Issues:** Conditional branches that never execute, missing error handling
- **Data Flow:** Variables overwritten unexpectedly, transformations not applied consistently
- **Architecture:** Circular dependencies, tight coupling, single responsibility violations

**Red Flags:**
- Data loss (state overwritten without backup)
- Silent failures (exceptions caught but not logged)
- Memory leaks (unbounded list growth, cached data never cleared)
- Type mismatches (null/None not handled, wrong types passed)

## Code Style Guidelines

- Follow PEP 8 Python style guide
- Use type hints for function signatures
- Document complex logic with docstrings
- Prefer composition over inheritance
- Use dependency injection for testability
- Implement proper async/await patterns

## Security Considerations

- Never commit API keys or secrets
- Use environment variables for sensitive configuration
- Implement role-based access control (Super Admin > Admin > Moderator > User)
- Validate and sanitize all user inputs
- Use secure communication (SSL/TLS) for all external services

## Performance Optimization

- Use async operations for I/O-bound tasks
- Implement multi-level caching strategies
- Connection pooling for database operations
- Rate limiting for API calls
- Efficient memory management with proper cleanup

## Deployment Notes

### Server Environments

**Production Server (Hetzner Cloud)**
- Provider: Hetzner Cloud (CPX31, 8 GB RAM, Helsinki `hel1`), Ubuntu 24.04 LTS
- Access: SSH key-only; targets/keys live in `~/.polyrob/ops.env` (gitignored), never in
  the repo
- Code: `/opt/polyrob` · Env: `/etc/polyrob/polyrob.env` · Data: `/var/lib/polyrob`
- Units: `polyrob.service` (agent), `polyrob-webview.service` (console),
  `polyrob-email.service` (email surface)
- History: migrated from AWS EC2 on 2026-05-17

### Deployment Process

`scripts/deploy_prod.sh` (on-box) / `scripts/deploy_from_local.sh` (remote-driven) — one
safety envelope: `git archive` clean tree → tar backup → rsync code dirs → DB migration
(idempotent) → subprocess import-test BEFORE restart → restart → verify (active +
autonomy loops + telegram online) → auto-rollback from the backup on failure; stamps
`/opt/polyrob/.deployed_sha`. `scripts/deploy_webview.sh` covers the console. Full
runbook: `DEPLOYMENT.md`.

### Service Management
```bash
# Check service status
sudo systemctl status polyrob.service polyrob-webview.service polyrob-email.service nginx

# View logs
sudo journalctl -u polyrob.service -f

# Restart (the deploy scripts restart for you)
sudo systemctl restart polyrob.service
```

### Deployment Best Practices

1. **Test locally before deploying** (`pytest tests/` + the deploy's import-test is the
   last line, not the first)
2. **Deploy consistent trees via the scripts** — never single-file scp, never
   working-tree rsync
3. **Let the scripts back up and verify** — they tar the live code and auto-roll-back
4. **Monitor logs after deployment**
5. **Check `.deployed_sha` vs `git rev-parse HEAD`** to detect drift

## Troubleshooting

### Common Issues

**Import Failures (deploy aborts before restart):**
- The deploy import-tests in a subprocess; on failure it restores the backup and exits —
  the running service was never touched. Reproduce with the IMPORT_OK one-liner in
  `DEPLOYMENT.md`.

**Service Failures:**
- Check logs: `sudo journalctl -u polyrob.service -n 100`
- Verify env file loads: `set -a && . /etc/polyrob/polyrob.env && set +a`
- Check venv: `/opt/polyrob/venv/bin/python -c "import core"`

**SSL Certificate Issues:**
- `sudo certbot certificates` / `sudo certbot renew`, then
  `sudo systemctl reload nginx`
