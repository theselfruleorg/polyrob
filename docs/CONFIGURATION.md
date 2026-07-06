# POLYROB Configuration Reference (env flags SSOT)

This is the single source of truth for POLYROB's environment/config flags. Every default
below was **read from source** (the `os.getenv`/`_bool_env` default or the `BotConfig`
`Field(default=...)`), not from prose, and verified against the code on **2026-06-22**.
When editing docs or reasoning about a default, **cite the code anchor and re-check it
before trusting the value** — lines rot, so anchors are `file.py:line` but trust the
*group/symbol* over the exact number. Boolean defaults follow POLYROB's falsey-set
semantics: a flag read via `_bool_env(name, default)` (`agents/task/constants.py:194`)
returns `default` when unset/blank, else `True` unless the value is one of
`{none, off, false, 0, no}`.

---

## LLM / providers

| Flag | Default | What it does | Code anchor |
|---|---|---|---|
| `LLM_MAX_OUTPUT_TOKENS` | `16384` | Hard cap on generated output tokens per call. | `modules/llm/llm_client.py:108,120` |
| `LLM_PROMPT_CACHE` / `ANTHROPIC_PROMPT_CACHE` | ON | Global prompt-caching kill-switch (either set falsey disables; default ON). | `modules/llm/cache_hints.py:25-34` |
| `GEMINI_PROMPT_CACHE` | OFF | Opt-in Gemini explicit `cachedContents` (billed, TTL'd object). | `modules/llm/cache_hints.py:93` |
| `GEMINI_CACHE_TTL_MIN` | `10` | TTL (minutes) for the Gemini explicit cache object. | `agents/task/.../*` (`os.getenv("GEMINI_CACHE_TTL_MIN","10")`) |
| `OPENROUTER_PROMPT_CACHE` | OFF | Adds Anthropic-style `cache_control` breakpoints on the OpenRouter tools block. | `modules/llm/cache_hints.py:112,150` |
| `THINKING_CONFIG_ENABLED` | OFF | Enables per-provider extended-thinking/reasoning-effort config (real behavior change). | `modules/llm/model_registry.py:1368` |
| `THINK_SCRUBBER_ENABLED` | ON (`"1"`) | Strips leaked `<think>`/`<reasoning>` blocks at the content→AIMessage seam. | `agents/task/.../*` (`os.getenv("THINK_SCRUBBER_ENABLED","1")`) |
| `STREAM_BRAIN_SCRUB` | ON (`"true"`) | Scrubs brain-state JSON from the streamed user-facing buffer. | `agents/task/agent/hitl_manager.py:18` |
| `TASK_MAX_INPUT_TOKENS` | unset | Caps input-token budget for the task agent (shared-client clobber guard). | `os.getenv("TASK_MAX_INPUT_TOKENS")` |
| `COMPACTION_MODEL` | unset | Explicit aux model for context compaction. | `agents/task/constants.py:142` |
| `COMPACTION_AUX_MODEL` | `''` | Legacy alias / aux compaction model string. | `agents/task/constants.py:560` |
| `COMPACTION_PROVIDER` | unset | Provider override for the compaction aux model. | `os.getenv("COMPACTION_PROVIDER")` |
| `COMPACTION_AUTO_AUX` | OFF | Auto-route compaction to the provider cheap-map model. | `agents/task/constants.py:159` |
| `AUX_MODEL_JUDGE` | unset | Explicit aux model for the output-validation judge task. | `agents/task/constants.py:143` |
| `AUX_AUTO` | OFF | Globally enable per-task aux-model auto-routing (union with `COMPACTION_AUTO_AUX`). | `agents/task/constants.py:157` |
| `AUX_PROVIDER` | unset | Provider for aux-model resolution. | `os.getenv("AUX_PROVIDER")` |
| `AUX_MODEL_COMPACTION` / `AUX_MODEL_JUDGE` / `AUX_MODEL_REFLECTION` | unset | B5: per-slot primary aux model, takes precedence over the legacy `COMPACTION_MODEL`/`AUX_MODEL_JUDGE` envs (note `AUX_MODEL_JUDGE` is both the legacy *and* new-slot env for judge — same string, no conflict). | `agents/task/constants.py::resolve_aux_chain` |
| `AUX_PROVIDER_COMPACTION` / `AUX_PROVIDER_JUDGE` / `AUX_PROVIDER_REFLECTION` | unset | B5: per-slot primary provider override, takes precedence over legacy `COMPACTION_PROVIDER`/`AUX_PROVIDER`. | `agents/task/constants.py::resolve_aux_chain` |
| `AUX_FALLBACK_COMPACTION` / `AUX_FALLBACK_JUDGE` / `AUX_FALLBACK_REFLECTION` | unset | B5: comma-separated ordered fallback candidates for the slot, each `provider/model` (or a bare `model`, which auto-detects its provider). `_provision_aux_llm` walks primary→fallbacks and uses the first candidate that builds; if all fail, falls back to the main model (unchanged fail-open contract). Hermes `auxiliary.<task>.fallback_chain` parity. | `agents/task/agent/core/llm_provisioning.py::_provision_aux_llm` |
| *(reflection inheritance)* | — | If `AUX_MODEL_REFLECTION` is unset, reflection inherits compaction's resolved model **and** provider as a pair (`AUX_MODEL_COMPACTION`/`AUX_PROVIDER_COMPACTION`, or the legacy `COMPACTION_MODEL`/`COMPACTION_PROVIDER`), plus `AUX_FALLBACK_COMPACTION` if `AUX_FALLBACK_REFLECTION` is also unset — back-compat for reflection historically reusing the compaction aux model wholesale. If reflection sets its own model, none of compaction's config (including its provider) is consulted. | `agents/task/constants.py::resolve_aux_chain` |
| `REFLECTION_LLM_ENABLED` | **ON** | H-MEM phase consolidation via aux LLM instead of string concat. Disable with `off/false/0/no/none/''`. | `agents/task/constants.py:165-179` |
| `NATIVE_TOOLS_DEBUG` | `''` (off) | Verbose native-tool-call debug logging. | `os.environ.get("NATIVE_TOOLS_DEBUG","")` |

The aux-model slot set is intentionally fixed at 3 (`compaction`/`judge`/`reflection`) —
the real aux LLM call sites in the codebase. UP-10 removed a dead `planner`/`vision` slot
pair that had zero call sites; extend the slot set only when a new aux-consuming feature
actually lands, not speculatively.

Per-provider **API keys / base URLs** are `BotConfig` fields (default `None`/SDK
default): `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY`,
`DEEPSEEK_API_URL` (`https://api.deepseek.com/v1`), `OPENROUTER_API_KEY`,
`NVIDIA_API_KEY`, `NVIDIA_API_URL` (`https://integrate.api.nvidia.com/v1`),
`PERPLEXITY_API_KEY` — see `core/config.py:54-65`.

**Default provider preference** (2026-06-24). When no provider is given explicitly
(`-p`/`SessionRequest.provider`) or pinned (`DEFAULT_PROVIDER`/`CHAT_PROVIDER`), the
default is the first provider **with a key present**, in `modules/llm/profiles.py`
`PROFILES` order: **`openrouter` → `anthropic` → `openai` → `gemini` → `nvidia` →
`deepseek`**. OpenRouter is first, so a host with an `OPENROUTER_API_KEY` defaults to
OpenRouter even when other provider keys are also present. To force a different
default, set `DEFAULT_PROVIDER=<provider>` or pass `-p`.

**Per-provider default-model override** — `POLYROB_<PROVIDER>_MODEL` (e.g.
`POLYROB_OPENROUTER_MODEL=x-ai/grok-4.3`; unset by default) overrides the hardcoded
`DEFAULT_MODELS` entry for that provider, so a headless deploy can pin/swap the model
with an env change + restart, no code change — see
`modules/llm/llm_client_registry.py::get_default_model`.

---

## Memory

| Flag | Default | What it does | Code anchor |
|---|---|---|---|
| `MEMORY_BACKEND` | **`sqlite`** (**`local_vector` under `POLYROB_LOCAL`**) | Cross-session memory provider: `sqlite` (FTS5), `local_vector` (hybrid kw+vec), `none/off/''` disables. | `modules/memory/backend_factory.py:40` |
| `MEMORY_REQUIRE_USER_ID` | **ON** (`True`) | Refuse empty/anonymous-user recall I/O (multi-tenant safety). Set false for single-user shared-`""` bucket. | `modules/memory/sqlite_memory_provider.py:34` |
| `MEMORY_VECTOR_MAX_DISTANCE` | `0.6` | Max vector distance for `local_vector` recall hits. | `modules/memory/local_vector_memory_provider.py:47` |
| `MEMORY_TOOL_ENABLED` | OFF | Opt-in bounded `memory` read/add/remove tool (also needs an external provider). | `tools/controller/action_registration.py:393` |
| `MEMORY_TOOL_MAX_ENTRIES` | `50` | Per-tenant entry cap for the `memory` tool. | `modules/memory/sqlite_memory_provider.py:204` |
| `MEMORY_TOOL_MAX_CHARS` | `2000` | Per-tenant char cap for the `memory` tool. | `modules/memory/sqlite_memory_provider.py:208` |
| `MEMORY_PREFETCH_CADENCE` | `0` (**`3` under `POLYROB_LOCAL`**) | `0` = prefetch on first step only; `N>0` = also every N steps (inert without external provider). Resolved at access time, not import, so it sees `POLYROB_LOCAL` even if set later via `os.environ.setdefault`. | `agents/task/constants.py::memory_prefetch_cadence` |
| `HMEM_SEMANTIC` | `auto` | H-MEM cross-phase recall mode: `auto` (embeddings if an embedder exists, else lexical) / `embeddings` / `lexical` / `off`. | `modules/memory/task/task_context_manager.py:_get_semantic_retriever` |
| `HMEM_TAIL_PLACEMENT` | OFF (**ON under POLYROB_LOCAL**) | Place in-session H-MEM as a dynamic suffix AFTER the conversation instead of in the foundation prefix, so the stable foundation + growing conversation form a cacheable prompt-cache prefix (only the small H-MEM tail is reprocessed each step). Off = legacy foundation placement (byte-identical on the server until soaked). | `agents/task/constants.py::hmem_tail_placement` |
| `MEMORY_THREAT_SCAN` | OFF | Prompt-injection scan rejecting injected findings before H-MEM write. | `modules/memory/task/hierarchical_memory.py:907` |
| `MEMORY_SEARCH_TOOL` | **ON** | Read-only cross-session `memory_search`/`session_search` tool (tenant-scoped). | `agents/task/constants.py:358` |
| `MEMORY_STORE_ANSWER_ONLY` | OFF (**ON under POLYROB_LOCAL**) | Store the distilled ANSWER (not the echoed "User: {q}\nAssistant: {a}" transcript) as the FTS-matched/embedded recall content, so a recall query restating the question doesn't rank the question text as highly as the answer. | `modules/memory/sqlite_memory_provider.py::SqliteMemoryProvider._store_answer_only` |
| `MESSAGE_STORE_BACKEND` | `''` (off) | `sqlite` = additive write-only durable mirror of JSON message history (JSON stays SSOT). | `agents/task/.../sqlite_persistence` (`os.getenv("MESSAGE_STORE_BACKEND","")`) |
| `MAX_MEMORY_CACHE_SIZE` | `30` | In-memory history cache size. | `agents/task/constants.py:58` |
| `MEMORY_CLEANUP_INTERVAL` | `50` | Clean memory every N operations. | `agents/task/constants.py:64` |

---

## Skills

| Flag | Default | What it does | Code anchor |
|---|---|---|---|
| `SKILL_PROGRESSIVE_DISCLOSURE` | **ON** | Inject only a compact `<skill-catalog>`; full body pulled via `load_skill`. Off = legacy eager full-body. Resolved by `skill_progressive_disclosure()` — an **access-time function** (not an import-bound constant), so an env override always wins even if set after this module first imports (Task 1 rename). | `agents/task/constants.py::skill_progressive_disclosure` |
| `SKILL_CATALOG_INCLUDE_ALL` | **ON everywhere** | Include all (auto-activatable) skills in the `<skill-catalog>` regardless of trigger match, so a paraphrased task can still discover+`load_skill` a skill it didn't trigger-match. Defaults ON unconditionally (not just under `POLYROB_LOCAL`) since the old OFF-by-default module constant was a dead decoy and was removed; `=false` restores trigger-matched-only. Note (FL-D9): this flag was previously also listed in `_SAFE_LOCAL_FLAGS`, but its resolver never consulted that set (it hardcodes the ON-everywhere default below) — the listing was dead and was removed (behavior-neutral). | `agents/task/constants.py::skill_catalog_include_all` |
| `SKILLS_WRITABLE` | OFF (**ON under POLYROB_LOCAL**) | Agent can create/patch/delete/promote skills (`skill_manage`). | `agents/task/constants.py:277` |
| `SKILLS_WRITABLE_REQUIRE_REVIEW` | ON (`True`) | Main-agent skill writes go to `.pending/` review unless promoted. | `agents/task/constants.py:281` |
| `SKILL_OVERWRITE_PROTECT` | **ON** | An agent/background overwrite of an existing ACTIVE skill becomes a `.pending/` proposal (owner promotes) instead of clobbering it in place; all overwrites archive the prior body. | `agents/task/constants.py::AutonomyConfig.skill_overwrite_protect` |
| `POLYROB_TRUST_PROJECT_SKILLS` | **local-mode default: ON**; server: **forced OFF, no env override** | Gate for project-scope skill discovery (`./.agents/skills/`, `./.claude/skills/`, walked CWD->git-root). On the SERVER (`not local_mode_enabled()`) this is fail-closed OFF unconditionally — the process CWD is the install dir, not a trusted operator repo (mirrors `PROJECT_CONTEXT_SERVER_MODE`); no env var can flip it on there. On the LOCAL operator CLI it defaults ON (the CWD is the operator's own repo); opt out with `POLYROB_TRUST_PROJECT_SKILLS=false`. | `agents/task/agent/skill_discovery.py::trust_project_skills_effective`, `project_external_roots` |

**Skill storage, size limits, and update-safety (agentskills.io compliance pass, 2026-07):**
- **Storage location.** Builtin/system skills ship read-only in the installed package tree
  (`data/prompts/skills/`, `skill_store.builtin_scope()`). Per-tenant authored/imported skills live at
  **`<data_home>/skills/user_<uid>/`** (`skill_store.skills_data_home()`/`user_scope()`) — for the local
  CLI that's `./.polyrob/skills/user_<uid>/` by default (project-scoped `cwd`), or wherever
  `POLYROB_DATA_DIR` points for a headless/server deployment — so a `polyrob update` code-swap (which
  replaces the package tree) never touches them. A one-time, idempotent, lock-guarded migration
  (`skill_store.migrate_legacy_user_skills`) moves any pre-existing code-tree `user_<uid>/` skills (incl.
  their `.pending`/`.archived` history) into data-home on first boot past this change.
  `cli/update/context.py` snapshots `<data_home>/skills` alongside `identity/` on every `polyrob update`,
  so authored skills survive an update or rollback. | `agents/task/agent/skill_store.py:108-127,532`, `cli/update/context.py:47` |
- **Char caps.** The old flat `MAX_SKILL_CONTENT_CHARS=12000` hard-reject is replaced by two thresholds:
  `MAX_SKILL_FILE_CHARS=40000` (on-disk DoS-guard ceiling — a body over this is rejected) and
  `MAX_SKILL_INJECT_CHARS=20000` (~5000-token agentskills.io-recommended injected-body size — a body
  between the two is accepted but warns at injection time, not rejected). | `agents/task/agent/skill_manager.py:37-40` |
- **`load_skill` dedup.** `build_load_skill_result(session_skills, skill_id, activated=None)` tracks a
  session-scoped `Controller._activated_skills` set; a repeat `load_skill` call for an id already active
  in the session returns a short ack (`metadata.skill_already_active=True`) instead of re-emitting the
  full body. | `tools/controller/_helpers.py:53-86` |

---

## Autonomy & continuous-learning loops

All read through `AutonomyConfig` (`agents/task/constants.py:250+`); safe ones flip ON
as a group under `POLYROB_LOCAL` (see profile subsection).

| Flag | Default | What it does | Code anchor |
|---|---|---|---|
| `SELF_WAKE_ENABLED` | OFF (**ON under POLYROB_LOCAL**) | W1: re-enter an idle session as a forged continuation turn. | `agents/task/constants.py:261` |
| `SELF_WAKE_MAX_REENTRIES` | `3` | Per-session self-wake depth cap. | `agents/task/constants.py:265` |
| `SELF_WAKE_IDLE_BACKOFF_SEC` | `30` | Idle backoff before a self-wake re-entry. | `agents/task/constants.py:270` |
| `BACKGROUND_REVIEW_ENABLED` | OFF (**ON under POLYROB_LOCAL**) | W2: fork a cheap aux reviewer every N productive turns. | `agents/task/constants.py:294` |
| `BG_REVIEW_INTERVAL` | `10` | Productive-turn interval between background reviews. | `agents/task/constants.py:298` |
| `BG_REVIEW_MAX_STEPS` | `8` | Max steps for a background-review fork. | `agents/task/constants.py:302` |
| `CRON_RUN_LOOP` | **ON** | W3 fix: cron jobs actually run `run_session` (not just create an idle session). | `agents/task/constants.py:307` |
| `CRON_DELIVERY_ENABLED` | OFF | Deliver cron results out-of-band (telegram/email/twitter). | `agents/task/constants.py:311` |
| `CRON_DELIVERY_ALLOW_EXPLICIT_TARGET` | OFF | Allow an explicit (non-tenant-default) delivery target. | `cron/delivery.py:43` |
| `TICKER_IDLE_BACKOFF_ENABLED` | OFF (**ON under POLYROB_LOCAL**) | Cron + goal-dispatch background tickers back off their poll interval on idle ticks (nothing due) instead of firing at a fixed cadence forever — cuts CPU wakeups/battery drain on a single-user local CLI. | `agents/task/constants.py:306` |
| `TICKER_IDLE_BACKOFF_MAX_MULTIPLIER` | `5` | Cap on how many multiples of a ticker's base interval idle backoff may reach (e.g. 5x a 60s base = 300s worst-case staleness). | `agents/task/constants.py:327` |
| `GOALS_ENABLED` | OFF (**ON under POLYROB_LOCAL**) | W4: durable cross-session goal board + dispatcher. | `agents/task/constants.py:316` |
| `GOAL_MAX_RETRIES` | `2` | Goal circuit-breaker failure threshold. | `agents/task/constants.py:320` |
| `GOAL_CLAIM_TTL_SEC` | `900` | Goal claim lease TTL. | `agents/task/constants.py:324` |
| `GOAL_MAX_RUN_SECONDS` | `1800` | Hard wall-clock cap on a single goal run (mirrors cron); a timeout is recorded as a failure and the slot is reclaimed. | `agents/task/constants.py::AutonomyConfig.goal_max_run_seconds` |
| `GOAL_DISPATCH_INTERVAL_SEC` | `60` | Goal dispatcher tick interval. | `agents/task/constants.py:328` |
| `GOAL_MAX_CONCURRENT` | `2` | Max concurrent goal runs. | `agents/task/constants.py:332` |
| `GOAL_PLANNER_ENABLED` | OFF | Goal-planner feature gate. | `agents/task/constants.py::AutonomyConfig.goal_planner_enabled` |
| `GOAL_PLANNER_MIN_READY` | `2` | Min ready goals before planner triggers. | `agents/task/constants.py::AutonomyConfig.goal_planner_min_ready` |
| `GOAL_PLANNER_COOLDOWN_SEC` | `3600` | Planner cooldown (seconds). | `agents/task/constants.py::AutonomyConfig.goal_planner_cooldown_sec` |
| `GOAL_PLANNER_HISTORY_N` | `10` | Planner goal history window. | `agents/task/constants.py::AutonomyConfig.goal_planner_history_n` |
| `GOAL_DAILY_QUOTA` | `6` | Max goal runs started per trailing 24h; <=0 disables. | `agents/task/constants.py::AutonomyConfig.goal_daily_quota` |
| `GOAL_SELF_WAKE_ENABLED` | OFF | Goal-initiated self-wake re-entry — gates whether the dispatcher *attempts* delivery (`agents/task/goals/dispatcher.py::_self_wake`). **Required combo (AU-F1.3):** this alone is not sufficient — the delivery producer (`TaskAgent.deliver_self_wake`) separately no-ops unless `SELF_WAKE_ENABLED` is ALSO on, so a goal announcing its own completion via self-wake needs **both** `GOAL_SELF_WAKE_ENABLED=true` and `SELF_WAKE_ENABLED=true` (the latter defaults ON under `POLYROB_LOCAL`, so on the CLI you typically only need to flip the former). | `agents/task/constants.py::AutonomyConfig.goal_self_wake_enabled`; `agents/task_agent_lite.py:1705` |
| `GOAL_DEDUP_THRESHOLD` | `0.6` | Goal dedup similarity threshold (0.0–1.0). | `agents/task/constants.py::AutonomyConfig.goal_dedup_threshold` |
| `CURATOR_ENABLED` | OFF (**ON under POLYROB_LOCAL**) | W5: stale/archive unused authored skills (Phase 1, no LLM). | `agents/task/constants.py:337` |
| `CURATOR_INTERVAL_HOURS` | `168` | Curator tick interval (hours). | `agents/task/constants.py:341` |
| `CURATOR_STALE_DAYS` | `30` | Days unused before a skill is staled. | `agents/task/constants.py:345` |
| `CURATOR_ARCHIVE_DAYS` | `90` | Days before stale skill is archived. | `agents/task/constants.py:349` |
| `INSIGHTS_TOOL` | OFF (**ON under POLYROB_LOCAL**) | W7: read-only authored-skill reuse-% `insights` action. | `agents/task/constants.py:363` |
| `PROJECT_CONTEXT_AUTOLOAD` | OFF (**ON under POLYROB_LOCAL**) | C9: auto-load a project file as a frozen `PROJECT_CONTEXT` foundation message. Names by precedence: `polyrob.md` > `POLYROB.md` > `AGENTS.md` > `CLAUDE.md` > `.cursorrules` (highest-precedence name that exists wins; not concatenated). Local = trusted/steering. | `agents/task/constants.py::project_context_autoload`; `agents/task/agent/core/project_context.py:28-44` |
| `PROJECT_CONTEXT_MAX_TOKENS` | `20000` | Token cap on concatenated project-context content (truncated with a notice). | `agents/task/constants.py::project_context_max_tokens` |
| `PROJECT_CONTEXT_SERVER_MODE` | OFF (NOT a safe-local flag) | Phase 2: load project context on the **server** (not local mode) and inject it **untrusted-wrapped** (framed as DATA, not instructions). Searches the **tenant session workspace** (`pm().get_workspace_dir`), NEVER the process CWD/install dir. `POLYROB_LOCAL` does NOT flip this on. | `agents/task/constants.py::project_context_server_mode`; `agents/task/agent/core/project_context.py::build_project_context_message` |
| `EPISODIC_MEMORY_ENABLED` | OFF (**ON under POLYROB_LOCAL**) | Episodic activity ledger: write one durable, time-ordered row per completed run (chat/goal/cron) to the `episodes` table in `memory.db`, independent of H-MEM findings. Feeds `recent_activity`, the session-start digest, and the continuity bridge. | `agents/task/constants.py::AutonomyConfig.episodic_memory_enabled` |
| `EPISODIC_DIGEST_INJECT` | OFF (**ON under POLYROB_LOCAL**) | Inject a passive session-start digest of recent episodes (chat sessions only, `exclude_surfaced=True`) as a pinned foundation message. | `agents/task/constants.py::AutonomyConfig.episodic_digest_inject` |
| `CONTINUITY_BRIDGE_ENABLED` | OFF (**ON under POLYROB_LOCAL**) | Idle-reset continuity bridge: write a closing episode + seed the next session's first step with a short "what happened last time" bridge message. | `agents/task/constants.py::AutonomyConfig.continuity_bridge_enabled` |
| `AUTONOMOUS_CONTINUITY_BRIDGE` | OFF | §7.5: carry a recent-activity summary INTO an **autonomous** goal/cron tick (mirror-image of the chat digest — autonomous-only, first-step, never sub-agent) so ticks stop re-deriving "nothing new". Additive context; fail-open. | `agents/task/constants.py::AutonomyConfig.autonomous_continuity_bridge`; `agents/task/agent/core/episodic_digest.py::build_mission_continuity` |
| `SELF_EVOLUTION_TRANSPARENCY` | OFF (**ON under POLYROB_LOCAL**) | §7.1: proactively notify the owner (Telegram) when the agent writes a pending identity/skill proposal, and back the `polyrob owner pending/promote/reject` surface. Fail-open. | `agents/task/constants.py::AutonomyConfig.self_evolution_transparency`; `core/self_evolution.py` |
| `GOAL_BLOCKER_ESCALATION` | OFF | §7.2: when a goal trips the circuit breaker (`status='blocked'`) OR the planner leaves the pipeline empty repeatedly, surface a concrete ask to the owner over the cron/delivery telegram rail instead of dying silently; also leaves a tracked `kind='ask'` row on the goal board (`polyrob owner asks/fulfill`, Telegram `/asks` `/fulfill`). Fail-open. | `agents/task/constants.py::AutonomyConfig.goal_blocker_escalation`; `agents/task/goals/escalation.py`; `agents/task/goals/board.py::create_ask` |
| `GOAL_EMPTY_PIPELINE_ESCALATE_AFTER` | `2` | Consecutive planner runs that leave the ready queue EMPTY before the stall is escalated to the owner (rides `GOAL_BLOCKER_ESCALATION`; a "queue healthy" planner verdict never escalates; escalates once per stall). | `agents/task/constants.py::AutonomyConfig.goal_empty_pipeline_escalate_after`; `agents/task/goals/dispatcher.py::_maybe_escalate_empty_pipeline` |
| `GOAL_COMPLETION_JUDGE` | OFF | §3.2 (goal-completion-verification, 2026-07-05): when a goal with `payload.acceptance` finishes, a cheap aux model (the `judge` aux slot) judges the acceptance against the framework-recorded action ledger + final message. `unmet` → `record_failure` (normal breaker retries); `met`/`unclear`/error/timeout → pass (never block on uncertainty). Metered like every aux call. | `agents/task/constants.py::AutonomyConfig.goal_completion_judge`; `agents/task/goals/completion_judge.py::judge_goal_completion` |
| `GOAL_JUDGE_TIMEOUT_SEC` | `60` | Wall-clock bound on one completion-judge LLM call; timeout fails open to pass. | `agents/task/constants.py::AutonomyConfig.goal_judge_timeout_sec` |
| `REFLECTION_ON_SESSION_CLOSE` | OFF | §7.7: consolidate a short session's findings at session close (the per-step 25-finding trigger is unreachable for short cron/goal sessions). Extra aux-model call per closed session — opt in after verifying cost. | `modules/memory/task/task_context_manager.py::close_session` |
| `REFLECTION_SESSION_CLOSE_THRESHOLD` | `5` | Minimum findings a session must accrue for the `REFLECTION_ON_SESSION_CLOSE` trigger to fire (cost gate). | `modules/memory/task/task_context_manager.py` |
| `CONTINUITY_LLM_SUMMARY` | OFF (everywhere; NOT a safe-local flag) | Use an aux-model LLM call to summarize the closing episode for the continuity bridge instead of a mechanical summary. Off by default everywhere — adds latency at reset. | `agents/task/constants.py::AutonomyConfig.continuity_llm_summary` |
| `EPISODIC_RETENTION_DAYS` | `90` | Episodic row retention window (days). Enforced by a global (all-tenants) prune riding the curator's own tick cadence — never the write path. | `agents/task/constants.py::AutonomyConfig.episodic_retention_days`; `agents/task/agent/core/curator.py::SkillCurator._prune_episodes` |

---

## Delegation / sub-agents

| Flag | Default | What it does | Code anchor |
|---|---|---|---|
| `SUB_AGENTS_ENABLED` | **ON** (`'true'`; `BotConfig` field also `True`) | Enable `delegate_task` / sub-agent delegation. | `agents/task/constants.py:412`, `core/config.py:84` |
| `SUBAGENT_LEAST_PRIVILEGE` | ON (`'true'`) | Give a delegated child a narrowed dedicated controller/toolset. | `agents/task/constants.py:421` |
| `DELEGATE_BLOCKED_TOOLS` | `{code_execution, coding, cronjob, x402_pay, hyperliquid, polymarket, git, github, process, tool_manage, mcp}` | Container tool_ids dropped from a delegated child. | `tools/controller/delegation.py:45,68` |
| `MAX_SUB_AGENT_DEPTH` | `1` | Delegation depth backstop. | `agents/task/constants.py:453` |
| `MAX_CONCURRENT_SUB_AGENTS` | `3` | Max concurrent sub-agents. | `agents/task/constants.py:445`, `core/config.py:87` |
| `MAX_ASYNC_SUB_AGENTS` | `2` (clamped ≤ concurrent) | Background/async delegation slots. | `agents/task/constants.py:464` |
| `SUB_AGENT_TIMEOUT_SECONDS` / `SUB_AGENT_TIMEOUT` | `600` | Single sub-agent (`goal`) timeout. | `agents/task/constants.py:429`, `core/config.py:85` |
| `PARALLEL_SUBTASKS_TIMEOUT_SECONDS` / `PARALLEL_SUBTASKS_TIMEOUT` | `900` | Parallel-subtasks timeout. | `agents/task/constants.py:437`, `core/config.py:86` |

---

## Tools / code-exec / cron / approvals

| Flag | Default | What it does | Code anchor |
|---|---|---|---|
| `MCP_ENABLED` | OFF (`False`) | Enable the MCP subsystem globally. | `core/config.py:235` |
| `MAX_MCP_PER_STEP` | `3` | Max MCP tool calls per step. | `agents/task/constants.py:536` |
| `MCP_EXEC_RATE_PER_WINDOW` | `20` | MCP exec rate-limit count per window. | `tools/mcp/mcp_tool.py:74` |
| `MCP_EXEC_RATE_WINDOW_SEC` | `60` | MCP exec rate-limit window (s). | `tools/mcp/mcp_tool.py:75` |
| `MCP_ENCRYPTION_KEY` | unset | Fernet key for MCP secret store. | `os.getenv("MCP_ENCRYPTION_KEY")` |
| `ANYSITE_TOOL_ENABLED` | ON (`True`) | Register the `anysite_api` CLI tool. | `tools/anysite/__init__.py:11-13` |
| `ANYSITE_API_KEY` | unset | AnySite API credential. | `os.getenv("ANYSITE_API_KEY")` |
| `CODING_TOOLS_ENABLED` | OFF (**ON under POLYROB_LOCAL**) | Register the coding tools (str_replace/grep/run_tests). | `tools/coding/__init__.py:19-21` |
| `GIT_TOOLS_ENABLED` | OFF (**ON under POLYROB_LOCAL**) | Register the `git` tool (status/diff/log/branch/checkout/add/commit/pull/push/clone) over the confined workspace. | `tools/git/__init__.py::git_enabled` |
| `GITHUB_TOOL_ENABLED` | OFF (not safe-local) | Register the `github` tool (PRs/issues/actions; auth via `GITHUB_TOKEN`/`GH_TOKEN`). | `tools/github/__init__.py::github_enabled` |
| `CODE_EXEC_ENABLED` | OFF | Register `code_execution` tool (NOT a sandbox; never in default tool_ids). | `tools/code_exec/__init__.py:26-27` |
| `CODE_EXEC_BACKEND` | `local_subprocess` | Code-exec backend selector. | `os.getenv("CODE_EXEC_BACKEND","local_subprocess")` |
| `CODE_EXEC_MAX_TIMEOUT_SEC` | `30` | Hard cap on a code-exec run. | `os.getenv("CODE_EXEC_MAX_TIMEOUT_SEC","30")` |
| `CODE_EXEC_MAX_OUTPUT_BYTES` | `100000` | Code-exec output byte cap. | `os.getenv("CODE_EXEC_MAX_OUTPUT_BYTES","100000")` |
| `CODE_EXEC_DOCKER_IMAGE` | `python:3.12-slim` | Container image for the `docker` code-exec backend. | `tools/code_exec/backends/docker.py:182` |
| `CODE_EXEC_NETWORK` | `none` | Docker container network policy: `none` / `egress` (→ docker `bridge`) / `host`; unrecognized values fall back to `none`. | `tools/code_exec/backends/docker.py::_resolve_network` |
| `CODE_EXEC_CONTAINER_MEMORY_MB` | `1024` | Docker container memory cap (MB); also sets `--memory-swap` equal (no swap headroom). | `tools/code_exec/backends/docker.py:183` |
| `CODE_EXEC_CONTAINER_CPUS` | `1.0` | Docker container CPU cap (`--cpus`). | `tools/code_exec/backends/docker.py:184` |
| `CODE_EXEC_PIDS_LIMIT` | `256` | Docker container PID cap (`--pids-limit`). | `tools/code_exec/backends/docker.py:185` |
| `CODE_EXEC_DOCKER_USER` | unset → invoking uid:gid, or `65534:65534` (nobody:nogroup) when the host process itself runs as root | Explicit override for the docker backend's `--user`. | `tools/code_exec/backends/docker.py:192-208` |
| `CODE_EXEC_DOCKER_PERSISTENT` | OFF (not safe-local) | Opt-in: ONE persistent per-session `docker` container (`docker exec` per call) instead of a fresh ephemeral container per `run_code` call, so pip installs/cwd survive across calls within a session. | `tools/code_exec/__init__.py::code_exec_docker_persistent_enabled` |
| `CRON_ENABLED` | OFF | Register the `cronjob` tool + start the cron ticker. | `tools/cronjob_tools.py:120` |
| `MESSAGE_TOOL_ENABLED` | OFF (**ON under POLYROB_LOCAL**) | Agent-callable `message(surface, target, text, action)` send tool. Every non-owner target is default-DENIED unless owner-allowlisted (`polyrob owner allow <surface> <target>` / Telegram `/allow`). ⚠️ Owner-target resolution reads the single process-level operator env (`POLYROB_OWNER_TELEGRAM_ID`/`POLYROB_OWNER_EMAIL`), so this flag is intended for single-owner/local use — enabling it on a multi-tenant server lets EVERY tenant's agent message the operator (owner-allowlisted third-party targets stay per-tenant scoped). | `agents/task/constants.py::message_tool_enabled` + `tools/controller/message_send.py` |
| `APPROVAL_REQUIRED_TOOLS` | `''` (no-op) | Comma list of tools requiring approval before execution. | `tools/controller/service.py:168` |
| `APPROVAL_PROVIDER` | `auto` | Approval provider: `auto`(allow)/`deny`/custom. | `os.getenv("APPROVAL_PROVIDER","auto")` |
| `APPROVAL_TIMEOUT_SEC` | `30` | Approval-request timeout (cancels provider on expiry). | `os.getenv("APPROVAL_TIMEOUT_SEC","30")` |
| `POLYROB_TOOL_DENYLIST` | `''` | Comma list of tools vetoed by the pre-tool-call guardrail (fail-closed). | `os.getenv("POLYROB_TOOL_DENYLIST","")` |
| `POLYROB_AGENT_TOOLSET` | unset | Named toolset driving the CLI session's tool list (`resolve_toolset`), same as `polyrob run --toolset <name>`. Unset ⇒ the legacy CLI default list (`filesystem`/`task`/`web_fetch` + gated `coding`/`anysite`). Either way the list is intersected with `cli_unavailable_tools` so unregistrable tools are never advertised. | `agents/task/tool_defaults.py:93` |
| `TOOL_SCHEMA_ERROR_POLICY` | `DROP_TOOL` | Invalid native schema handling: `DROP_TOOL`/`RAISE`/`WARN`. | `tools/controller/registry/schema_generators.py:33` |
| `TOOL_SCHEMA_SANITIZE` | ON (`'true'`) | Fix hostile JSON-Schema constructs in the emitted tools list. | `os.getenv("TOOL_SCHEMA_SANITIZE","true")` |
| `UNTRUSTED_TOOL_RESULT_WRAP` | **ON** | Frame untrusted tool-result strings in `<untrusted_tool_result>` delimiters. | `agents/task/constants.py:603` |
| `ENABLE_GIF_CREATION` | OFF | (Legacy/dead) GIF creation. | `agents/task/constants.py:60` |
| `FS_REALPATH_CONFINE` | ON (`"on"`) | Confine filesystem ops via realpath. | `os.getenv("FS_REALPATH_CONFINE","on")` |
| `BROWSER_ALLOW_PRIVATE_URLS` | OFF (`'false'`) | Allow browser navigation to private/loopback URLs. | `os.getenv("BROWSER_ALLOW_PRIVATE_URLS","false")` |

Browser timeouts/quality (`agents/task/constants.py:59,394-489`):
`BROWSER_TIMEOUT_SECONDS`=120, `FILESYSTEM_TIMEOUT_SECONDS`=30,
`POLYMARKET_TIMEOUT_SECONDS`=60, `DEFAULT_TOOL_TIMEOUT_SECONDS`=60,
`MCP_TIMEOUT_SECONDS`=180, `SCREENSHOT_JPEG_QUALITY`=70,
`BROWSER_CLOSE_TIMEOUT`=3.0, `BROWSER_CONTEXT_CLOSE_TIMEOUT`=8.0,
`BROWSER_INSTANCE_CLOSE_TIMEOUT`=5.0, `PROCESS_KILL_TIMEOUT`=2.0.

---

## API / server / sessions

| Flag | Default | What it does | Code anchor |
|---|---|---|---|
| `OPENAI_COMPAT_API_ENABLED` | OFF | Mount the OpenAI-compatible `/v1` router. On `/v1`, the request `body.model` (which OpenAI SDK clients always send) is applied per-session and **overrides** the operator's `CHAT_MODEL`/`CHAT_PROVIDER` pin for that session (live `swap_model` on a reused session; baked into the SessionRequest for a new one). | `api/openai_compat/router.py:17-21` |
| `SESSION_REGISTRY_BACKEND` | `memory` | Active session→orchestrator map backend: `memory` (in-proc) or `sqlite` (cross-proc metadata). | `agents/task_agent_lite.py:176`, `api/app.py:185` |
| `API_HOST` | `127.0.0.1` | API bind host. | `core/config.py:205` |
| `API_PORT` | `9000` | API bind port. | `core/config.py:206` |
| `API_AUTH_TOKEN` | unset | API auth token. | `core/config.py:207` |
| `API_RATE_LIMIT_RPM` | `60` | API requests/minute. | `os.environ.get("API_RATE_LIMIT_RPM","60")` |
| `API_RATE_LIMIT_RPH` | `1000` | API requests/hour. | `os.environ.get("API_RATE_LIMIT_RPH","1000")` |
| `API_RATE_LIMIT_BURST` | `10` | API burst allowance. | `os.environ.get("API_RATE_LIMIT_BURST","10")` |
| `CORS_ALLOW_ORIGINS` | **Two independent consumers, two different defaults** — see note below | Comma-separated allowed CORS origins. | `webview/server.py:74-78` (Socket.IO), `api/app.py:470-471` (main FastAPI `CORSMiddleware`) |
| `WEBVIEW_DOMAIN` | the reference deployment's public host (`webview/server.py`, `api/auth_endpoints.py`); `localhost:5050` (`agents/task/utils_webview.py`) — **inconsistent default across call sites; set `WEBVIEW_DOMAIN` for your own deployment** | The public hostname this instance is served from. Feeds the Socket.IO CORS default (below) and the SIWE `domain` field (`api/auth_endpoints.py:108`). | `webview/server.py:75` |
| `JWT_SECRET_KEY` | unset (dev) | JWT signing secret, shared by the API auth middlewares and the WebView (`webview/owner_auth.py` issues/verifies the owner session cookie with the same secret). **Required in production** — `api/app.py` raises `RuntimeError` at startup if unset and `ENVIRONMENT=production`; in dev, unset only logs a warning and disables authentication. A value under 32 chars logs a "consider a stronger secret" warning. | `core/config.py:788`, `api/app.py:531-549`, `webview/owner_auth.py:79-81` |
| `A2A_BASE_URL` | the reference deployment's base URL (set this to your own public URL) | Base URL advertised in the A2A agent card. | `os.environ.get("A2A_BASE_URL",...)` |
| `SESSION_TTL_SECONDS` | `86400` | Session lifetime. | `core/config.py:72` |
| `MAX_SESSIONS_IN_MEMORY` | `100` | Resident session cap. | `core/config.py:73` |
| `MAX_SESSIONS_PER_USER` | `10` | Per-user session cap. | `core/config.py:75` |
| `CREATED_SESSION_TTL_SECONDS` | `3600` | TTL for a created-but-not-yet-run session. | `core/config.py:80` |
| `SESSION_RESET_MODE` | `idle` (#7) | Chat session-boundary policy: `idle`/`daily`/`both`/`none`. Default `idle` everywhere — a chat idle past `SESSION_IDLE_MINUTES` starts fresh (memory recall bridges the gap). The server flip (was `none`) is gated on the recreate-race (#2) + mute-on-resume (#0) fixes. Pin `none` for the legacy inert behavior. | `agents/task/surface_config.py::session_reset_mode` |
| `SESSION_IDLE_MINUTES` | `1440` (**720 POLYROB_LOCAL**) | Idle threshold; a chat idle longer than this starts a fresh session (when `SESSION_RESET_MODE` includes idle). Last-activity is bumped per message via `SessionChatRegistry.touch`. | `agents/task/surface_config.py::session_idle_minutes` |
| `SESSION_RESET_HOUR` | `4` | Local hour (0-23) for the daily session roll (when mode includes daily). | `agents/task/surface_config.py::session_reset_hour` |
| `SURFACE_GC_ENABLED` | ON when local **+** chat bus on, else OFF | Periodic GC (hourly via the autonomy runtime) of stale chat<->session bindings; horizon `max(2× idle window, 7d)`. Keeps the routing map from growing unboundedly. | `agents/task/surface_config.py::surface_gc_enabled`, `core/autonomy_runtime.py::_build_surface_gc_ticker` |
| `UVICORN_HOST` | `127.0.0.1` | Uvicorn bind host (read in `main.py`). | `main.py:39` |
| `UVICORN_PORT` | `9000` | Uvicorn bind port (read in `main.py`). | `main.py:40` |
| `UVICORN_WORKERS` | `1` | Uvicorn worker count (read in `main.py`). | `main.py:41` |
| `UVICORN_RELOAD` | OFF (`false`) | Uvicorn auto-reload (read in `main.py`). | `main.py:42` |
| `ENVIRONMENT` / `ENV` | `development` (varies by site) | Environment selector. | `core/config.py`, `agents/task/constants.py:32` |
| `LOG_LEVEL` | `INFO` | Logging level. | `core/config.py:50` |
| `CHAT_TOOL_IDS` | `task` | Tools loaded for the chat surface. | `agents/task/constants.py:617` |
| `CHAT_MAX_STEPS` | `8` | Max agent steps for a chat-surface turn. | `agents/task/constants.py:618` |
| `CHAT_SKIP_CREDIT_CHECK` | ON (truthy) | Skip the credit check on chat-path turns. | `agents/task/constants.py:627` |

> **`UVICORN_WORKERS`**: read in-process in `main.py` (`int(os.environ.get("UVICORN_WORKERS","1"))`,
> `main.py:41`), default **1 worker**. `workers>1` is only safe with
> `SESSION_REGISTRY_BACKEND=sqlite` + sticky LB routing (see
> `docs/deploy/nginx-sticky-sessions.md` and AGENTS.md SessionRegistry).

> **`CORS_ALLOW_ORIGINS` two-site divergence:** only the Socket.IO CORS default
> (`webview/server.py:74-78`) derives from `WEBVIEW_DOMAIN` —
> `f"http://localhost:3000,https://localhost:3000,https://{WEBVIEW_DOMAIN},http://{WEBVIEW_DOMAIN}"`.
> The main FastAPI `CORSMiddleware` (`api/app.py:470-471`) still falls back to a **plain hardcoded**
> `"http://localhost:3000"` when unset and does **not** pick up `WEBVIEW_DOMAIN` — so changing
> `WEBVIEW_DOMAIN` alone only fixes Socket.IO CORS, not the main API's. Set `CORS_ALLOW_ORIGINS`
> explicitly on any non-default-domain deploy to cover both.

---

## Identity / polyrob / local profile

| Flag | Default | What it does | Code anchor |
|---|---|---|---|
| `POLYROB_LOCAL` | OFF | Single-user terminal profile; flips the `_SAFE_LOCAL_FLAGS` group ON as a default. | `agents/task/constants.py:240-242` |
| `POLYROB_DATA_DIR` | unset | Isolation switch (doc 01/06): when set — the headless/server case — the runtime data home (goals.db/cron.db/memory.db + agent workspaces) lives THERE, OUTSIDE the code tree, and the workspace is under it (not cwd). Unset (local-dev) keeps `cwd/.polyrob` as the data home, workspace == cwd. Independent of `POLYROB_PROJECT_DIR`. | `core/bootstrap.py::_resolve_cli_data_home` |
| `POLYROB_PROJECT_DIR` | unset | When set, the agent uses this folder as ONE persistent workspace shared across sessions/goals/cron (Claude-Code style), **independent** of `POLYROB_DATA_DIR`. Also via `polyrob --project <path>`. Goal concurrency is clamped to single-flight while active; the cross-process lock keys off this dir. CLI/headless single-tenant only — the multi-tenant server ignores it. | `core/bootstrap.py::_resolve_cli_data_home` |
| `POLYROB_PROJECT_SECRET_REFUSE` | `0` | When truthy, refuse to start if the cwd-default persistent workspace contains secrets/`.git` (SEC-1). Default = warn only. | `core/bootstrap.py` (build_cli_container) |
| `POLYROB_REQUIRE_PAIRING` | OFF | Owner-allowlist + DM-pairing ingress gate; when ON, an unpaired sender is denied at the surface dispatcher. | `core/pairing.py:30` |
| `TELEGRAM_BOT_TOKEN` | unset | Telegram bot token (from @BotFather) for the `polyrob telegram` local-polling surface. Read from process env or `./.polyrob/.env`; never commit it. | `cli/commands/telegram.py` |
| `ALLOWED_TELEGRAM_USER_IDS` | unset | Comma list of raw Telegram numeric user ids allowed to drive the bot. Unset ⇒ bot is locked and replies with the sender's id (bootstrap); set ⇒ only those ids reach the agent, others are ignored. A **single** entry here also serves as the owner tg id when `POLYROB_OWNER_TELEGRAM_ID` is unset (see below). | `surfaces/telegram/harness.py::owner_allowed` |
| `POLYROB_OWNER_TELEGRAM_ID` | unset | The owner's raw Telegram numeric id, used for two things: (1) the **inbound owner alias** — an inbound from this id is aliased to the OWNER principal (`POLYROB_OWNER_USER_ID`) instead of its surface-hashed `u_…` id, so the owner's chat shares autonomy's tenant (goals/memory/SELF); (2) the **out-of-band delivery** target for cron/goal/self-wake reports to a non-numeric tenant (e.g. `rob`). Falls back to a single-entry `ALLOWED_TELEGRAM_USER_IDS`. Telegram-only (authenticated sender); email/WhatsApp are never aliased. | `core/instance.py::resolve_owner_telegram_id` / `owner_surface_alias`, `cron/delivery.py::_owner_telegram` |
| `POLYROB_OWNER_EMAIL` / `BOT_OWNER_EMAIL` | unset | The owner's email address for **out-of-band cron delivery** (`deliver="email"`) on single-owner headless deploys where no `user_directory` service is registered. Mirrors `POLYROB_OWNER_TELEGRAM_ID`. Unset ⇒ email delivery has no recipient (fail-open, no send). Requires `CRON_DELIVERY_ENABLED`. (Goal-board results are delivered via the self-wake rail, not out-of-band email.) | `core/instance.py::resolve_owner_email`, `cron/delivery.py::_owner_email` |
| `SINGULAR_CHAT_ENABLED` | OFF (**ON for `polyrob telegram`**) | Installs the outbound surface bus so agent replies route to a chat surface. | `core/surfaces/bootstrap.py:27` |
| `TELEGRAM_SURFACE_ENABLED` | OFF (**ON for `polyrob telegram`**) | Telegram surface enable flag. | `agents/task/surface_config.py` |
| `TELEGRAM_INCREMENTAL_STREAM` | OFF | Live `editMessageText` streaming (#8): a turn's deltas open+edit one message (stable per-turn `stream_id`); the discrete reply finalizes that bubble in place via `_finalize_live_on_send` (clean final, no duplicate). Engine is in the base `Surface`; Telegram supplies the transport primitives. **Opt-in:** intermediate frames are raw deltas (per-chunk brain-scrubbed, best-effort); the persisted final is clean. | `agents/task/surface_config.py::telegram_incremental_stream`, `core/surfaces/surface.py` |
| `TELEGRAM_STREAM_EDIT_INTERVAL_SEC` | `1.5` | Min seconds between live stream edits (flood-control); `0` edits on every delta. | `agents/task/surface_config.py::telegram_stream_edit_interval_sec` |
| `VOICE_TRANSCRIPTION_ENABLED` | OFF | Transcribe inbound voice/audio to text before routing (#9), so a voice note is handled like a typed message. Needs the faster-whisper extra; degrades to no-transcript if absent. | `agents/task/surface_config.py::voice_transcription_enabled`, `modules/transcription/` |
| `VOICE_TRANSCRIPT_ECHO` | ON | Echo the transcript back into the chat as a persistent, voice-note-anchored message (`🎙️ Transcript: "…"`) before the agent answers, on Telegram and WhatsApp. WhatsApp additionally marks the voice note read (✓✓). `false` = byte-identical prior behavior. | `agents/task/surface_config.py::voice_transcript_echo_enabled`, `core/surfaces/voice_echo.py` |
| `VOICE_TRANSCRIPTION_MODEL` | `base` | faster-whisper model size (`tiny`/`base`/`small`/`medium`/`large-v3`). | `agents/task/surface_config.py::voice_transcription_model` |
| `CORRESPONDENT_ACCESS_ENABLED` | OFF | WS-A three-tier access model — the **single switch** for the whole feature (no sub-flags). When ON, `route_inbound` classifies each inbound as OWNER (steers), CORRESPONDENT (reply = DATA into the originating session via a `CORRESPONDENT`-origin control message, never a command), or DENIED; the tier block is **fail-CLOSED**; the local-owner bypass is surface-scoped to `{cli,local,repl}`; and the **capability gate** (deny high-impact tools while a session is correspondent-tainted) + the principal-awareness frame are registered. OFF ⇒ byte-identical legacy routing. | `agents/task/surface_config.py::correspondent_access_enabled`, `core/surfaces/{dispatcher,access}.py`, `agents/task/agent/core/correspondent_gate.py` |
| `CORRESPONDENT_REQUIRE_APPROVAL` | ON (`True`) | A newly auto-seeded correspondent is PENDING (owner must `polyrob owner approve`) before replies route. Set `false` for single-user/local. | `agents/task/surface_config.py::correspondent_require_approval`, `surfaces/email/seed.py` |
| `CORRESPONDENT_MAX_NEW_PER_DAY` | `20` | Per-tenant cap on new correspondents seeded per 24h (bounds injected mass-contact). | `agents/task/surface_config.py::correspondent_max_new_per_day` |
| `EMAIL_SURFACE_ENABLED` | OFF (**ON for `polyrob email`**) | Email surface (IMAP poll inbound + SMTP outbound). v1 is correspondent-only; owner-by-email stays OFF. | `agents/task/surface_config.py::email_surface_enabled`, `surfaces/email/harness.py` |
| `EMAIL_IMAP_POLL_SEC` | `60` | Seconds between IMAP polls for new mail (no IDLE in v1). | `agents/task/surface_config.py::email_imap_poll_sec` |
| `GMAIL_EMAIL` / `GMAIL_APP_PASSWORD` | unset | Mail credentials for the `email` tool (SMTP send + IMAP read) — the email surface reuses the same tool/creds. Both must be set or the email tool refuses to initialize. Any IMAP/SMTP provider works despite the `GMAIL_` name (see server vars below). | `core/config.py:222-223`, `tools/email_tool.py:55` |
| `GMAIL_IMAP_SERVER` / `GMAIL_SMTP_SERVER` / `GMAIL_SMTP_PORT` | `imap.gmail.com` / `smtp.gmail.com` / `587` | IMAP/SMTP server endpoints for the email tool; point these at any provider. | `core/config.py:224-226` |
| `WHATSAPP_SURFACE_ENABLED` | OFF (**ON for `polyrob whatsapp`**) | WhatsApp Cloud API webhook surface enable flag. | `agents/task/surface_config.py:32`, `cli/commands/whatsapp.py:41` |
| `WHATSAPP_ACCESS_TOKEN` | unset | Meta permanent/system-user access token used by the Cloud API sender client. | `surfaces/whatsapp/client.py:13` |
| `WHATSAPP_PHONE_NUMBER_ID` | unset | Meta Phone Number ID of the sending number. | `surfaces/whatsapp/client.py:12` |
| `WHATSAPP_VERIFY_TOKEN` | unset | Token echoed back on the GET webhook verify handshake (Meta webhook setup). | `agents/task/surface_config.py::webhook_verify_token` |
| `WHATSAPP_WEBHOOK_SECRET` | unset | HMAC-SHA256 payload-signature secret (Meta app secret). **Unset ⇒ inbound webhook payloads are rejected** (fail-closed). | `surfaces/whatsapp/inbound.py::verify_signature` |
| `WHATSAPP_TEMPLATE_NAME` | `task_ready` | Approved utility template used to re-open the 24h messaging window for a proactive (agent-initiated) message. | `agents/task/surface_config.py:171` |
| `SELF_CONTEXT_WRITABLE` | OFF (**ON under POLYROB_LOCAL**) | Agent can write its evolving SELF identity doc. | `agents/task/constants.py:286` |
| `SELF_CONTEXT_REQUIRE_REVIEW` | ON (`True`) | SELF-context writes go to `.pending/` review. | `agents/task/constants.py:290` |
| `DATA_ROOT` | `./data/task` | Server-bootstrap (`build_bot`/`main.py`) data root for sessions/dbs. Distinct from the CLI's `POLYROB_DATA_DIR`/`./.polyrob` default (see Identity/local-profile above). | `os.getenv("DATA_ROOT","./data/task")` (`core/bootstrap.py:180`) |
| `POLYROB_CLI_CONFIG` | unset | CLI config path override (default `~/.polyrob/cli.json`). | `cli/config_store.py::_config_path` |
| `POLYROB_GITIGNORE_DOTROB` | ON (`"1"`) | Auto-gitignore the `.polyrob/` home. | `os.environ.get("POLYROB_GITIGNORE_DOTROB","1")` (`core/bootstrap.py:506`) |
| `CLI_WORKSPACE_LOCK` | ON (`"1"`) | CLI workspace lock to prevent concurrent CWD corruption. | `os.environ.get("CLI_WORKSPACE_LOCK","1")` |
| `CLI_PREFER_ACTION_TEXT` | ON (`'true'`) | CLI prefers clean action text over raw streamed buffer. | `os.getenv("CLI_PREFER_ACTION_TEXT","true")` |
| `POLYROB_INSTANCE_ID` / `BOT_INSTANCE_ID` | `"rob"` (`DEFAULT_INSTANCE_ID`) | Instance identity id; `POLYROB_INSTANCE_ID` is canonical, `BOT_INSTANCE_ID` an accepted alias. | `core/instance.py::resolve_instance_id` |
| `POLYROB_OWNER_USER_ID` / `BOT_OWNER_USER_ID` | unset → **instance id** | Explicit binding of this instance's OWNER principal (an **internal `user_id`** — **not** a raw Telegram id). It is the tenant key the owner's chat/CLI operates under. **You normally do NOT set this**: precedence is (1) this var, (2) first `SURFACE_SUPER_ADMIN_USER_IDS`, (3) **auto-derive to the instance id** (`POLYROB_INSTANCE_ID`, default `rob`). So on a single-instance deploy, setting only `POLYROB_OWNER_TELEGRAM_ID` aliases the owner's chat onto the instance's own tenant (`rob` — the goals/memory/`identity/rob/user_rob/`) with no redundant name. Set it explicitly ONLY to bind a **distinct human owner uid** different from the instance (that owner IS then named in the agent's awareness line; the auto-derived self-owner is not). `resolve_owner_principal(default_to_instance=False)` gives the STRICT value (None when only the auto-default applies) for diagnostics / layered fallbacks. | `core/instance.py::resolve_owner_principal` |
| `POLYROB_LOCAL_OWNER` | unset | Fallback owner id for `webgate.local_owner_id()`, ranked BETWEEN an explicitly-bound owner and the instance-id default (uses the strict `resolve_owner_principal(default_to_instance=False)` so it isn't shadowed by the auto-derive). | `webview/webgate.py::local_owner_id` |
| `SURFACE_SUPER_ADMIN_USER_IDS` | unset | Comma list of **internal `user_id`s**. **Only its FIRST entry has any effect** — it is precedence (2) for the OWNER principal (`resolve_owner_principal`). POLYROB's model is **single-owner**: there is no surface-layer role ladder, so entries 2..N are inert. (The dead `SurfacePermissions` ladder that used to read the whole set was removed 2026-07-03 as unwired permission-theatre.) | `core/instance.py::resolve_owner_principal` |
| `SURFACE_ADMIN_USER_IDS` | unset | **Inert — grants nothing.** Its only consumer was the dead `SurfacePermissions` role ladder (removed 2026-07-03). Kept documented so operators do not mistake it for an access control. HTTP/API admin is a **separate** concern keyed on `request.state.role` ∈ `ADMIN_ROLES` (`core/constants.py`), not on this var. | `core/constants.py::ADMIN_ROLES` |

---

## Billing / x402 / wallet

> ⚠️ **Unaudited — use at your own risk.** The wallet, signing, x402, and crypto-trading code
> below has had **no independent security audit**. It ships as-is with no warranty and can lose
> funds. All of it is **OFF by default**; enable only if you accept that risk, and prefer
> testnets. See [SECURITY.md](../SECURITY.md#crypto--wallet--payment-features).

| Flag | Default | What it does | Code anchor |
|---|---|---|---|
| `BILLING_FAILOVER_ENABLED` | OFF | Attempt provider fallback on a billing/quota/402 error before permanent halt. | `agents/task/agent/core/error_recovery.py:48` |
| `X402_ENABLED` | OFF (`'false'`) | Enable x402 pay-per-request *receiving*. | `os.environ.get("X402_ENABLED","false")` |
| `X402_CLIENT_ENABLED` | OFF (`'false'`) | Enable the agent-side x402 *paying* tool. | `core/wallet/config.py:46`, `os.getenv("X402_CLIENT_ENABLED","false")` |
| `X402_PAYMENT_RECIPIENT` | `''` | Treasury/recipient address for x402 receipts. | `os.environ.get("X402_PAYMENT_RECIPIENT",...)` |
| `X402_DEFAULT_CHAIN` | `base` | Default chain for x402. | `os.environ.get("X402_DEFAULT_CHAIN","base")` |
| `X402_FACILITATOR_URL` | `''` | x402 facilitator endpoint. | `os.environ.get("X402_FACILITATOR_URL","")` |
| `X402_PRICE_USD` | _derived_ (unset ⇒ economics-based, ~$30) | Single-source x402 per-request price (C2 SSOT) — the middleware charge, `/api/x402/pricing`, the Agent Card, and the 402 challenge all read this. **If set, it wins.** If unset (or invalid), the price is DERIVED (S6): `X402_MAX_TOKENS_PER_REQUEST × max-model-output-rate × X402_PRICE_MARKUP` — the worst-case cost of the budgeted tokens, marked up, since x402 settles before the run. | `modules/x402/x402_integration.py` `get_x402_price_usd()` |
| `X402_MAX_TOKENS_PER_REQUEST` | `200000` | Token budget one x402 request prepays for. **SSOT for BOTH the derived price AND the runtime hard cap** — `LLMUsageTracker` halts an x402 request (`InsufficientCreditsError`) once its per-session cumulative tokens exceed this, so actual cost can never exceed what was prepaid (S6 cost-amplification fix). Admin tier is exempt + uncapped. | `modules/x402/x402_integration.py` `get_x402_max_tokens_per_request()` + `modules/credits/usage_tracker.py` `_enforce_x402_budget()` |
| `X402_PRICE_MARKUP` | `2.0` | Safety multiplier on the derived x402 price (margin over worst-case token cost). Ignored when `X402_PRICE_USD` is set. | `modules/x402/x402_integration.py` `get_x402_price_usd()` |
| `AGENT_WALLET_ENABLED` | OFF | Enable the agent's native wallet. | `core/wallet/config.py:45` |
| `AGENT_WALLET_NETWORK` | `testnet` | Wallet network (`testnet`/`mainnet`). | `core/wallet/config.py:43` |
| `AGENT_WALLET_BACKEND` | `local_eoa` | Wallet key backend. | `core/wallet/config.py:47` |
| `AGENT_WALLET_MAX_PER_TX_USD` | `1000000` | Per-transaction USD ceiling. | `core/wallet/config.py:50` |
| `WALLET_DAILY_CAP_USD` | unset (disabled) | Rolling 24h spend cap; unset = per-tx ceiling only. | `core/wallet/config.py:52` |
| `CREDIT_VALUE_USD` | `0.01` | USD value of one credit. | `os.environ.get("CREDIT_VALUE_USD","0.01")` |
| `WELCOME_BONUS` | `100` | New-user credit grant. | `os.environ.get("WELCOME_BONUS","100")` |
| `EIP8004_ENABLED` | OFF (`'false'`) | Enable EIP-8004 on-chain agent registration. | `api/app.py:702`, `modules/eip8004/registration.py:19` |
| `ETH_PRICE_USD_OVERRIDE` | unset | Fixed ETH/USD price for the deposit monitor — bypasses the live CoinGecko fetch entirely. For ops/testnets with no real price feed; never set on a mainnet deployment (it would misprice real deposits). | `modules/payments/price_oracle.py::get_eth_price_usd` |
| `ETH_PRICE_USD_MAX` | `50000` | Sanity upper bound on the ETH/USD price (override or live fetch); a price above this raises instead of crediting a wildly-inflated deposit — guards against an oracle schema error or an `ETH_PRICE_USD_OVERRIDE` typo. | `modules/payments/price_oracle.py::get_eth_price_usd` |
| `DEPOSIT_MONITOR_ENABLED` | OFF | Enable the deposit-monitoring background loop (requires `SEPOLIA_RPC_URL`/`ETHEREUM_RPC_URL` too). | `core/initialization.py:648`, `core/config.py:782` |
| `ENABLE_AUTH` | OFF | **The real billing gate.** `core/initialization.py::initialize_auth_services()` (called unconditionally from `core/bot.py` on every boot) returns immediately when this is `False`, *before* `balance_manager`/`tier_manager`/`api_key_manager`/`wallet_generator`/the deposit monitor are ever registered on the container — so with it off, no billing service exists at all (not just "routes 404"). Independent of `ENABLE_CREDIT_SYSTEM` (defaults `True` but is only consulted *inside* this same gate, so it's inert whenever `ENABLE_AUTH` is off) and of `X402_ENABLED` (separately gates `X402PaymentMiddleware`). | `core/config.py:780` (`BotConfig.enable_auth`), `core/initialization.py::initialize_auth_services` |
| `PAYMENT_MASTER_SEED` / `MASTER_SEED` | unset | Master seed for deterministic per-user deposit-address derivation. Two names have existed historically (`PAYMENT_MASTER_SEED` is the documented convention; `MASTER_SEED` is a legacy pydantic-settings alias) — `resolve_master_seed()` is the SSOT: **`PAYMENT_MASTER_SEED` wins if set, else `MASTER_SEED`, else `None`** (unset ⇒ deposit-address generation disabled, logged as a warning, not a crash). Distinct from `AGENT_WALLET_MASTER_SEED` (the agent's own outbound wallet, `core/wallet/config.py`). | `core/payment_config.py::resolve_master_seed` |

### Posture ↔ billing (C9)

Own-ops deployments (Posture 0 `local`, Posture 1 `own_ops` — see `webview/webgate.py::posture()`)
run with **no billing services registered** by default. The gate is `ENABLE_AUTH` (`BotConfig.enable_auth`,
default OFF): `core/initialization.py::initialize_auth_services()` — called unconditionally from
`core/bot.py` on every boot — checks `container.config.enable_auth` first and returns immediately
when it's `False`, *before* it ever reaches the code that would register `balance_manager`,
`tier_manager`, `api_key_manager`, `wallet_generator`, or the deposit monitor. `X402_ENABLED`
(default OFF) independently gates `X402PaymentMiddleware` — it's only added to the app when true
(`api/app.py`). Own-ops instances authenticate solely via the owner's own LLM provider API keys —
there is no metered usage to bill.

> **Correction vs. the original C9 plan:** the plan assumed `ENABLE_CREDIT_SYSTEM`
> (`BotConfig.enable_credit_system`) was the off-by-default gate. It isn't — it has defaulted
> `True` since 2025-11-13 (commit `72cf279d`), predating Workstream C entirely, and is only ever
> consulted *inside* `initialize_auth_services()`, after the `enable_auth` check — so it's inert
> whenever `ENABLE_AUTH` is off (the default). It is not a regression; the plan simply misidentified
> which flag does the gating. `tests/unit/api/test_billing_off_by_default.py` locks the real gate.

Note: `api/payment_endpoints.py` and `api/x402_endpoints.py` are mounted **unconditionally** in
`api/app.py` (no `if enable_credit_system` / `if x402_enabled` guard around `include_router`) —
each endpoint self-checks at request time (e.g. `container.get_service('balance_manager')` is
`None` when `ENABLE_AUTH` is off, and the handler 503s or falls back to an admin-only stub) rather
than being absent from the route table. The effective "no billing" guarantee is that **no billing
service is ever registered on the container**, not that the routes 404.

Multitenant deployments (Posture 2 — today `WEBGATE_MULTITENANT=true` or `POLYROB_POSTURE=multitenant`,
resolved by `webview/webgate.py::posture()`/`is_multitenant()`) are the intended posture for turning
`ENABLE_AUTH`/`ENABLE_CREDIT_SYSTEM`/`X402_ENABLED` on. **Forward note for Workstream B:** `webgate.posture()`
does not currently enforce this — an operator could set `POLYROB_POSTURE=local` while also setting
`ENABLE_AUTH=true`/`X402_ENABLED=true`, and nothing today refuses that combination. Wiring `posture()`
to hard-guard against billing flags on a `local`/`own_ops` instance (fail loud, not silently ignore)
is out of scope for Workstream C and is tracked as a B follow-up, not re-litigated here.

---

## WebView / Console (deployment posture, owner login, branding)

`webview/webgate.py` is the SSOT for the deployment **posture** — `local` | `own_ops` |
`multitenant` — and the bind/ownership/branding decisions derived from it. Landmine
(AGENTS.md-noted): these flags are read via `os.environ` directly in `webgate.py`, NEVER via
`BotConfig.get(flag, default)` (a silent-default `getattr`).

| Flag | Default | What it does | Code anchor |
|---|---|---|---|
| `POLYROB_POSTURE` | _derived_ (see below) | Explicit deployment posture override: `local` \| `own_ops` \| `multitenant` (case-insensitive). **Wins outright** over every other posture signal. Resolution order when unset: (1) `WEBGATE_MULTITENANT=true` → `multitenant`; (2) an explicit `WEBGATE_HOST`/`WEBVIEW_HOST` override → `local` if loopback else `own_ops`; (3) no override at all → `local` (today's default: loopback bind, no auth — Posture 0). | `webview/webgate.py::posture` |
| `WEBGATE_MULTITENANT` | OFF | Back-compat boolean alias that maps onto `POLYROB_POSTURE=multitenant` when the explicit posture var is unset (step 1 of the resolution order above). `polyrob dashboard --posture` and `--multitenant` are the CLI-facing spellings. | `webview/webgate.py::posture`, `core/env.py::bool_env` |
| `WEBGATE_HOST` / `WEBVIEW_HOST` | _posture-derived_ (`127.0.0.1` for `local`, `0.0.0.0` for `own_ops`/`multitenant`) | Explicit bind-host override; also feeds posture derivation (step 2 above) when `POLYROB_POSTURE`/`WEBGATE_MULTITENANT` are both unset — a non-loopback value implies `own_ops`. `WEBGATE_HOST` wins over `WEBVIEW_HOST` when both are set. | `webview/webgate.py::bind_host`, `_explicit_host_override` |
| `WEBGATE_PORT` / `WEBVIEW_PORT` | `5050` | Bind port. `WEBGATE_PORT` wins over `WEBVIEW_PORT` when both are set. (`server_launcher.py`'s own code-level default is 3000; the production systemd unit sets `WEBVIEW_PORT=5050`, which `webgate.bind_port()` also defaults to.) | `webview/webgate.py::bind_port` |
| `POLYROB_OWNER_USERNAME` | unset | Owner login username for `own_ops`/`multitenant` posture (Posture 1/2 console access). Both this and the hash below must be set for owner-login to be "configured"; if either is missing, login is refused. | `webview/owner_auth.py::owner_credentials_configured` |
| `POLYROB_OWNER_PASSWORD_HASH` | unset | Argon2 hash of the owner password — **never plaintext**. `verify_owner_password` always runs a real argon2 verify (against a precomputed dummy hash when the username doesn't match) so a wrong username can't be distinguished from a wrong password by response timing (username-enumeration fix, B3 F1). No CLI helper generates the hash yet; hash it with the `argon2-cffi` `PasswordHasher` directly. | `webview/owner_auth.py::verify_owner_password` |
| `POLYROB_CONSOLE_NAME` | `"POLYROB Console"` | Opt-in override of the web console's product display name. Unset keeps the framework brand even when `resolve_instance_id()` is a custom instance id — renaming the console is a deliberate, separate choice from naming the instance. | `core/instance.py::console_display_name` |
| `POLYROB_SUPPORT_URL` | `https://t.me/tmachinrobot` | Support link shown in the console footer/help. | `webview/webgate.py::branding_config` |
| `POLYROB_SUPPORT_HANDLE` | `@TMACHINROBOT` | Support handle text shown alongside the support link. | `webview/webgate.py::branding_config` |
| `POLYROB_ACCESS_GATE_LABEL` | `"DEN holders"` | Label used on the own_ops/multitenant access-gate copy (e.g. "for ⟨label⟩"). | `webview/webgate.py::branding_config` |
| `POLYROB_BRAND_URL` | `https://your-polyrob-host.example` | Instance brand URL; also the base for the derived `terms_url`/`privacy_url` defaults below. | `webview/webgate.py::branding_config` |
| `POLYROB_ORG_URL` | `https://theselfrule.org` | Parent-org URL shown in the console footer. | `webview/webgate.py::branding_config` |
| `POLYROB_TERMS_URL` | `{POLYROB_BRAND_URL}/terms` | Terms-of-service link (was a dead `href="#"` placeholder; now renders a real link). | `webview/webgate.py::branding_config` |
| `POLYROB_PRIVACY_URL` | `{POLYROB_BRAND_URL}/privacy` | Privacy-policy link (same fix as above). | `webview/webgate.py::branding_config` |

All `branding_config()` values are read fresh on every call (not memoized), so every field is
independently overridable per-deploy — an OSS/instance fork isn't locked to any one
instance's domain / support handle / access-gate copy baked in at authoring time.

---

## Misc / runtime knobs

| Flag | Default | What it does | Code anchor |
|---|---|---|---|
| `TWITTER_ENABLED` | OFF (`'false'`) | Enable the Twitter/X write surface. | `os.getenv("TWITTER_ENABLED","false")` |
| `TWITTER_REQUIRE_APPROVAL` | ON (`'true'`) | Require approval for Twitter writes. | `os.getenv("TWITTER_REQUIRE_APPROVAL","true")` |
| `ALLOWED_REASONING_TURNS` | `1` | Tool-free planning turns allowed before escalation. | `agents/task/constants.py:567` |
| `COMPACTION_COOLDOWN_STEPS` | `3` | Steps between LLM-compaction firings (85–95% band). | `agents/task/constants.py:543` |
| `COMPACTION_KEEP_RECENT` | `10` | Min recent messages kept verbatim. | `agents/task/constants.py:550` |
| `COMPACTION_TAIL_TOKEN_RATIO` | `0.20` | Tail kept by token budget. | `agents/task/constants.py:551` |
| `COMPACTION_PER_MSG_CHARS` | `3000` | Per-message head+tail budget into summarizer. | `agents/task/constants.py:552` |
| `COMPACTION_TOOL_RESULT_CHARS` | `2000` | Tool-result budget into summarizer. | `agents/task/constants.py:553` |
| `COMPACTION_MAX_SUMMARY_TOKENS` | `12000` | Summary budget ceiling. | `agents/task/constants.py:554` |
| `COMPACTION_MIN_SUMMARY_TOKENS` | `2000` | Summary budget floor. | `agents/task/constants.py:555` |
| `COMPACTION_MIN_SAVINGS_PCT` | `10.0` | Anti-thrash back-off threshold. | `agents/task/constants.py:556` |
| `COMPACTION_CHECKPOINT` | ON (`'true'`) | Dump pre-compaction trajectory. | `agents/task/constants.py:557` |
| `CONTEXT_OVERFLOW_THRESHOLD` | `0.90` | Context-usage fraction triggering overflow handling. | `os.getenv("CONTEXT_OVERFLOW_THRESHOLD","0.90")` |
| `MAX_REPETITIONS` | `2` (dev) / `3` (prod) | Loop-detection repeat threshold. | `agents/task/constants.py:36,41` |
| `UNCHANGED_STATE_THRESHOLD` | `3` (dev) / `4` (prod) | Unchanged-state loop threshold. | `agents/task/constants.py:37,42` |
| `EMPTY_ACTION_FAST_ESCALATE` | ON (`'true'`) | Escalate fast on empty-action loops. | `os.getenv("EMPTY_ACTION_FAST_ESCALATE","true")` |
| `MIN_STEPS_BEFORE_DONE` | `3` | Minimum steps before `done()` is honored. | `os.getenv("MIN_STEPS_BEFORE_DONE","3")` |
| `STEP_TIMEOUT_SECONDS` | `300` | Per-step timeout. | `agents/task/constants.py:477` |
| `STALL_TIMEOUT_SECONDS` | `300` | Stall detection. | `agents/task/constants.py:478` |
| `LLM_BASE_TIMEOUT_SECONDS` | `30` | Base LLM timeout (adjusted up by token count). | `agents/task/constants.py:483` |
| `LLM_REQUEST_TIMEOUT_SECONDS` | `120` | Standard LLM request timeout. | `agents/task/constants.py:481` |
| `LLM_STREAM_TIMEOUT_SECONDS` | `300` | Streaming LLM timeout. | `agents/task/constants.py:482` |
| `MAX_CONSECUTIVE_FAILURES` | `5` | Step-error halt threshold. | `os.getenv("MAX_CONSECUTIVE_FAILURES","5")` |
| `MAX_PARSE_RETRIES` | `3` | JSON-from-text parse retries (fallback path). | `os.getenv("MAX_PARSE_RETRIES","3")` |
| `TASK_MODE` | `BALANCED` | Task agent mode preset. | `os.getenv("TASK_MODE", TaskMode.BALANCED)` |
| `TASK_PERSONALITY_BLOCK` | OFF (`'false'`) | Inject a personality block into the task agent. | `agents/task/constants.py:611` |
| `ANONYMIZED_TELEMETRY` | ON (`True`) | Enable anonymized telemetry. | `core/config.py:181` |
| `DESTRUCTIVE_ACTION_POLICY` | `none` | HITL policy for destructive actions (`confirm_phrase`/`soft_wait`/`none`). | `core/config.py:157` |
| `HITL_MODE` | `chat` | Human-in-the-loop mode (`chat`/`block`/`off`). | `core/config.py:156` |
| `VALIDATE_OUTPUT` | OFF | Judge-backed validation of the agent's final answer before it's accepted (CO-F1/CO-F10, wired live in Task 16 via the lazily-provisioned aux judge model, fail-open to the main model). An explicit `AgentConfig(validate_output=...)` kwarg still wins over this env default. | `agents/task/agent/service.py::AgentConfig.validate_output` |
| `INTERRUPT_REDIRECT` | OFF | T16: Ctrl-C mid-turn prompts for a redirect instruction that becomes the next turn instead of silently aborting. Deliberately **not** in `_SAFE_LOCAL_FLAGS` — it changes SIGINT UX for all local users, so it stays an explicit opt-in even under `POLYROB_LOCAL`. | `agents/task/constants.py::AutonomyConfig.interrupt_redirect_enabled` |
| `POLYROB_ENV` | `development` (falls back to `ENVIRONMENT`) | Deployment environment name; `production`/`prod` makes the MCP encryption layer require a real `MCP_ENCRYPTION_KEY` instead of the dev key file. | `tools/mcp/security.py:31` |
| `POLYROB_ENV_KEY_BACKFILL` | ON (`"1"`) | Local-mode backfill of missing secret keys (API keys etc.) from `config/.env.*` into the process env. Set falsey to disable. | `core/bootstrap.py:65` |
| `POLYROB_HOME` | `~/.polyrob` | Override for the polyrob home directory (CLI config, keys, home-migration target). | `core/paths.py:27` |
| `POLYROB_IN_DOCKER` | unset | Force the self-update detector to classify the install as Docker (normally auto-detected via `/.dockerenv`); updates then mean "rebuild the image". | `cli/update/detect.py:123` |
| `POLYROB_NONINTERACTIVE` | unset | Truthy ⇒ never prompt interactively (suppresses the inline API-key wizard; same effect as `CI=true`). | `cli/keys.py:64` |
| `POLYROB_PERSISTENT_INPUT` | ON | REPL uses the long-lived bottom-anchored prompt_toolkit input; set `0`/`off` for the legacy ephemeral prompt. | `cli/ui/app.py:318`, `cli/ui/persistent_loop.py` |
| `POLYROB_PERSONA` | unset | Persona for the CLI agent's `<identity>`: a known template key renders that template's persona (and seeds its skills); any other non-empty value is used as literal free-form persona text. Only applied when the personality block is enabled. | `cli/persona.py:20`, `agents/task/agent/core/construction.py:712` |
| `POLYROB_PLAIN` | OFF | Force the plain (non-Rich) CLI renderer; mirrors `--plain` (the flag wins). Render toggle only — does not imply non-interactive. | `cli/commands/run.py:136` |
| `POLYROB_WORKSPACE_LOCK_DIR` | set by bootstrap | Directory for the CLI workspace lock (interactive-gate/CWD-corruption guard). Set automatically by `build_cli_container`; override only for tests/multi-instance layouts. | `core/bootstrap.py:527`, `core/interactive_gate.py:28` |

---

## POLYROB_LOCAL profile (`_SAFE_LOCAL_FLAGS` group)

When `POLYROB_LOCAL` is truthy (set by `build_cli_container` for the terminal-native
single-user agent), the following *safe* flags default **ON as a group** instead of OFF.
An explicit per-flag value still wins — only the default moves. The multi-tenant server
never sets `POLYROB_LOCAL`, so server defaults are unchanged. Anchor:
`agents/task/constants.py:224-237` (`_SAFE_LOCAL_FLAGS`) + `_safe_autonomy_default`.

- `SELF_WAKE_ENABLED`
- `SKILLS_WRITABLE`
- `SELF_CONTEXT_WRITABLE`
- `BACKGROUND_REVIEW_ENABLED`
- `GOALS_ENABLED`
- `CURATOR_ENABLED`
- `INSIGHTS_TOOL`
- `CODING_TOOLS_ENABLED`
- `EPISODIC_MEMORY_ENABLED`
- `EPISODIC_DIGEST_INJECT`
- `CONTINUITY_BRIDGE_ENABLED`
- `SELF_EVOLUTION_TRANSPARENCY`

Deliberately **excluded** from the local group (multi-tenant blast radius even on one
machine): `CODE_EXEC_ENABLED` (not a sandbox), the sub-agent concurrency caps, and
`CONTINUITY_LLM_SUMMARY` (latency at reset — OFF everywhere by default, not gated by
local mode).

(This list is not exhaustive of every flag `_SAFE_LOCAL_FLAGS` carries in code — e.g.
`KB_ENABLED`/`KB_AUTO_PREFETCH`/`CONTEXT_REFERENCES_ENABLED`/`PROJECT_CONTEXT_AUTOLOAD`/
`SKILL_CATALOG_INCLUDE_ALL` are also in the set; see `agents/task/constants.py` for the
current authoritative membership.)

Note: `SKILL_CATALOG_INCLUDE_ALL` still appears in the `_SAFE_LOCAL_FLAGS` set in code, but it is no
longer *gated* by local mode in practice — `skill_catalog_include_all()` (`agents/task/constants.py:871`)
hardcodes its default to `True` unconditionally, so it's ON everywhere, not just under `POLYROB_LOCAL`.
See the Skills section above.

## Web fetch (Tier-1 lightweight web reader)

The default web tool is `web_fetch` (stateless `fetch_url(url) -> markdown`, no browser/Chromium).
The Playwright `browser` tool is opt-in (`tool_ids=['browser']` or a browser-oriented toolset) and
requires the extra: `pip install '.[browser]' && python -m playwright install chromium`.

- `WEB_FETCH_ALLOW_PRIVATE_URLS` — default **false**. When true, `web_fetch` skips SSRF validation
  (allows loopback/private/metadata targets). Single-user/local dev ONLY — never enable in
  multi-tenant prod. Anchor: `tools/web_fetch/tool.py::_allow_private_urls`. When false (default),
  every redirect hop is re-validated and the connection is pinned to the validated IP
  (`tools/web_fetch/fetcher.py::safe_fetch`).

## Crypto trading (Polymarket / Hyperliquid)

The crypto tools split into wallet-free **read** tools and gated **trade** tools. Reads
need no flags; real order submission is OFF by default (dry-run).

| Flag | Default | What it does | Code anchor |
|------|---------|--------------|-------------|
| `CRYPTO_TRADE_LIVE_ENABLED` | OFF | Master kill-switch for real order submission across both venues. OFF → trade tools dry-run (build/validate/route through PolicyGate, never submit). | `tools/crypto_trade_gate.py::evaluate_live_trade` |
| `POLYMARKET_TRADING_ENABLED` | OFF | Per-venue live switch for Polymarket. ANDed with the master switch. | `tools/crypto_trade_gate.py` |
| `HYPERLIQUID_TRADING_ENABLED` | OFF | Per-venue live switch for Hyperliquid. ANDed with the master switch. | `tools/crypto_trade_gate.py` |
| `POLYMARKET_TRADE_MAX_USD` | `5` | Per-order live ceiling for Polymarket (ANDed with `TradingLimits` + PolicyGate). | `tools/crypto_trade_gate.py` |
| `HYPERLIQUID_TRADE_MAX_USD` | `5` | Per-order live ceiling for Hyperliquid. | `tools/crypto_trade_gate.py` |

Tools: `polymarket_data` / `hyperliquid_data` are read-only (no wallet, delegatable, in the
`research` / `trading_research` toolsets); `polymarket` / `hyperliquid` are the gated trade
tools (high-risk, delegate-blocked, correspondent-gated). Polymarket trading requires
`py-clob-client-v2` (`tools/polymarket/clob_adapter.py`); missing → a typed
`POLYMARKET_CLIENT_MISSING` error, not a silent read-only degrade. Hyperliquid agent-wallet
delegation: `approve_agent` / `revoke_agent` / `agent_status`. **Verify on testnet before
enabling live trading.**

## `polyrob update` — self-update & rollback

The self-update mechanism (`cli/update/`, command `cli/commands/update.py`). "Latest" is
discovered from GitHub Releases (git/editable/systemd installs) or PyPI (pip/pipx). Snapshots
land under `<data_home>/snapshots` and capture every SQLite DB (config-resolved `bot.db` +
sidecars), `.env`/config, `identity/`, and `skills/`.

| Env | Default | Meaning | Code anchor |
|-----|---------|---------|-------------|
| `POLYROB_UPDATE_REPO` | derived from the dist Project-URL, else `theselfruleorg/polyrob` | GitHub `owner/name` the update-checker queries. Override for a fork / private mirror / renamed repo. | `cli/update/versions.py::resolve_repo` |
| `POLYROB_UPDATE_PYPI` | `polyrob` | PyPI project name for pip/pipx installs. | `cli/update/versions.py::resolve_pypi_package` |

Flags on the command (not env): `--check` (exit 0 up-to-date / 10 update-available), `--dry-run`,
`--channel stable|pre|git`, `--list-snapshots`, `--rollback [--snapshot NAME]`, `--json`, and
**`--force`** (override the in-use guard on `--rollback`). The rollback guard refuses while a
POLYROB **server/agent process is running** or a DB is actively write-locked
(`cli/update/process_guard.py`) — on a server, **stop the service before rolling back** (the
printed manual steps do this), or pass `--force`.

**Versioning policy (releases):** versions are **3-segment semver** `MAJOR.MINOR.PATCH`, tagged
`vX.Y.Z`. Bug-fix-only releases bump PATCH (`0.4.2` → `0.4.3`); new backwards-compatible features
bump MINOR (`→ 0.5.0`); breaking changes bump MAJOR. **Do not use 4-segment versions** (e.g.
`0.4.2.1`): the semver parser (`cli/update/versions.py::_SEMVER_RE`) reads the 4th segment as a
suffix and treats `0.4.2.1` as **equal to** `0.4.2`, so `--check` would never offer the update.
`release.yml` fails a tag whose `vX.Y.Z` ≠ `pyproject.toml` version.

---

## Avatar (pfp)

The agent avatar (Mindprint) is deterministic and **optional/deferrable** — the agent runs fine with
no avatar. Generation, the live terminal/webview render, and the browser studio need **no flag**
(operator-invoked local actions). Only social-surface push is flag-gated.

| Flag | Default | What it does | Code anchor |
|---|---|---|---|
| `PFP_PUSH_TWITTER` | OFF | Allow `polyrob pfp push --twitter` to set the X profile image (v1.1 `update_profile_image`, OAuth1 from `TWITTER_*` env; decoupled from `TWITTER_ENABLED`; fail-open on 403). | `modules/pfp/push.py` |
| `PFP_PUSH_TELEGRAM` | OFF | Enable the Telegram assisted-push branch (prints BotFather `/setuserpic` steps — the Bot API cannot set a bot's own avatar). | `modules/pfp/push.py` |

The frozen identity blob (`avatar/config/rob.json`; runtime `<home>/identity/{instance_id}/pfp/pfp.json`)
is the reproducibility SSOT — picture traits **and** the engine-agnostic voice signature
(`{pitch,rate,timbre}`) the future voice-interface app consumes. The still PNG is rendered headlessly
via the `[browser]` extra (Playwright/Chromium); when that is unavailable, `pfp generate` falls back to
the committed `avatar/renders/rob.png`. The CLI renders the face LIVE from the pure-Python field port
(`modules/pfp/mesh.py`) — no PNG, no Chromium.
