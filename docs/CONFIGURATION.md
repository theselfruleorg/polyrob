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
| `LLM_TOKEN_STREAMING` | OFF | 019 P5: TRUE per-token streaming. When ON and the provider client implements `astream_agent_response` (Anthropic + OpenAI today), `LLMClientAdapter.astream` yields real scrubbed text deltas (StreamingThinkScrubber per delta; a completion starting with `{` — brain-state JSON — suppresses live deltas so raw JSON never streams to the user); tool_calls/usage/provider-response-id ride the final chunk, so accounting is unchanged. OFF (default) = the legacy single-chunk astream, byte-identical. A pre-first-chunk failure falls back to single-chunk; a mid-stream failure propagates to the retry machinery. | `modules/llm/adapters.py::token_streaming_enabled` |
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
| `AUX_FALLBACK_COMPACTION` / `AUX_FALLBACK_JUDGE` / `AUX_FALLBACK_REFLECTION` | unset | B5: comma-separated ordered fallback candidates for the slot, each `provider/model` (or a bare `model`, which auto-detects its provider). `_provision_aux_llm` walks primary→fallbacks and uses the first candidate that builds; if all fail, falls back to the main model (unchanged fail-open contract). | `agents/task/agent/core/llm_provisioning.py::_provision_aux_llm` |
| *(reflection inheritance)* | — | If `AUX_MODEL_REFLECTION` is unset, reflection inherits compaction's resolved model **and** provider as a pair (`AUX_MODEL_COMPACTION`/`AUX_PROVIDER_COMPACTION`, or the legacy `COMPACTION_MODEL`/`COMPACTION_PROVIDER`), plus `AUX_FALLBACK_COMPACTION` if `AUX_FALLBACK_REFLECTION` is also unset — back-compat for reflection historically reusing the compaction aux model wholesale. If reflection sets its own model, none of compaction's config (including its provider) is consulted. | `agents/task/constants.py::resolve_aux_chain` |
| `REFLECTION_LLM_ENABLED` | **ON** | H-MEM phase consolidation via aux LLM instead of string concat. Disable with `off/false/0/no/none/''`. | `agents/task/constants.py:165-179` |
| `NATIVE_TOOLS_DEBUG` | `''` (off) | Verbose native-tool-call debug logging. | `os.environ.get("NATIVE_TOOLS_DEBUG","")` |
| `DEFAULT_PROVIDER` | unset | Operator-pinned default provider for new CLI/chat sessions (precedence 2 — below an explicit `-p`/API arg, above the `~/.polyrob/cli.json` stored default). `CHAT_PROVIDER` wins over this when both are set. Written by `polyrob init`. | `cli/config_store.py::resolve_provider_model` |
| `DEFAULT_MODEL` | unset | Operator-pinned default model, paired with `DEFAULT_PROVIDER`/`CHAT_PROVIDER`. `CHAT_MODEL` wins over this when both are set. | `cli/config_store.py::resolve_provider_model` |
| `CHAT_PROVIDER` | unset | Highest-precedence operator pin for the default provider; wins over `DEFAULT_PROVIDER`. | `cli/config_store.py::resolve_provider_model` |
| `CHAT_MODEL` | unset | Highest-precedence operator pin for the default model; wins over `DEFAULT_MODEL`. | `cli/config_store.py::resolve_provider_model` |

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
| `KB_ENABLED` | OFF (**ON under POLYROB_LOCAL**) | Tenant knowledge base: `@folder`/`@url` ingestion + KB recall via `session_search`. | `agents/task/constants.py::AutonomyConfig.kb_enabled` |
| `KB_AUTO_PREFETCH` | OFF (**ON under POLYROB_LOCAL**) | Inject KB recall alongside memory recall at step start (T13). | `agents/task/constants.py::AutonomyConfig.kb_auto_prefetch` |
| `CONTEXT_REFERENCES_ENABLED` | OFF (**ON under POLYROB_LOCAL**) | `@file`/`@folder`/`@url` context references in chat input. | `agents/task/constants.py::AutonomyConfig.context_references_enabled` |
| `MEMORY_REQUIRE_USER_ID` | **ON** (`True`) | Refuse empty/anonymous-user recall I/O (multi-tenant safety). Set false for single-user shared-`""` bucket. | `modules/memory/sqlite_memory_provider.py:34` |
| `MEMORY_VECTOR_MAX_DISTANCE` | `0.6` | Max vector distance for `local_vector` recall hits. | `modules/memory/local_vector_memory_provider.py:47` |
| `MEMORY_TOOL_ENABLED` | OFF | Opt-in bounded `memory` notes tool (read/add/remove + C1 create/update/archive/list/show; writes threat-scanned fail-closed, forged turns quarantine to pending; also needs an external provider). | `tools/controller/action_registration.py::_register_memory_tool_action` |
| `MEMORY_TOOL_MAX_ENTRIES` | `50` | Per-tenant entry cap for the `memory` tool (active+pending notes; archived notes free space). | `modules/memory/sqlite_memory_provider.py::_curated_caps` |
| `MEMORY_TOOL_MAX_CHARS` | `2000` | Per-tenant char cap for the `memory` tool. | `modules/memory/sqlite_memory_provider.py::_curated_caps` |
| `MEMORY_ROW_MAX_CHARS` | `4000` | Max chars per auto-injected cross-session `memories` row (D8; truncated at write, shared by the FTS and vector halves so RRF dedup stays consistent). `<=0` disables. | `modules/memory/sqlite_memory_provider.py::SqliteMemoryProvider._row_cap` |
| `MEMORY_RETENTION_DAYS` | `365` | Age-based retention for cross-session `memories` rows (B3): rows with a provenance stamp older than this are pruned on the curator tick (legacy stampless rows exempt). `<=0` disables. | `agents/task/constants.py::AutonomyConfig.memory_retention_days`; `agents/task/agent/core/curator.py::SkillCurator._prune_memories` |
| `KNOWLEDGE_CURATOR_ENABLED` | OFF (**ON under POLYROB_LOCAL**) | C4 mechanical note consolidation on the curator tick: archive never-read agent-authored notes + collapse exact duplicates (archive-only, audited; owner notes exempt). | `agents/task/constants.py::AutonomyConfig.knowledge_curator_enabled` |
| `KNOWLEDGE_NOTE_STALE_DAYS` | `90` | Days an agent-authored note can stay unread before the knowledge curator archives it. | `agents/task/constants.py::AutonomyConfig.knowledge_note_stale_days` |
| `MEMORY_PREFETCH_CADENCE` | `0` (**`3` under `POLYROB_LOCAL` and for autonomous sessions**) | `0` = prefetch on first step only; `N>0` = also every N steps (inert without external provider). Autonomous (goal/cron) sessions default to `3` everywhere (SA-06). Resolved at access time, not import, so it sees `POLYROB_LOCAL` even if set later via `os.environ.setdefault`. Explicit value (incl. `0`) always wins. | `agents/task/constants.py::memory_prefetch_cadence` |
| `HMEM_SEMANTIC` | `auto` | H-MEM cross-phase recall mode: `auto` (embeddings if an embedder exists, else lexical) / `embeddings` / `lexical` / `off`. | `modules/memory/task/task_context_manager.py:_get_semantic_retriever` |
| `HMEM_TAIL_PLACEMENT` | ON | Place in-session H-MEM as a dynamic suffix AFTER the conversation instead of in the foundation prefix, so the stable foundation + growing conversation form a cacheable prompt-cache prefix (only the small H-MEM tail is reprocessed each step). Default flipped ON 2026-07-06 (T1-09) after the local soak; `=false` restores legacy foundation placement. | `agents/task/constants.py::hmem_tail_placement` |
| `MEMORY_THREAT_SCAN` | OFF | Prompt-injection scan rejecting injected findings before H-MEM write. | `modules/memory/task/hierarchical_memory.py:907` |
| `MEMORY_SEARCH_TOOL` | **ON** | Read-only cross-session `memory_search`/`session_search` tool (tenant-scoped). | `agents/task/constants.py:358` |
| `MEMORY_STORE_ANSWER_ONLY` | OFF (**ON under POLYROB_LOCAL**) | Store the distilled ANSWER (not the echoed "User: {q}\nAssistant: {a}" transcript) as the FTS-matched/embedded recall content, so a recall query restating the question doesn't rank the question text as highly as the answer. | `modules/memory/sqlite_memory_provider.py::SqliteMemoryProvider._store_answer_only` |
| `MESSAGE_STORE_BACKEND` | `''` (off) | `sqlite` = additive write-only durable mirror of JSON message history (JSON stays SSOT). | `agents/task/.../sqlite_persistence` (`os.getenv("MESSAGE_STORE_BACKEND","")`) |
| `TRAJECTORY_CAPTURE` | OFF | Opt-in run-end trajectory capture: each finished goal/cron/`polyrob run` session is assembled into a canonical training record (outcome-labeled, fail-closed scrubbed, correspondent-tainted sessions skipped) under `<data_root>/datagen/captured/<user_id>/`. Deliberately NOT in the `POLYROB_LOCAL` safe group — data collection is always an explicit opt-in. ⚠️ No built-in retention: ONE JSON per completed run, forever — on a busy autonomy box budget disk or rotate the `captured/` dir externally. | `datagen/capture.py::_enabled` |
| `MAX_MEMORY_CACHE_SIZE` | `30` | In-memory history cache size. | `agents/task/constants.py:58` |
| `MEMORY_CLEANUP_INTERVAL` | `50` | Clean memory every N operations. | `agents/task/constants.py:64` |

---

## Skills

| Flag | Default | What it does | Code anchor |
|---|---|---|---|
| `SKILL_PROGRESSIVE_DISCLOSURE` | **ON** | Inject only a compact `<skill-catalog>`; full body pulled via `load_skill`. Off = legacy eager full-body. Resolved by `skill_progressive_disclosure()` — an **access-time function** (not an import-bound constant), so an env override always wins even if set after this module first imports (Task 1 rename). | `agents/task/constants.py::skill_progressive_disclosure` |
| `TOOL_PROGRESSIVE_DISCLOSURE` | OFF (**ON under POLYROB_LOCAL**) | Dynamic tool rig (S1-S4): pin an honest `<tool-catalog>` foundation block (every known tool: `loaded` / `loadable` / `gated:<reason>` + remedy), register the `load_tool(tool_id)` action so a session can self-serve a container-servable tool mid-run, and (S4) stop `goal_create` writing keyword-inferred `payload.tools` (inference-only goals stay tools-less → dispatch's wide default applies; explicit tools unchanged). Hard lines regardless of flag: money tools NEVER loadable (explicit owner/goal grant only), delegate-blocked ids refused for leaves, taint/posture/approval gates unchanged (loading registers schemas only). | `core/config_policy/policy.py::tool_progressive_disclosure` + `tools/tool_disclosure.py` |
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

**`AUTONOMY_MODE`** is the capability/approval master switch (proposal 013) — a FOURTH
axis reconciled with the existing three (`POLYROB_LOCAL` = trust profile,
`AUTONOMY_POSTURE` = which autonomy loops run — its *default* is raised to `full` by
this mode, `AGENT_COMPUTE_POSTURE` = host capability, untouched by this mode).
`supervised` (default) is byte-identical to today. `autonomous` is only effective on a
single-owner deployment (`POLYROB_LOCAL` + a bound owner principal); otherwise it
clamps to `supervised` with a one-time WARN:

| `AUTONOMY_MODE` | Default | What it does | Code anchor |
|---|---|---|---|
| `AUTONOMY_MODE` | `supervised` | Capability/approval master. `autonomous` = single-owner act-and-report instance (capability flags below default ON, full autonomous toolset, allow+audit+notify approvals, outbound policy `open`); only effective with `POLYROB_LOCAL` + a bound owner, else clamps. Money-SPEND/host/secrets never move. | `agents/task/constants.py::autonomy_mode` |
| `TOOL_AVAILABILITY_HINT` | **ON** | 013 T8 (owner transparency directive): injects a `<tool-availability>` system-prompt block disclosing every known-but-not-loaded tool with its gate + remedy (`loadable`/`disabled`/`reserved` tiers), so a missing capability is always named rather than guessed at or used as an excuse. Only emitted when the session's `tool_ids` are known (never claims an absence it can't verify); per-session stable (prompt-cache safe). The goal planner's grounding block (`build_planner_prompt`'s "TOOL GROUND TRUTH" section) reuses the same registry's `grantable_autonomous_tools()` regardless of this flag. Fail-open (`""` on any error). A deliberate exception to byte-identity-by-default — pure transparency, no capability change; set `false` to opt out. | `agents/task/agent/core/tool_availability.py`; `agents/task/agent/prompts.py::get_system_message`; `agents/task/goals/planner.py::build_planner_prompt` |

**`AUTONOMY_POSTURE` (W1-1)** is the single coherent switch for the group of autonomy
flags that ship wired but default-OFF (completion judge, blocker escalation, self-wake
delivery, continuity bridge, cron). It moves their *defaults* together — an explicit
per-flag env still wins:

| `AUTONOMY_POSTURE` | Default | What it does | Code anchor |
|---|---|---|---|
| `AUTONOMY_POSTURE` | `silent` | `silent` = today's defaults (autonomy runs but is unverified + owner-silent). `owner-visible` = the agent's autonomous work becomes verified + owner-visible: turns on `GOAL_COMPLETION_JUDGE`, `GOAL_BLOCKER_ESCALATION`, `GOAL_SELF_WAKE_ENABLED`, `AUTONOMOUS_CONTINUITY_BRIDGE`, `EPISODIC_MEMORY_ENABLED`, `EPISODIC_DIGEST_INJECT`, `REFLECTION_ON_SESSION_CLOSE`. `full` = owner-visible **plus** `CRON_ENABLED` (time-based initiative) + `WAKE_CHANGE_GATE` (skip no-change review ticks). Unknown value → `silent`. **Recommended: `owner-visible` for a single-user local/prod-autonomous instance.** | `agents/task/constants.py::autonomy_posture`, `_posture_autonomy_default` |

**`AGENT_COMPUTE_POSTURE`** is the compute-capability ladder (computer-use parity) — a
third axis, orthogonal to `POLYROB_LOCAL` (trust profile) and `AUTONOMY_POSTURE`
(which loops run): how much host/compute capability the agent has. **Frozen at
process start** (import-time snapshot) so a mid-process env mutation can never raise
the running posture; set it in real process env (systemd `EnvironmentFile`, shell, or
dotenv loaded at startup). Capabilities at posture ≥1 additionally require the session
to pass `compute_posture_allows` — OWNER tenant, not a leaf/sub-agent, not a forged
self-wake/delegation-result turn:

| `AGENT_COMPUTE_POSTURE` | Default | What it does | Code anchor |
|---|---|---|---|
| `AGENT_COMPUTE_POSTURE` | `0` | `0` = confined (today's docker sandbox, no persistent shell). `1` = sandbox-dev: persistent networked sandbox with **importable pip installs** (`/install` + `PYTHONPATH`), the `shell` tool scoped INTO the container, loopback port-publish the browser may fetch. `2` = self-maintain: posture 1 + the approval-gated `self_env` verbs (install_dep/patch_source/restart_service/git_pull). `3` = host (requires `POLYROB_LOCAL`, single-tenant box only). Unset/garbage/out-of-range → `0` (garbage never rounds up). | `agents/task/constants.py::compute_posture`, `compute_posture_allows` |

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
| `OWNER_DIGEST_ENABLED` | OFF | Owner daily digest: a cron job carrying `payload.digest` is composed deterministically ($0, no model turn) from the unified ledger + event log + open asks and pushed via the delivery rail. Requires `CRON_DELIVERY_ENABLED` for the actual send; seed with `scripts/seed_owner_digest.py`. | `agents/task/constants.py::AutonomyConfig.owner_digest_enabled`; `cron/digest.py` |
| `CRON_DELIVERY_ALLOW_EXPLICIT_TARGET` | OFF | Allow an explicit (non-tenant-default) delivery target. | `cron/delivery.py:43` |
| `SEND_MESSAGE_USER_DELIVERY` | **ON** | §3.1 communication contract: the agent's `send_message` in an AUTONOMOUS session (goal/cron/planner) routes to the session's OWN principal through the one user-delivery rail instead of dying in the session feed (the lost-blocker-report class). Strict scope: own principal only — arbitrary recipients stay the gated `message` tool's job. Fail-open; `false` restores feed-only. | `core/surfaces/user_delivery.py::send_message_user_delivery_enabled` |
| `USER_DELIVERY_DEDUP_HOURS` | `24` | §3.2 rail memory: identical content (per tenant, content-hash) delivered again within this window is suppressed (`deduped`) — the watermark duplicate-spam class. State lives in the durable telemetry event log; fail-open when unavailable. | `core/surfaces/user_delivery.py::_dedup_hours` |
| `USER_DELIVERY_RATE_PER_HOUR` | `10` | §3.2 rail memory: max user-bound sends per tenant per rolling hour (agent sends + cron delivery + framework notices share the budget). | `core/surfaces/user_delivery.py::_rate_per_hour` |
| `USER_DELIVERY_DAILY_CAP` | `30` | §3.2 rail memory: max user-bound sends per tenant per rolling 24h. | `core/surfaces/user_delivery.py::_daily_cap` |
| `USER_DELIVERY_RESERVED_SLOTS` | `8` | Slots of `USER_DELIVERY_DAILY_CAP` that `priority="low"` traffic (e.g. goal-start pings) may NOT consume, so goal completions + the daily digest keep headroom. Clamped to leave ≥1 slot for low traffic. Safety-bearing sources (`credit_sentinel`/`halt`/`security`) bypass the cap and the hourly limit entirely (dedup still applies). Added after the 2026-07-19 night, when a flat FIFO cap was spent by 17:33Z and then dropped 99 goal completions, both digests, and the credit-sentinel halt notice. | `core/surfaces/user_delivery.py::_reserved_slots` |
| `TELEMETRY_EVENT_LOG_PATH` | unset | Override the durable telemetry event log's db path (default: `<data_root>/telemetry_events.db`). The test suite uses this to keep telemetry (and the §3.2 delivery-rail memory) out of the real data home. | `agents/task/telemetry/event_log.py::get_event_log` |
| `RUN_EVENTS_ENABLED` | **ON** | 019 live run-state observability: master gate for the span/wait feed events (`tool_started` / `llm_started` / `awaiting_approval` / `approval_resolved`, plus the P1 compaction/retry/sub-agent/delegation kinds) that let every surface show what the agent is doing RIGHT NOW (in-flight tool, thinking, blocked approval) instead of dead air. Fail-open; OFF restores the pre-019 outcome-only feed byte-identically. | `core/config_policy/policy.py::AutonomyConfig.run_events_enabled` |
| `TELEGRAM_PROGRESS_EDITS` | **ON** | 019 P2: the Telegram `⚙️ Working…` bubble becomes a live status line (throttled in-place edits ≤1/2.5s: current tool / step / tool count / elapsed / cost, wait states override immediately — `⏸ Waiting for your approval`, `↻ retrying`, `📦 compacting`). Per-owner opt-out via pref `progress.telegram`. | `core/config_policy/policy.py::AutonomyConfig.telegram_progress_edits`; `surfaces/telegram/harness.py::_maybe_start_progress_tracker` |
| `AUTONOMY_START_NOTICE` | OFF (**ON under `AUTONOMY_POSTURE=full`/autonomous**) | 019 P2: one-line owner notice when an autonomous goal/cron run STARTS (`▶ goal started: …`) via the one delivery rail (dedup + caps) — today the owner learns only at completion or in the digest. Digest/$0-gated ticks never notify. | `core/config_policy/policy.py::AutonomyConfig.autonomy_start_notice`; `agents/task/goals/dispatcher.py::_run_goal`; `cron/runner.py::_execute` |
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
| `GOAL_NOTIFY_ON_DONE` | ON | §3.4 SAFETY NET (demoted from unconditional push): tell the user when a background goal completes **only when the agent itself said nothing** during the run (`RunOutcome.user_messages` empty) — the agent's own `send_message` (delivered live via the §3.1 rail) is the primary channel. The fallback text is factual and honestly labeled (✅ only when the completion is evidence-verified, §4.3); rides `push_owner_message` → the delivery rail (dedup + durable `owner_notice` fallback). Set `false` to silence entirely. | `agents/task/constants.py::AutonomyConfig.goal_notify_on_done`; `agents/task/goals/dispatcher.py::_notify_owner_done` |
| `GOAL_DEDUP_THRESHOLD` | `0.6` | Goal dedup similarity threshold (0.0–1.0). | `agents/task/constants.py::AutonomyConfig.goal_dedup_threshold` |
| `AUTONOMY_HALT` | OFF | Owner kill-switch: halt ALL autonomous agent invocation — goal dispatch AND (since G-35) cron ticks, checked FIRST in each gate chain, before any paid work. Togglable without a restart via `polyrob owner halt`/`resume`, or a `<data_dir>/AUTONOMY_HALT` marker file (either trips it). Cron's owner-digest job (`payload.digest`) stays exempt — it's $0 by construction and is the owner's own status report. | `agents/task/constants.py::AutonomyConfig.autonomy_halted`; `agents/task/goals/dispatcher.py::dispatch_once`; `cron/runner.py::make_agent_runner` |
| `CURATOR_ENABLED` | OFF (**ON under POLYROB_LOCAL**) | W5: stale/archive unused authored skills (Phase 1, no LLM). | `agents/task/constants.py:337` |
| `CURATOR_INTERVAL_HOURS` | `168` | Curator tick interval (hours). | `agents/task/constants.py:341` |
| `CURATOR_STALE_DAYS` | `30` | Days unused before a skill is staled. | `agents/task/constants.py:345` |
| `CURATOR_ARCHIVE_DAYS` | `90` | Days before stale skill is archived. | `agents/task/constants.py:349` |
| `INSIGHTS_TOOL` | OFF (**ON under POLYROB_LOCAL**) | W7: read-only authored-skill reuse-% `insights` action. | `agents/task/constants.py:363` |
| `AGENT_STATUS_TOOL` | OFF (**ON under POLYROB_LOCAL**) | I-6: read-only `agent_status` introspection action — steps used/remaining, active tools, context-token usage, wallet + tenant ledger; every section fails soft independently. | `agents/task/constants.py::AutonomyConfig.agent_status_tool`; `tools/controller/action_registration.py::_register_agent_status_action` |
| `VERIFY_BEFORE_DONE` | OFF (**ON under POLYROB_LOCAL**) | I-3 / H3 (dedup decision D1): when the run's action ledger shows a successful code edit (`str_replace`/`apply_patch`/`create_file`/`move_file`/`delete_file`) more recent than the last successful `run_tests` (or with no `run_tests` at all), the first `done()` is intercepted with a guidance nudge to run tests; bounded to 2 attempts (`continue` re-enters the `for step_num in range(max_steps)` loop, same precedent as `VALIDATE_OUTPUT` just below it), never a hard block. Deterministic, ledger-derived, no LLM — the deliberate coding-specific exception to `goals/completion_judge.py`'s capability-agnostic rule. Off = byte-identical legacy. | `agents/task/constants.py::AutonomyConfig.verify_before_done`; `agents/task/runtime/edit_verify.py::edited_since_last_test`; `agents/task/agent/core/run_loop.py:~455` |
| `PROJECT_CONTEXT_AUTOLOAD` | OFF (**ON under POLYROB_LOCAL**) | C9: auto-load a project file as a frozen `PROJECT_CONTEXT` foundation message. Names by precedence: `polyrob.md` > `POLYROB.md` > `AGENTS.md` > `CLAUDE.md` > `.cursorrules` (highest-precedence name that exists wins; not concatenated). Local = trusted/steering. | `agents/task/constants.py::project_context_autoload`; `agents/task/agent/core/project_context.py:28-44` |
| `ENV_CONTEXT_BLOCK` | ON | 014-C1: pin the `<environment>` foundation block (instance, platform, data dir, absolute workspace path + persistence semantics, posture axes, host-executable probe) so the agent knows where it lives. Emits ONLY under `POLYROB_LOCAL` or effective `AUTONOMY_MODE=autonomous` — a plain multi-tenant server session is byte-identical. | `agents/task/agent/core/env_context.py::_enabled` |
| `PROJECT_CONTEXT_MAX_TOKENS` | `20000` | Token cap on concatenated project-context content (truncated with a notice). | `agents/task/constants.py::project_context_max_tokens` |
| `PROJECT_CONTEXT_SERVER_MODE` | OFF (NOT a safe-local flag) | Phase 2: load project context on the **server** (not local mode) and inject it **untrusted-wrapped** (framed as DATA, not instructions). Searches the **tenant session workspace** (`pm().get_workspace_dir`), NEVER the process CWD/install dir. `POLYROB_LOCAL` does NOT flip this on. | `agents/task/constants.py::project_context_server_mode`; `agents/task/agent/core/project_context.py::build_project_context_message` |
| `EPISODIC_MEMORY_ENABLED` | OFF (**ON under POLYROB_LOCAL or `AUTONOMY_POSTURE` owner-visible/full**) | Episodic activity ledger: write one durable, time-ordered row per completed run (chat/goal/cron) to the `episodes` table in `memory.db`, independent of H-MEM findings. Feeds `recent_activity`, the session-start digest, and the continuity bridge. | `agents/task/constants.py::AutonomyConfig.episodic_memory_enabled` |
| `EPISODIC_DIGEST_INJECT` | OFF (**ON under POLYROB_LOCAL or `AUTONOMY_POSTURE` owner-visible/full**) | Inject a passive session-start digest of recent episodes (chat sessions only, `exclude_surfaced=True`) as a pinned foundation message. | `agents/task/constants.py::AutonomyConfig.episodic_digest_inject` |
| `CONTINUITY_BRIDGE_ENABLED` | OFF (**ON under POLYROB_LOCAL**) | Idle-reset continuity bridge: write a closing episode + seed the next session's first step with a short "what happened last time" bridge message. | `agents/task/constants.py::AutonomyConfig.continuity_bridge_enabled` |
| `AUTONOMOUS_CONTINUITY_BRIDGE` | OFF | §7.5: carry a recent-activity summary INTO an **autonomous** goal/cron tick (mirror-image of the chat digest — autonomous-only, first-step, never sub-agent) so ticks stop re-deriving "nothing new". Additive context; fail-open. | `agents/task/constants.py::AutonomyConfig.autonomous_continuity_bridge`; `agents/task/agent/core/episodic_digest.py::build_mission_continuity` |
| `SELF_EVOLUTION_TRANSPARENCY` | OFF (**ON under POLYROB_LOCAL**) | §7.1: proactively notify the owner (Telegram) when the agent writes a pending identity/skill proposal, and back the `polyrob owner pending/promote/reject` surface. Fail-open. | `agents/task/constants.py::AutonomyConfig.self_evolution_transparency`; `core/self_evolution.py` |
| `GOAL_BLOCKER_ESCALATION` | OFF | §7.2: when a goal trips the circuit breaker (`status='blocked'`) OR the planner leaves the pipeline empty repeatedly, surface a concrete ask to the owner over the cron/delivery telegram rail instead of dying silently; also leaves a tracked `kind='ask'` row on the goal board (`polyrob owner asks/fulfill`, Telegram `/asks` `/fulfill`). Fail-open. **§3.4 demotion:** the push is skipped when the agent itself already reported the block to its user during the run (`RunOutcome.user_messages` non-empty); the durable tracked ask is ALWAYS created either way. | `agents/task/constants.py::AutonomyConfig.goal_blocker_escalation`; `agents/task/goals/escalation.py`; `agents/task/goals/board.py::create_ask` |
| `GOAL_EMPTY_PIPELINE_ESCALATE_AFTER` | `2` | Consecutive planner runs that leave the ready queue EMPTY before the stall is escalated to the owner (rides `GOAL_BLOCKER_ESCALATION`; a "queue healthy" planner verdict never escalates; escalates once per stall). | `agents/task/constants.py::AutonomyConfig.goal_empty_pipeline_escalate_after`; `agents/task/goals/dispatcher.py::_maybe_escalate_empty_pipeline` |
| `GOAL_COMPLETION_JUDGE` | **ON** | §4.3 evidence-grounded completion review (autonomous runs): a cheap aux model (the `judge` aux slot) reads the agent's CLAIM (done() text) against the mechanical evidence pack — action ledger, artifact diff, captured ids, typed-check results — acceptance prose optional. `unmet` (claim contradicted by evidence) → `record_failure` with the specific gap; `met` → **verified** (eligible for the ✅ push + self-wake); `unclear`/error/timeout → **done (unverified)** — completes, honestly labeled, excluded from the learning loops (no self-wake, no inline skill distillation). Metered like every aux call. `=false` restores the unjudged legacy path. | `agents/task/constants.py::AutonomyConfig.goal_completion_judge`; `agents/task/goals/completion_judge.py::judge_run_outcome` |
| `GOAL_JUDGE_TIMEOUT_SEC` | `60` | Wall-clock bound on one completion-judge LLM call; timeout fails open to pass. | `agents/task/constants.py::AutonomyConfig.goal_judge_timeout_sec` |
| `GOAL_BLOCKED_MAX_AGE_DAYS` | `14` | §5.3 blocked-goal stewardship: `blocked` goals older than this (measured from when they blocked) age out VISIBLY to `cancelled` (logged `aged_out` event) on the dispatch tick, instead of rotting as permanent planner context. `0` disables aging. The owner/agent can requeue a blocked goal earlier via `goal_unblock` (rationale-logged, retry budget reset). | `agents/task/constants.py::AutonomyConfig.goal_blocked_max_age_days`; `agents/task/goals/board.py::age_out_blocked` |
| `REFLECTION_ON_SESSION_CLOSE` | OFF (**ON under `AUTONOMY_POSTURE` owner-visible/full**) | §7.7: consolidate a short session's findings at session close (the per-step 25-finding trigger is unreachable for short cron/goal sessions). Extra aux-model call per closed session. | `agents/task/constants.py::AutonomyConfig.reflection_on_session_close`; `modules/memory/task/task_context_manager.py::close_session` |
| `AUTONOMY_STATE_DURABLE` | ON | Restart-durable autonomy state (`autonomy_state.db`): background delegations write dispatched/terminal rows (a crash-interrupted delegation is marked `interrupted` at next start and surfaced back to its session — never silently resumed), and the self-wake `ReentryBudget` depth cap survives restart. Fail-open to the legacy volatile registries. | `agents/task/agent/autonomy_state.py`; `agents/task/constants.py::AutonomyConfig.autonomy_state_durable` |
| `REFLECTION_SESSION_CLOSE_THRESHOLD` | `5` | Minimum findings a session must accrue for the `REFLECTION_ON_SESSION_CLOSE` trigger to fire (cost gate). | `modules/memory/task/task_context_manager.py` |
| `CONTINUITY_LLM_SUMMARY` | OFF (everywhere; NOT a safe-local flag) | Use an aux-model LLM call to summarize the closing episode for the continuity bridge instead of a mechanical summary. Off by default everywhere — adds latency at reset. | `agents/task/constants.py::AutonomyConfig.continuity_llm_summary` |
| `EPISODIC_RETENTION_DAYS` | `90` | Episodic row retention window (days). Enforced by a global (all-tenants) prune riding the curator's own tick cadence — never the write path. | `agents/task/constants.py::AutonomyConfig.episodic_retention_days`; `agents/task/agent/core/curator.py::SkillCurator._prune_episodes` |

---

## Delegation / sub-agents

| Flag | Default | What it does | Code anchor |
|---|---|---|---|
| `SUB_AGENTS_ENABLED` | **ON** (`'true'`; `BotConfig` field also `True`) | Enable `delegate_task` / sub-agent delegation. | `agents/task/constants.py:412`, `core/config.py:84` |
| `SUBAGENT_LEAST_PRIVILEGE` | ON (`'true'`) | Give a delegated child a narrowed dedicated controller/toolset. | `agents/task/constants.py:421` |
| `DELEGATE_BLOCKED_TOOLS` | `{code_execution, coding, cronjob, git, github, hf_deploy, hyperliquid, mcp, polymarket, process, self_env, shell, tool_manage, x402_invoice, x402_pay}` | Container tool_ids dropped from a delegated child. Default DERIVED from the capability table (`ids_with("delegate_blocked")`); env override keeps the same shape. | `core/tool_capabilities.py`; `tools/controller/delegation.py:52,65` |
| `MAX_SUB_AGENT_DEPTH` | `1` | Delegation depth backstop. | `agents/task/constants.py:453` |
| `MAX_CONCURRENT_SUB_AGENTS` | `3` | Max concurrent sub-agents. | `agents/task/constants.py:445`, `core/config.py:87` |
| `MAX_ASYNC_SUB_AGENTS` | `2` (clamped ≤ concurrent) | Background/async delegation slots. | `agents/task/constants.py:464` |
| `SUB_AGENT_TIMEOUT_SECONDS` / `SUB_AGENT_TIMEOUT` | `600` | Single sub-agent (`goal`) timeout. | `agents/task/constants.py:429`, `core/config.py:85` |
| `PARALLEL_SUBTASKS_TIMEOUT_SECONDS` / `PARALLEL_SUBTASKS_TIMEOUT` | `900` | Parallel-subtasks timeout. | `agents/task/constants.py:437`, `core/config.py:86` |

---

## Tools / code-exec / cron / approvals

| Flag | Default | What it does | Code anchor |
|---|---|---|---|
| `MCP_ENABLED` | OFF (`False`) (ON under `AUTONOMY_MODE=autonomous`) | Enable the MCP subsystem globally. | `core/config.py:235` |
| `MAX_MCP_PER_STEP` | `3` | Max MCP tool calls per step. | `agents/task/constants.py:536` |
| `MCP_EXEC_RATE_PER_WINDOW` | `20` | MCP exec rate-limit count per window. | `tools/mcp/mcp_tool.py:74` |
| `MCP_EXEC_RATE_WINDOW_SEC` | `60` | MCP exec rate-limit window (s). | `tools/mcp/mcp_tool.py:75` |
| `MCP_ENCRYPTION_KEY` | unset | Fernet key for MCP secret store. | `os.getenv("MCP_ENCRYPTION_KEY")` |
| `MCP_SELF_INSTALL_ENABLED` | OFF (`False`) | Register the agent-callable `mcp_install` action (install a vetted catalog MCP server at runtime). Forged/autonomous turns always refused; approval is Deny-by-default unless `APPROVAL_PROVIDER` is set. NOT in the `POLYROB_LOCAL` safe group. | `tools/mcp/self_install.py::self_install_enabled` |
| `MCP_INSTALL_CATALOG_FILE` | unset | Path to a REVIEWED JSON file of extra installable catalog entries (`{"<id>": {description, transport, url/command, trust}}`), merged over the builtins (file wins on id clash). The real operator seam for extending the install catalog. | `tools/mcp/catalog.py::_load_file_entries` |
| `MCP_INSTALL_ALLOWLIST` | unset | Comma list of extra ids admitted to the install allowlist. An id WITHOUT a catalog entry still has nothing to install — prefer `MCP_INSTALL_CATALOG_FILE`. | `tools/mcp/catalog.py::allowlist` |
| `ANYSITE_TOOL_ENABLED` | ON (`True`) | Register the `anysite_api` CLI tool. | `tools/anysite/__init__.py:11-13` |
| `ANYSITE_API_KEY` | unset | AnySite API credential. | `os.getenv("ANYSITE_API_KEY")` |
| `CODING_TOOLS_ENABLED` | OFF (**ON under POLYROB_LOCAL**) | Register the coding tools (str_replace/grep/run_tests). | `tools/coding/__init__.py:19-21` |
| `CODING_LSP_ENABLED` | OFF (not safe-local) | I-2 / H1 (dedup decision D2): after a successful `str_replace`/`apply_patch`/`create_file`, run an external checker (`pyright --outputjson` for `.py`, `tsc --noEmit` for `.ts`/`.tsx`/`.js`/`.jsx`) against the freshly-written file and append an errors-only `<diagnostics>` block to the result. Deterministic, no LLM call, individually fail-open (missing checker binary / timeout / unparsable output / unsupported extension all yield no-op, never an error result); output capped at 1500 chars. Wired directly into the coding tool (NOT a Controller transform hook). No new pip dependency — checkers are external binaries invoked only if present on PATH. | `agents/task/constants.py::AutonomyConfig.coding_lsp_enabled`; `tools/coding/lsp.py::diagnose_file`; `tools/coding/tool.py::CodingTool._with_diagnostics` |
| `CODING_SNAPSHOT_ENABLED` | OFF (not safe-local) | I-4 / H2 (dedup decision D3): shadow-git PER-FILE snapshot/restore. Before each mutating coding action (`str_replace`/`apply_patch`/`move_file`/`delete_file`, and `create_file` only when overwriting an existing file), commit the SINGLE touched file into a shadow git repo under the session data dir (`pm().get_subdir(session_id, 'coding_snapshots', user_id)`; under default POLYROB_LOCAL that lands in `<cwd>/.polyrob/...`, physically under the project-root workspace like all other `.polyrob/` session state). Safety comes from mechanism, not placement: the shadow git dir is named `git` not `.git` (workspace repo auto-discovery never finds it), `.polyrob/` is auto-gitignored (`core/bootstrap.py`, default-on), every call passes an explicit `--git-dir` (the workspace's own `.git` is never touched), and NEVER `add -A` / the whole tree — only the file about to be mutated is ever staged. Adds two actions: `snapshots(file_path?)` (list pre-edit checkpoints) and `restore(file_path, snapshot_id?)` (checkout a prior version; defaults to the latest snapshot of that file). Gated by the flag AND `compute_posture_allows(ctx, 1)` AND a resolvable `session_id` — any of those failing (or git itself being absent/timing out) is a silent skip, never a blocked edit (fail-open). Snapshot runs BEFORE the write; LSP diagnostics (`CODING_LSP_ENABLED`) still run AFTER, on the result — the two are independent, unentangled hooks. | `agents/task/constants.py::AutonomyConfig.coding_snapshot_enabled`; `tools/coding/snapshot.py`; `tools/coding/tool.py::CodingTool._snapshot_dir`/`_snapshot_before_edit` |
| `GIT_TOOLS_ENABLED` | OFF (**ON under POLYROB_LOCAL**) | Register the `git` tool (status/diff/log/branch/checkout/add/commit/pull/push/clone) over the confined workspace. | `tools/git/__init__.py::git_enabled` |
| `GITHUB_TOOL_ENABLED` | OFF (not safe-local) | Register the `github` tool (PRs/issues/actions; auth via `GITHUB_TOKEN`/`GH_TOKEN`). | `tools/github/__init__.py::github_enabled` |
| `CODE_EXEC_ENABLED` | OFF | Register `code_execution` tool (NOT a sandbox; never in default tool_ids). | `tools/code_exec/__init__.py:26-27` |
| `CODE_EXEC_BACKEND` | `local_subprocess` | Code-exec backend selector. | `os.getenv("CODE_EXEC_BACKEND","local_subprocess")` |
| `CODE_EXEC_MAX_TIMEOUT_SEC` | `30` | Hard cap on a code-exec run. | `os.getenv("CODE_EXEC_MAX_TIMEOUT_SEC","30")` |
| `CODE_EXEC_MAX_OUTPUT_BYTES` | `100000` | Code-exec output byte cap. | `os.getenv("CODE_EXEC_MAX_OUTPUT_BYTES","100000")` |
| `CODE_EXEC_DOCKER_IMAGE` | `python:3.12-slim` | Container image for the `docker` code-exec backend (explicit value wins over `CODE_EXEC_DEV_IMAGE` in every mode). | `tools/code_exec/backends/docker.py::__init__` |
| `CODE_EXEC_DEV_IMAGE` | `nikolaik/python-nodejs:python3.11-nodejs20` | Image for the posture≥1 persistent DEV container when `CODE_EXEC_DOCKER_IMAGE` is unset — python+node so npm/npx toolchains work (014 B1). | `tools/code_exec/backends/docker.py::__init__` |
| `CODE_EXEC_NETWORK` | `none` | Docker container network policy: `none` / `egress` (→ docker `bridge`) / `host`; unrecognized values fall back to `none`. | `tools/code_exec/backends/docker.py::_resolve_network` |
| `CODE_EXEC_CONTAINER_MEMORY_MB` | `1024` | Docker container memory cap (MB); also sets `--memory-swap` equal (no swap headroom). | `tools/code_exec/backends/docker.py:183` |
| `CODE_EXEC_CONTAINER_CPUS` | `1.0` | Docker container CPU cap (`--cpus`). | `tools/code_exec/backends/docker.py:184` |
| `CODE_EXEC_PIDS_LIMIT` | `256` | Docker container PID cap (`--pids-limit`). | `tools/code_exec/backends/docker.py:185` |
| `CODE_EXEC_DOCKER_USER` | unset → invoking uid:gid, or `65534:65534` (nobody:nogroup) when the host process itself runs as root | Explicit override for the docker backend's `--user`. | `tools/code_exec/backends/docker.py:192-208` |
| `CODE_EXEC_DOCKER_PERSISTENT` | OFF (**ON at `AGENT_COMPUTE_POSTURE>=1`**) | Opt-in: ONE persistent per-session `docker` container (`docker exec` per call) instead of a fresh ephemeral container per `run_code` call, so pip installs/cwd survive across calls within a session. Defaults ON at posture≥1 (sandbox-dev installs must persist); explicit value wins. | `tools/code_exec/__init__.py::code_exec_docker_persistent_enabled` |
| `CODE_EXEC_PUBLISH_PORTS` | `8000,5000,8080,3000` | Container ports a **dev** (posture≥1) persistent sandbox publishes to host **loopback** (`-p 127.0.0.1::<port>`, docker-assigned host ports) so the agent can HTTP-test a server it started. Non-dev/ephemeral runs publish nothing. | `tools/code_exec/backends/docker.py::_publish_ports` |
| `SHELL_TOOLS_ENABLED` | OFF (**ON at `AGENT_COMPUTE_POSTURE>=1`**) | Register the persistent `shell` + `process` tools (run inside the session's sandbox container; cwd/env persist; background job manager). Every action is additionally `compute_posture_allows(ctx,1)`-gated (owner, not leaf/forged). In `DELEGATE_BLOCKED_TOOLS`; never in default tool_ids. | `tools/shell/__init__.py::shell_tools_enabled` |
| `SELF_ENV_ENABLED` | OFF (**ON at `AGENT_COMPUTE_POSTURE>=2`**) | Register the `self_env` self-maintenance tool (install_dep/read_source/patch_source/git_pull/restart_service). Every verb is `compute_posture_allows(ctx,2)`- AND approval-gated; install-tree-confined; env/config hard-denied. In `DELEGATE_BLOCKED_TOOLS`. | `tools/self_env/__init__.py::self_env_enabled` |
| `POLYROB_INSTALL_TREE` | repo root (2 levels up from `tools/self_env/tool.py`) | The install-tree root `self_env` read/patch/git_pull operate under (realpath-confined). Set to `/opt/polyrob` on prod. | `tools/self_env/tool.py::_install_root` |
| `POLYROB_SUPERVISED` | OFF | Assert a supervisor (e.g. systemd `Restart=`) will respawn the process, so `self_env_restart_service` may request a restart. Unset = restart refused (never kill an unsupervised agent). | `tools/self_env/tool.py::self_env_restart_service` |
| `HF_DEPLOY_ENABLED` | OFF (not safe-local) | Register the `hf_deploy` tool (deploy/undeploy/list_deployments — publish the session workspace as a Hugging Face Space, Docker SDK). Requires `compute_posture_allows(ctx,2)`. The FIRST publish of a new app is gated by a real approving provider — the tool resolves the same interactive-default provider the Controller uses at posture≥2 (`resolve_gated_actions`), so an unattended/headless run cannot first-publish a new PUBLIC app (interactive_cli fail-closes to deny). Once approved (registry-backed), a redeploy of that SAME app runs unattended within caps. A green `run_tests` with no edit since is required for every deploy (ship == tested). NOT in the `POLYROB_LOCAL` safe group. Never in default tool_ids; in `DELEGATE_BLOCKED_TOOLS` and the correspondent-gate high-impact set. | `tools/hf_deploy/__init__.py::hf_deploy_enabled` |
| `HF_DEPLOY_ORG` | unset | The Hugging Face org/user namespace to publish Spaces under (`<HF_DEPLOY_ORG>/<app_name>`). Required for `deploy`/`undeploy`; refused with a clear error when unset. | `tools/hf_deploy/tool.py::HFDeployTool.deploy` |
| `HF_DEPLOY_DAILY_MAX` | `10` | Max deploy attempts per tenant per rolling 24h. | `tools/hf_deploy/__init__.py::hf_deploy_daily_max` |
| `HF_DEPLOY_MIN_INTERVAL_SEC` | `120` | Minimum seconds between deploy attempts of the SAME app. | `tools/hf_deploy/__init__.py::hf_deploy_min_interval_sec` |
| `HF_TOKEN` | unset (secret) | Hugging Face fine-grained write token used ONLY by the `hf_deploy` broker (`HfApi`) to create/update Spaces and set secrets; read+stripped at call time, never logged/returned (broker errors are token-scrubbed). See the owner runbook in `docs/guide/self-hosting.md`. | `tools/hf_deploy/broker.py::HFSpacesBroker.resolve_token` |
| `DEPLOYED_APPS_DB_PATH` | unset (derived from data root) | Override path for the `hf_deploy` registry DB (`deployed_apps.db`); the test suite redirects this to keep unit runs off the developer's real data home. | `tools/hf_deploy/registry.py::default_deployed_apps_db` |
| `CRON_ENABLED` | OFF | Register the `cronjob` tool + start the cron ticker. | `tools/cronjob_tools.py:120` |
| `WAKE_CHANGE_GATE` | OFF (**ON under `AUTONOMY_POSTURE=full`**) | Cron wake change-gate: a job with `payload.change_gated` skips the paid model call ($0 tick, `cron_run skipped/no_change`) when the tenant's observable state fingerprint (goal board/events, other cron runs, newest episode) is unchanged since the last tick. Delivery jobs (`payload.deliver`) are never gated; fail-open on any fingerprint error. Baseline stored in `cron.db::wake_gate`. | `agents/task/constants.py::AutonomyConfig.wake_change_gate`; `cron/wake_gate.py` |
| `MESSAGE_TOOL_ENABLED` | OFF (**ON under POLYROB_LOCAL**) | Agent-callable `message(surface, target, text, action)` send tool. Every non-owner target is default-DENIED unless owner-allowlisted (`polyrob owner allow <surface> <target>` / Telegram `/allow`). ⚠️ Owner-target resolution reads the single process-level operator env (`POLYROB_OWNER_TELEGRAM_ID`/`POLYROB_OWNER_EMAIL`), so this flag is intended for single-owner/local use — enabling it on a multi-tenant server lets EVERY tenant's agent message the operator (owner-allowlisted third-party targets stay per-tenant scoped). | `agents/task/constants.py::message_tool_enabled` + `tools/controller/message_send.py` |
| `MESSAGE_AUTONOMOUS_ALLOWLISTED` | OFF (ON under `AUTONOMY_MODE=autonomous`) | Whether a forged/AUTONOMOUS turn (goal/cron/planner session, sub-agent, self-wake re-entry) may use `message` at all. OFF = blanket refusal ("owner must be in the loop"). ON = the send falls through to the normal tier gate, so an autonomous run can reach ONLY the owner or an owner-ALLOWLISTED target — the owner-curated allowlist is the owner-in-the-loop mechanism. Single-owner instances that want autonomous posting to sanctioned chats (battle-test 2026-07-14) set this ON; arbitrary targets stay denied either way. | `agents/task/constants.py::message_autonomous_allowlisted`, `tools/controller/action_registration.py::_autonomous_message_refusal` |
| `DELIVERABLES_ATTACH_ENABLED` | OFF (**ON under POLYROB_LOCAL**) | Goal/cron completion pushes attach the run's file deliverables (screened + capped) to the OWNER chat via the delivery rail; the honest text listing (`attached` / `server-only: <path> (<reason>)`) rides regardless. Owner rail only — correspondents never receive workspace files. Proposal 021 / usability assessment 2026-07-19. | `core/config_policy/policy.py::AutonomyConfig.deliverables_attach_enabled`; producer `agents/task/goals/deliverables.py` |
| `DELIVERABLES_ATTACH_MAX_MB` | `10` | Per-file attach cap in MB for the completion AUTO-attach (Telegram bot API hard limit is 50). Oversized files are listed server-only, never silently dropped. The explicit `message` tool rides the larger `MESSAGE_MEDIA_MAX_MB` instead. | `core/surfaces/attachments.py::attach_max_mb` |
| `DELIVERABLES_ATTACH_MAX_FILES` | `3` | Max files attached to one completion message; the remainder is listed by name+size+path. | `core/surfaces/attachments.py::attach_max_files` |
| `MESSAGE_MEDIA_MAX_MB` | `45` | Per-file cap for the explicit `message` tool `media_paths` send (owner-directed; Telegram hard limit ~50 MB). The full attach screen (confinement, secret filename+content, injection scan) applies regardless of cap. | `core/surfaces/attachments.py::message_media_max_mb` |
| `WEBVIEW_PUBLIC_URL` | unset | Public base URL of the owner-auth webview console (e.g. `https://console.example.com`). Set ⇒ goal completions carry a `/session/<id>` deep link and the daily digest a console line; unset ⇒ no links (byte-identical). Links carry no credentials — the console's own owner login gates access. | `core/surfaces/deep_link.py::webview_public_url` |
| `APPROVAL_REQUIRED_TOOLS` | `''` (no-op) | Comma list of tools requiring approval before execution. | `tools/controller/service.py:168` |
| `APPROVAL_PROVIDER` | `auto` | Approval provider: `auto`(allow)/`auto_notify`(allow + `tool_auto_approved` audit event + post-hoc owner notification — act-and-report, 013 T4)/`deny`/custom (`interactive_cli`, `owner_queue` self-register on import). Under effective `AUTONOMY_MODE=autonomous` an unset/`auto`/`interactive_cli` provider **defaults to `auto_notify`** (explicit `deny`/`owner_queue`/custom still wins), with an always-owner-queued lane: the self-modification verbs (`_ALWAYS_GATED_VERBS`: `self_env_*`, `mcp_install`, `self_modify`, `tool_manage`) + owner `approvals.require` pref pins keep the durable `owner_queue` provider (`autonomous_gating_lanes`). Owner `approvals.provider` prefs merge stricter-of over the ladder `auto < auto_notify < interactive_cli < deny`. | `tools/controller/approval.py::resolve_gated_actions`; `os.getenv("APPROVAL_PROVIDER","auto")` |
| `APPROVAL_TIMEOUT_SEC` | `30` | Approval-request timeout (cancels provider on expiry). | `os.getenv("APPROVAL_TIMEOUT_SEC","30")` |
| `PAYMENT_APPROVAL_MODE` | `approve` (`auto` under `AUTONOMY_MODE=autonomous`) | Task 9 / G-2 — the owner-legible switch for outward payment-creation actions (`PAYMENT_APPROVAL_TOOLS`), gated FIRST-CLASS independent of `APPROVAL_REQUIRED_TOOLS`. `approve`: every request (receive AND spend) queues through the durable, remote-capable `owner_queue` provider (`tools/controller/approval_queue.py` — a real owner tap over Telegram `/approve`/`/reject` or `polyrob owner promote/reject tool_approval <tap-id>`, closing G-2's "only a blocking stdin prompt" gap). The wait uses `APPROVAL_TIMEOUT_SEC` but defaults to **300s** for payment tools specifically (vs the generic 30s — a real Telegram round-trip needs minutes); an explicit `APPROVAL_TIMEOUT_SEC` still wins. `auto`: **only the RECEIVE-side subset** (`PAYMENT_RECEIVE_APPROVAL_TOOLS` — today just `x402_request`) is NOT queued — it executes immediately within `modules/x402/invoicing.py`'s own caps (never duplicated here), then fires a post-execution owner notification + `payment_auto_approved` audit event. **The SPEND-side subset — the live-trade order verbs (`hyperliquid_place_limit_order`, `hyperliquid_place_market_order`, `polymarket_place_limit_order`, `polymarket_place_market_order`) — is UNAFFECTED by `auto` and always keeps the SAME `owner_queue` pre-approval as `approve`**, in every mode: trading is never act-and-report (013 T7 review fix, Important finding). A new addition to `PAYMENT_APPROVAL_TOOLS` defaults to the strict (spend) lane unless also added to `PAYMENT_RECEIVE_APPROVAL_TOOLS`. Any other value falls back to `approve` (fail-closed for money). 013 T7: the DEFAULT is mode-dependent — supervised (unset) stays `approve` (byte-identical); under effective `AUTONOMY_MODE=autonomous` (`full_autonomy_enabled()`) it defaults to `auto`, because `approve`'s `owner_queue` path hard-denies forged/autonomous turns, which would otherwise make receive-side invoicing impossible for a single-owner autonomous instance — this default-flip is receive-side only. An explicit `PAYMENT_APPROVAL_MODE` always wins in both modes (and, per the T7 review fix, an *explicit* `auto` no longer act-and-reports trade verbs either — a deliberate behavior change from the original T7 cut). | `agents/task/constants.py::payment_approval_mode`, `PAYMENT_APPROVAL_TOOLS`, `PAYMENT_RECEIVE_APPROVAL_TOOLS`, `_snapshot_payment_approval_mode`, `payment_approval_timeout_sec`; wiring in `tools/controller/service.py::Controller.__init__` |
| `APPROVAL_GRANT_TTL_HOURS` | `24` | TTL for an `owner_queue` ONE-SHOT grant: an owner decision recorded AFTER the requester already timed out still lets the NEXT identical request (same tool+params+tenant hash) through without re-queuing — but only within this window, and only once (atomic single redemption via `GoalBoard.consume_ask_grant`). | `agents/task/constants.py::approval_grant_ttl_hours` |
| `POLYROB_TOOL_DENYLIST` | `''` | Comma list of tools vetoed by the pre-tool-call guardrail (fail-closed). | `os.getenv("POLYROB_TOOL_DENYLIST","")` |
| `POLYROB_AGENT_TOOLSET` | unset | Named toolset driving the CLI session's tool list (`resolve_toolset`), same as `polyrob run --toolset <name>`. Unset ⇒ the legacy CLI default list (`filesystem`/`task`/`web_fetch` + gated `coding`/`anysite`). Either way the list is intersected with `cli_unavailable_tools` so unregistrable tools are never advertised. | `agents/task/tool_defaults.py:93` |
| `TOOL_SCHEMA_ERROR_POLICY` | `DROP_TOOL` | Invalid native schema handling: `DROP_TOOL`/`RAISE`/`WARN`. | `tools/controller/registry/schema_generators.py:33` |
| `TOOL_SCHEMA_SANITIZE` | ON (`'true'`) | Fix hostile JSON-Schema constructs in the emitted tools list. | `os.getenv("TOOL_SCHEMA_SANITIZE","true")` |
| `UNTRUSTED_TOOL_RESULT_WRAP` | **ON** | Frame untrusted tool-result strings in `<untrusted_tool_result>` delimiters. | `agents/task/constants.py:603` |
| `ENABLE_GIF_CREATION` | OFF | (Legacy/dead) GIF creation. | `agents/task/constants.py:60` |
| `FS_REALPATH_CONFINE` | ON (`"on"`) | Confine filesystem ops via realpath. | `os.getenv("FS_REALPATH_CONFINE","on")` |
| `BROWSER_ALLOW_PRIVATE_URLS` | OFF (`'false'`) | Allow browser navigation to private/loopback URLs. | `os.getenv("BROWSER_ALLOW_PRIVATE_URLS","false")` |

> **`PAYMENT_APPROVAL_TOOLS` also gates DRY-RUN orders (L9, 2026-07-15):** the four
> namespaced order verbs (`hyperliquid_place_limit_order`, `hyperliquid_place_market_order`,
> `polymarket_place_limit_order`, `polymarket_place_market_order`) are in
> `PAYMENT_APPROVAL_TOOLS`, and the approval pre-hook fires on ACTION NAME — before the
> tool's own dry-run-vs-live decision runs (`tools/hyperliquid/service.py`,
> `tools/polymarket/service.py`). So under `PAYMENT_APPROVAL_MODE=approve` — **and, since
> the T7 review fix, under `auto` too** (these four verbs are SPEND-side, not in
> `PAYMENT_RECEIVE_APPROVAL_TOOLS`, so `auto` never exempts them) — a paper-trading posture
> (venue tools loaded, `CRYPTO_TRADE_LIVE_ENABLED` OFF) requires **one owner approval per
> dry-run order** — dry-run is decided only AFTER approval clears. An autonomous/forged turn
> (goal, cron, self-wake) can't interactively clear that queue, so it **cannot paper-trade at
> all**, live or dry-run, in EITHER mode. Goal-driven dry-run trading rigs are retired by
> design as a result — build a paper-trading harness against the venue tool directly
> (bypassing the approval-gated action), not via an autonomous goal. Separately: the owner
> kill-switch (`AUTONOMY_HALT` / `polyrob owner halt`, see `autonomy_halted()`) freezes the
> CANCEL verbs too, not just new orders — during an incident, cancel open orders directly at
> the venue (exchange UI/API), not through the agent.

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
| `WEBVIEW_ACTIVITY_ENABLED` | `true` | The console's global `/activity` stream (page + `/api/activity/*` + `join_activity` socket room). Owner/admin-gated in every non-local posture, so on-by-default is safe; `false` = 404/denied everywhere. | `webview/webgate.py::activity_enabled` |
| `WEBVIEW_ACTIVITY_TAIL_SEC` | `2.0` | Poll interval for the activity hub's SQLite id-cursor tails (`telemetry_events.db`, `goal_events`, `skill_install_audit`). Feed events are push (watchfiles), not polled. | `webview/activity.py::ActivityHub._tail_loop` |
| `WEBVIEW_READ_ONLY` | `false` | Monitoring-only console: mutating endpoints (session messages POST, `/api/repair/{id}`, preferences PATCH, pending promote/reject) return 403 server-side and the chat input is not rendered. For deployments where the webview observes a headless agent (`deployment/polyrob-webview.service` sets it via `/etc/polyrob/webview.env`). | `webview/webgate.py::read_only` |
| `POLYROB_API_BASE` | `http://127.0.0.1:9000` | Base URL of the main task API the webview proxies to (chat delivery, queue-status, health) in the classic two-service shape. Override for a non-default API port/host. | `webview/server.py::_api_base` |
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
> `docs/deploy/nginx-sticky-sessions.md` and AGENTS.md SessionRegistry). **The money layer is
> a separate, narrower `workers=1` assumption on top of that:** the amount-jitter
> same-amount-invoice dedupe (`X402_INVOICE_AMOUNT_JITTER`) is serialized by an in-process
> `asyncio.Lock` (`modules/x402/invoicing.py`), and each worker under `workers>1` runs its own
> settlement watcher — so `X402_SETTLE_ONCHAIN_DETECT` + `workers>1` can re-open the
> same-amount collision the jitter exists to prevent (M5). Keep `UVICORN_WORKERS=1` on any
> deployment with `X402_INVOICE_ENABLED`/`X402_SETTLE_ONCHAIN_DETECT`/`SUBSCRIPTIONS_ENABLED`
> on, independent of the session-registry sticky-routing story above.

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
| `POLYROB_LOG_DIR` | unset | Runtime log directory override. Unset → `<data_home>/logs` (T11, 2026-07-16; previously `<install_root>/logs` — a write into the code tree that broke packaged installs). Never CWD-relative. | `core/logging.py::resolve_log_dir` |
| `POLYROB_PROJECT_SECRET_REFUSE` | `0` | When truthy, refuse to start if the cwd-default persistent workspace contains secrets/`.git` (SEC-1). Default = warn only. | `core/bootstrap.py` (build_cli_container) |
| `POLYROB_REQUIRE_PAIRING` | OFF | Owner-allowlist + DM-pairing ingress gate; when ON, an unpaired sender is denied at the surface dispatcher. | `core/pairing.py:30` |
| `TELEGRAM_BOT_TOKEN` | unset | Telegram bot token (from @BotFather) for the `polyrob telegram` local-polling surface. Read from process env or `./.polyrob/.env`; never commit it. | `cli/commands/telegram.py` |
| `ALLOWED_TELEGRAM_USER_IDS` | unset | Comma list of raw Telegram numeric user ids allowed to drive the bot. Unset ⇒ bot is locked and replies with the sender's id (bootstrap); set ⇒ only those ids reach the agent, others are ignored. A **single** entry here also serves as the owner tg id when `POLYROB_OWNER_TELEGRAM_ID` is unset (see below). | `surfaces/telegram/harness.py::owner_allowed` |
| `POLYROB_OWNER_TELEGRAM_ID` | unset | The owner's raw Telegram numeric id, used for two things: (1) the **inbound owner alias** — an inbound from this id is aliased to the OWNER principal (`POLYROB_OWNER_USER_ID`) instead of its surface-hashed `u_…` id, so the owner's chat shares autonomy's tenant (goals/memory/SELF); (2) the **out-of-band delivery** target for cron/goal/self-wake reports to a non-numeric tenant (e.g. `rob`). Falls back to a single-entry `ALLOWED_TELEGRAM_USER_IDS`. Telegram-only (authenticated sender); email/WhatsApp are never aliased. | `core/instance.py::resolve_owner_telegram_id` / `owner_surface_alias`, `cron/delivery.py::_owner_telegram` |
| `POLYROB_OWNER_EMAIL` / `BOT_OWNER_EMAIL` | unset | The owner's email address for **out-of-band cron delivery** (`deliver="email"`) on single-owner headless deploys where no `user_directory` service is registered. Mirrors `POLYROB_OWNER_TELEGRAM_ID`. Unset ⇒ email delivery has no recipient (fail-open, no send). Requires `CRON_DELIVERY_ENABLED`. (Goal-board results are delivered via the self-wake rail, not out-of-band email.) | `core/instance.py::resolve_owner_email`, `cron/delivery.py::_owner_email` |
| `SINGULAR_CHAT_ENABLED` | OFF (**ON for `polyrob telegram`**) | Installs the outbound surface bus so agent replies route to a chat surface. | `core/surfaces/bootstrap.py:27` |
| `TELEGRAM_SURFACE_ENABLED` | OFF (**ON for `polyrob telegram`**) | Telegram surface enable flag. | `agents/task/surface_config.py` |
| `DISCORD_SURFACE_ENABLED` | OFF (**ON for `polyrob discord`**) | Discord surface enable flag (Gateway WS bot; DMs + allowlisted guild channels). Token via `DISCORD_BOT_TOKEN`. ⚠️ The **Message Content Intent** must be enabled in the Discord developer portal (Bot → Privileged Gateway Intents) — without it guild messages arrive with EMPTY `content` and are silently skipped (DMs and @mentions still populate). | `cli/commands/discord.py`, `surfaces/discord/` |
| `SLACK_SURFACE_ENABLED` | OFF (**ON for `polyrob slack`**) | Slack surface enable flag (Socket Mode — no public URL). Tokens via `SLACK_BOT_TOKEN` (xoxb-) + `SLACK_APP_TOKEN` (xapp-). | `cli/commands/slack.py`, `surfaces/slack/` |
| `SIGNAL_SURFACE_ENABLED` | OFF (**ON for `polyrob signal`**) | Signal surface enable flag (local `signal-cli daemon --http`). Config via `SIGNAL_DAEMON_URL` (default `http://127.0.0.1:8080`) + `SIGNAL_ACCOUNT` (+E164); `SIGNAL_SEND_MIN_INTERVAL_SEC` (default `1.0`) throttles sends. | `cli/commands/signal.py`, `surfaces/signal/` |
| `X_SURFACE_ENABLED` | OFF (**ON for `polyrob x`**) | X (Twitter) DM surface enable flag. Inbound = polling `GET /2/dm_events` (no DM webhook on the pay-per-use tier); outbound = `POST /2/dm_conversations/with/:participant_id/messages`. Creds are the SAME OAuth 1.0a user-context `TWITTER_*` env vars the twitter tool uses (bearer-only won't work — DMs need user context). 1:1 DMs only in v1 (group DM conversations are skipped). | `cli/commands/x.py`, `surfaces/x/` |
| `X_DM_POLL_SEC` | `90` | Seconds between `dm_events` polls. X allows **15 DM reads / 15 min per user** (shared across DM GET endpoints), so 60s sits exactly at the cap with no pagination headroom; 90s (10/15 min) leaves room. A 429 backs off to the `x-rate-limit-reset` epoch regardless of this value. Sends are a separate 15/15 min + 1,440/24 h bucket. | `surfaces/x/poller.py::XDMPoller`, `surfaces/x/harness.py::build_x_harness` |
| `GROUP_CHAT_ENABLED` | OFF (ON under `AUTONOMY_MODE=autonomous`) | W3 group/channel ingress. When ON: only chats in the default-DENY group allowlist (`polyrob owner groups allow <surface> <chat_id>`) are served; the owner keeps the normal flow (mention-gated); any other member's @mention routes as untrusted DATA into the bound group session (correspondent rail + capability taint); everything else is a **silent** deny (no auth spam into channels). Fail-CLOSED once on. OFF ⇒ group/channel messages are **silently denied at the dispatcher** (they never fall through to the agent — a bot invited into a channel must not obey arbitrary members). The participant-as-DATA rail also needs `SINGULAR_CHAT_ENABLED` (the owner's group session binds via the chat registry; every `polyrob <surface>` daemon sets it). | `core/surfaces/{access,dispatcher,group_allowlist}.py`, `agents/task/surface_config.py::group_chat_enabled` |
| `GROUP_REQUIRE_MENTION` | ON | In group chats, act only when the bot is @mentioned (surfaces set `InboundMessage.mentions_bot`; unknown counts as NOT mentioned). `false` = respond to every message in allowlisted chats. | `agents/task/surface_config.py::group_require_mention` |
| `TELEGRAM_INCREMENTAL_STREAM` | OFF | Live `editMessageText` streaming (#8): a turn's deltas open+edit one message (stable per-turn `stream_id`); the discrete reply finalizes that bubble in place via `_finalize_live_on_send` (clean final, no duplicate). Engine is in the base `Surface`; Telegram supplies the transport primitives. **Opt-in:** intermediate frames are raw deltas (per-chunk brain-scrubbed, best-effort); the persisted final is clean. | `agents/task/surface_config.py::telegram_incremental_stream`, `core/surfaces/surface.py` |
| `TELEGRAM_STREAM_EDIT_INTERVAL_SEC` | `1.5` | Min seconds between live stream edits (flood-control); `0` edits on every delta. | `agents/task/surface_config.py::telegram_stream_edit_interval_sec` |
| `VOICE_TRANSCRIPTION_ENABLED` | OFF | Transcribe inbound voice/audio to text before routing (#9), so a voice note is handled like a typed message. Needs the faster-whisper extra; degrades to no-transcript if absent. | `agents/task/surface_config.py::voice_transcription_enabled`, `modules/transcription/` |
| `VOICE_TRANSCRIPT_ECHO` | ON | Echo the transcript back into the chat as a persistent, voice-note-anchored message (`🎙️ Transcript: "…"`) before the agent answers, on Telegram and WhatsApp. WhatsApp additionally marks the voice note read (✓✓). `false` = byte-identical prior behavior. | `agents/task/surface_config.py::voice_transcript_echo_enabled`, `core/surfaces/voice_echo.py` |
| `VOICE_TRANSCRIPTION_MODEL` | `base` | faster-whisper model size (`tiny`/`base`/`small`/`medium`/`large-v3`). | `agents/task/surface_config.py::voice_transcription_model` |
| `CORRESPONDENT_ACCESS_ENABLED` | OFF (ON under `AUTONOMY_MODE=autonomous`) | WS-A three-tier access model — the **single switch** for the whole feature (no sub-flags). When ON, `route_inbound` classifies each inbound as OWNER (steers), CORRESPONDENT (reply = DATA into the originating session via a `CORRESPONDENT`-origin control message, never a command), or DENIED; the tier block is **fail-CLOSED**; the local-owner bypass is surface-scoped to `{cli,local,repl}`; and the **capability gate** (deny high-impact tools while a session is correspondent-tainted) + the principal-awareness frame are registered. OFF ⇒ byte-identical legacy routing. | `agents/task/surface_config.py::correspondent_access_enabled`, `core/surfaces/{dispatcher,access}.py`, `agents/task/agent/core/correspondent_gate.py` |
| `CORRESPONDENT_REQUIRE_APPROVAL` | ON (`True`) (OFF under `AUTONOMY_MODE=autonomous`) | A newly auto-seeded correspondent is PENDING (owner must `polyrob owner approve`) before replies route. Set `false` for single-user/local. | `agents/task/surface_config.py::correspondent_require_approval`, `surfaces/email/seed.py` |
| `CORRESPONDENT_MAX_NEW_PER_DAY` | `20` | Per-tenant cap on new correspondents seeded per 24h (bounds injected mass-contact). Applies to genuinely NEW addresses only — idempotent re-seeds and thread-anchor rows (`provenance='thread'`) are exempt. Since 2026-07-13 the seed runs BEFORE the send and a cap-refusal **blocks** the outbound (no more orphaned replies). | `agents/task/surface_config.py::correspondent_max_new_per_day`, `core/surfaces/seed.py` |
| `OUTBOUND_POLICY` | `allowlist` (`open` under `AUTONOMY_MODE=autonomous`) | 013 T5 — pure outbound-policy model (`open\|domains\|allowlist\|off` ladder). `resolve_outbound_policy` merges env-or-mode-default with an optional guarded `outbound.policy` pref via the `stricter_policy` merge kind (pref can only TIGHTEN, never loosen). Model only in this task — enforcement at the send gates (`message_send.py`/`email_tool.py`) is a separate task. | `core/surfaces/outbound_policy.py::resolve_outbound_policy`, `core/surfaces/outbound_target.py::resolve_target_tier` |
| `OUTBOUND_DOMAINS` | unset (empty) | Comma-separated domain allowlist consulted when `OUTBOUND_POLICY=domains` (email-shaped targets only; other surfaces fall back to the exact-address allowlist). Merges with the guarded `outbound.domains` pref via `narrow_list` (allowlist polarity, T5 review fix): the pref can only NARROW this set (intersection) when the env var is set, or define it from scratch when the env var is empty — `union` would let a pref WIDEN past an operator restriction, which is wrong for an allowlist-shaped list. | `core/surfaces/outbound_policy.py::resolve_outbound_policy` |
| `OUTBOUND_DAILY_SEND_CAP` | `30` | Control-plane cap on outbound sends/day once policy is `open`/`domains` (mirrors the `USER_DELIVERY_DAILY_CAP` convention). Guarded pref `outbound.daily_send_cap` merges via `min`. Landed as a `core/prefs.py` row in this task; enforcement is a separate task. | `core/prefs.py::PREF_SCHEMA["outbound.daily_send_cap"]` |
| `CORRESPONDENT_TTL_DAYS` | `0` (never) | Days of inactivity before a correspondent binding is marked expired (stops resolving). `0` = never — expiry silently breaks reply routing for long-quiet contacts, so it is an explicit opt-in. When >0, the hourly surface GC ticker runs `purge_expired`. | `agents/task/surface_config.py::correspondent_ttl_days`, `core/autonomy_runtime.py::_build_surface_gc_ticker` |
| `CORRESPONDENT_RESOLVE_LATEST` | ON (`True`) | When ONE tenant has several active bindings for one address (multiple sessions contacted the same person) and a reply carries no usable thread anchor, route it to the most recently active binding instead of quarantining. Cross-tenant ambiguity ALWAYS denies regardless. `false` = legacy ambiguous→deny. | `agents/task/surface_config.py::correspondent_resolve_latest`, `core/surfaces/correspondents.py::resolve` |
| `CONVERSATION_RESUME_ENABLED` | ON (`True`) | When a correspondent replies to a conversation whose originating session is dead (missing/unrecreatable), create a replacement session seeded with the durable conversation context and re-point the bindings — instead of silently dropping their message (`correspondent_resumed` event). `false` = legacy drop+audit. | `agents/task/surface_config.py::conversation_resume_enabled`, `agents/task_agent_lite.py::_try_conversation_resume` |
| `CORRESPONDENT_REPLY_ENABLED` | OFF (ON under `AUTONOMY_MODE=autonomous`) | D1 scoped reply-while-tainted: permit `message`/`send_email` to EXACTLY the tainting (surface, address) — 1:1 only (multi-recipient/cc/bcc never exempt), rounds-capped, fail-closed. Default OFF keeps the owner-in-the-loop-per-round posture; every other high-impact tool stays blocked regardless. | `agents/task/surface_config.py::correspondent_reply_enabled`, `agents/task/agent/core/correspondent_gate.py::build_reply_allowed` |
| `CORRESPONDENT_REPLY_MAX_ROUNDS` | `5` | Outbound messages per correspondent per 24h allowed under the scoped tainted-reply exemption (counted from the ConversationStore). | `agents/task/surface_config.py::correspondent_reply_max_rounds` |
| `MAX_EPHEMERAL_MESSAGES` | `30` | Cap on the one-shot ephemeral injection queue (correspondent replies / recall notes); overflow drops the OLDEST with a WARN. Parity with the HITL queue's `MAX_QUEUED_MESSAGES` backpressure. | `agents/task/agent/messages/retrieval.py::push_ephemeral_message` |
| `EMAIL_SURFACE_ENABLED` | OFF (**ON for `polyrob email`**) (ON under `AUTONOMY_MODE=autonomous`) | Email surface (IMAP poll inbound + SMTP outbound). v1 is correspondent-only; owner-by-email stays OFF. | `agents/task/surface_config.py::email_surface_enabled`, `surfaces/email/harness.py` |
| `EMAIL_IMAP_POLL_SEC` | `60` | Seconds between IMAP polls for new mail (no IDLE in v1). | `agents/task/surface_config.py::email_imap_poll_sec` |
| `EMAIL_AUTONOMY_RUNTIME` | OFF | Proposal 010 A: whether `polyrob email` starts the shared autonomy runtime (goal dispatcher + cron ticker + curator + surface GC). Default OFF — the telegram process is the single autonomy driver, so a telegram-outbound goal can never be claimed by the email-only process (whose `MessageRouter` has no telegram surface → deterministic send failure). `true` restores the legacy dual-runtime behavior. SMTP outbound (cron `deliver=email` / the agent's `send_email` tool) is credential-driven and does not need this runtime. | `cli/commands/email.py::_run_email` |
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
| `OWNER_DOC_WRITABLE` | OFF (**ON under POLYROB_LOCAL**) | Agent can maintain a bounded owner-facts doc (`owner.md`, ≤1600 chars) injected on the SELF/SOUL seam — durable facts/preferences about the owner. Same quarantine-then-promote + identity-scan model as SELF-context. Agent action `owner_doc_manage`. | `agents/task/constants.py::AutonomyConfig.owner_doc_writable`; `core/owner_doc_writer.py` |
| `OWNER_DOC_REQUIRE_REVIEW` | ON (`True`) | Owner-facts writes go to `.pending/` review (owner promotes via `/pending` or `polyrob owner`). | `agents/task/constants.py::AutonomyConfig.owner_doc_require_review` |
| `DATA_ROOT` | `./data/task` | Server-bootstrap (`build_bot`/`main.py`) data root for sessions/dbs. Distinct from the CLI's `POLYROB_DATA_DIR`/`./.polyrob` default (see Identity/local-profile above). | `os.getenv("DATA_ROOT","./data/task")` (`core/bootstrap.py:180`) |
| `DB_PATH` | unset (derived: `<data_dir>/database/bot.db`) | Explicit `bot.db` location (R-2 B2 made it REAL — historically a decoy the app never opened). Unset = the historical derivation, following the CLI's post-construction `data_dir` reassignment. Set = honored, with a refuse-to-guess guard: if the derived location still holds the real DB and the configured path doesn't exist, startup raises instead of opening a fresh empty DB — move the file first. `polyrob update` snapshots trust this same value. | `modules/database/database_manager.py::resolve_bot_db_path` |
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

## Preferences (owner UX)

Typed, per-tenant preferences layer (owner-UX Phase 1) stored at
`identity/{instance_id}/user_{uid}/preferences.toml`, resolved as
pref > env > default — except guarded keys, which merge most-restrictive
(min/union/AND/stricter-provider) so a preference can tighten operator policy
but never widen it. No file present == byte-identical legacy behavior. See
`core/prefs.py` module docstring for the full schema/merge contract.

| Flag | Default | What it does | Code anchor |
|---|---|---|---|
| `PREFS_ENABLED` | ON | Master switch for the whole preferences layer; when OFF, `resolve_with_source` always returns the env/default value and `preferences.toml` is never read. | `core/prefs.py::prefs_enabled` |
| `PREFS_TOOL_ENABLED` | OFF (**ON under POLYROB_LOCAL**) | Agent-callable `preferences(operation, key?, value?, text?)` action — `list`/`get` read the typed schema (effective value/source/applies); `set` writes SAFE keys immediately or queues a pending proposal for GUARDED keys (owner reviews with `/pending`); `contract_propose` proposes durable operating-contract rules via `ContractWriter`. A forged/autonomous turn (self-wake/delegation-result re-entry, sub-agent/leaf, or an autonomous run) has `set` refused outright (a SAFE pref has no quarantine step of its own); `contract_propose` instead forces `PROVENANCE_BACKGROUND` (always quarantined). A leaf/sub-agent never even sees the tool (excluded via `delegation_exclusions_for_child`), and a correspondent-tainted session is denied the whole action. | `agents/task/constants.py::prefs_tool_enabled`; `tools/controller/action_registration.py::_register_preferences_action` |
| `CONTRACT_DOC_ENABLED` | ON | Injects the owner-authored operating-contract doc (`contract.md`, written via `ContractWriter`/the `preferences(contract_propose)` action) plus a deterministic one-line style summary from typed prefs (`core/prefs.py::render_style_line`) as a frozen `## Operating contract` block, read once at session start alongside SOUL/owner-facts/SELF. No file present and no style prefs set == `""` == byte-identical to legacy. The file is protected from direct agent writes by the secret-guard (see Identity/local-profile group above) — the only write path is the reviewed `contract_propose` proposal flow. | `agents/task/constants.py::contract_doc_enabled`; `agents/task/agent/core/construction.py`; `core/contract_writer.py::ContractWriter` |
| `CONTRACT_DOC_REQUIRE_REVIEW` | ON | Whether a `contract_propose` write lands as a pending proposal (owner reviews/promotes with `/pending` + `/approve`) rather than activating immediately. A forged/background author (self-wake, sub-agent/leaf, autonomous run) is **always** quarantined regardless of this flag — only a genuine owner turn can bypass review when it's set OFF. | `agents/task/constants.py::contract_doc_require_review`; `core/contract_writer.py::ContractWriter._resolve_pending` |

---

## Billing / x402 / wallet

> ⚠️ **Unaudited — use at your own risk.** The wallet, signing, x402, and crypto-trading code
> below has had **no independent security audit**. It ships as-is with no warranty and can lose
> funds. All of it is **OFF by default**; enable only if you accept that risk, and prefer
> testnets. See [SECURITY.md](../SECURITY.md#crypto--wallet--payment-features).

| Flag | Default | What it does | Code anchor |
|---|---|---|---|
| `BILLING_FAILOVER_ENABLED` | **ON** | Attempt provider fallback on a billing/quota/402 error before permanent halt. Detection is the SSOT `core/credit_sentinel.py::looks_like_credit_death` (I-5, 2026-07-10 — the old ad-hoc substring check missed real-shape 402s). On a single-funded-provider deployment there is nothing to fall back *to* until a second provider key is funded (`docs/proposals/008-llm-provider-fallback-goal-resilience.md`). | `agents/task/agent/core/error_recovery.py:48` |
| `CREDIT_SENTINEL_ENABLED` | **ON** | §6.3 provider-credit sentinel: a credit-death refusal (402/insufficient_quota/billing) from an autonomous goal/cron run sends ONE safety-net notice via the delivery rail and pauses goal dispatch + LLM cron ticks via a durable latch file (`<data_root>/CREDIT_SENTINEL`) — ends the silent multi-day 402 grind. $0 ticks (digest, `wake_agent=false`) keep flowing. | `core/credit_sentinel.py::credit_sentinel_enabled` |
| `CREDIT_SENTINEL_RELEASE_HOURS` | `6` | Auto-release window for the credit sentinel — after this many hours the latch expires and one paid probe may run (re-tripping if credits are still dead). Remove the latch file to release early. | `core/credit_sentinel.py::_release_hours` |
| `LLM_OUTAGE_NOTICE` | **ON** | Proposal 015 #2: when an owner-facing chat-surface turn dies with ALL LLM providers exhausted (e.g. the 2026-07-16 OpenRouter 402 cascade), send ONE static, LLM-independent ⚠️ notice back over the originating surface instead of total silence. Classification reuses the credit-death SSOT plus provider-exhaustion strings; cooldown = at most one notice per surface+chat per 30 min (in-process timestamp map — a restart may allow one extra, the safe direction). Structurally never fires for goal/cron/background runs (the send-site is the chat-surface post-run deliver seam only). Default-ON is deliberate — an outage notice that defaults OFF would never fire (precedent: `UNTRUSTED_TOOL_RESULT_WRAP`). | `core/surfaces/llm_outage_notice.py::llm_outage_notice_enabled`; send-site `surfaces/telegram/harness.py::_run_and_deliver` |
| `X402_ENABLED` | OFF (`'false'`) | Enable x402 pay-per-request *receiving*. | `os.environ.get("X402_ENABLED","false")` |
| `X402_CLIENT_ENABLED` | OFF (`'false'`) | Enable the agent-side x402 *paying* tool. | `core/wallet/config.py:46`, `os.getenv("X402_CLIENT_ENABLED","false")` |
| `X402_PAYMENT_RECIPIENT` | `''` | Treasury/recipient address for x402 receipts. | `os.environ.get("X402_PAYMENT_RECIPIENT",...)` |
| `X402_INVOICE_ENABLED` | OFF (ON under `AUTONOMY_MODE=autonomous`) | Agent money loop: registers the `x402_invoice` tool (`x402_request` create-invoice + `x402_invoices` list + `accounting` unified ledger) AND starts the settlement watcher in the autonomy runtime. Needs `X402_PAYMENT_RECIPIENT`. `x402_request` is in the recommended approval set; leaf sub-agents never get the tool. Settle via `polyrob owner settle <id>`. | `tools/x402/__init__.py::register_x402_invoice_tool`; `modules/x402/invoicing.py`; `core/autonomy_runtime.py` |
| `X402_INVOICE_MAX_USD` | `50` | Hard ceiling on a single agent-created payment request (an absurd invoice is a reputation incident). | `modules/x402/invoicing.py::invoice_max_usd` |
| `X402_INVOICE_DAILY_MAX` | `10` | Max invoices one tenant may create per trailing 24h. | `modules/x402/invoicing.py::invoice_daily_max` |
| `X402_PUBLIC_RATE_PER_WINDOW` | `20` | G-20: per-IP call budget for the PUBLIC (anon-allowed, enumerable `inv_<12hex>` id) invoice endpoints — `GET /api/x402/requests/{id}` and `POST /api/x402/requests/{id}/pay`. Reuses the existing generic sliding-window limiter (`tools/mcp/rate_limit.py::MCPExecRateLimiter`, same mechanism as `MCP_EXEC_RATE_PER_WINDOW`) rather than a new one; keyed per `(endpoint, client_ip)` so a real payer polling their OWN invoice a few times stays well under budget while enumeration/spam from one IP is throttled. Exceeding it returns HTTP 429 with a `Retry-After` header. | `api/x402_endpoints.py::_enforce_public_invoice_rate_limit` |
| `X402_PUBLIC_RATE_WINDOW_SEC` | `60` | Window (seconds) for `X402_PUBLIC_RATE_PER_WINDOW`. | `api/x402_endpoints.py::_enforce_public_invoice_rate_limit` |
| `X402_TRUSTED_PROXIES` | `''` (loopback `127.0.0.1`/`::1` always trusted) | ⚠️ **Security fix (G-20 regression, reproduced live):** the public invoice rate limiter above was keyed on `get_client_ip`, which returns a client-supplied `X-Forwarded-For` VERBATIM — spoofable, letting an attacker either evade the cap (rotate a fake XFF per request) or poison a victim payer's bucket (spoof the victim's IP to burn their budget and 429-lock them out). Fixed by keying on `get_trusted_client_ip` instead: if `request.client.host` (the real, un-spoofable TCP peer) is a trusted proxy — loopback by default, plus any CSV-listed IP here for a multi-hop chain (e.g. a CDN in front of nginx) — `X-Forwarded-For` is walked from the RIGHT past trusted-proxy hops to recover the real client (nginx's `$proxy_add_x_forwarded_for` APPENDS the address it directly observed, so the rightmost non-proxy entry is the one genuine fact in the chain); otherwise the header is IGNORED ENTIRELY and the raw peer is used. Only widen this beyond loopback if you run an additional trusted reverse-proxy hop. | `api/dependencies.py::get_trusted_client_ip`, `_trusted_proxy_set` |
| `USAGE_INVOICE_BRIDGE_ENABLED` | OFF | Task 13 (Phase 3 R3), the metering→invoice bridge: registers the read-only `usage_summary` agent action (tenant-scoped LLM usage rollup — api_cost_usd/credits/calls, optionally narrowed to a `session_id`/`since` — plus a non-binding `suggested_invoice` draft). Fixes G-29 (`LLMUsageTracker.get_session_breakdown` aggregates by `session_id` ALONE, no `user_id` filter — a cross-tenant read hole); `usage_rollup` REQUIRES `user_id`. The action NEVER creates a payment request itself — the agent must still call the separate, approval-gated `x402_request` action to actually invoice. Deliberately NOT in the `_SAFE_LOCAL_FLAGS` group (an explicit billing feature, never auto-on under `POLYROB_LOCAL`); added to the correspondent-taint high-impact set (same reasoning as `agent_status` — cost/invoice data must not leak to a tainted session). | `modules/credits/usage_rollup.py::usage_invoice_bridge_enabled`, `usage_rollup`, `build_invoice_draft`; `tools/controller/action_registration.py::_register_usage_summary_action` |
| `USAGE_INVOICE_MARKUP` | `1.0` | Multiplier applied to a rollup's `api_cost_usd` when `build_invoice_draft` proposes a `suggested_invoice` amount (`1.0` = passthrough, no markup). A distinct flag from `PRICING_MARKUP` (`modules/credits/pricing.py`) — that one prices internal platform credit-billing; this one prices an outbound agent-to-payer invoice for measured usage. The draft respects `X402_INVOICE_MAX_USD`: an over-cap amount is returned UNCLAMPED with `over_cap=true` (flagged, never silently clamped) so the agent can narrow scope or ask the owner to raise the cap. | `modules/credits/usage_rollup.py::usage_invoice_markup`, `build_invoice_draft` |
| `INVOICE_CARD_ENABLED` | OFF (**ON under POLYROB_LOCAL**) | Task 6 (Phase 1): after a successful `x402_request`, also render a branded PNG invoice card (Mindprint avatar, amount, purpose, request id, expiry, "billed to" when `payer_contact` is present, QR, pay instructions, footer) into the session workspace (`<workspace>/invoices/invoice_<request_id>.png`) and append `invoice card: <path>` to the action result. Pure Pillow (no headless browser); fail-open — any render error (missing flag aside) logs one WARN and returns the text-only result unchanged, since the invoice itself already succeeded. | `agents/task/constants.py::invoice_card_enabled`; `tools/x402/invoice_tool.py::_maybe_render_invoice_card`; `modules/pfp/cards.py::render_invoice_card` |
| `INVOICE_QR_STYLE` | `address` | What the invoice card's QR code encodes (via `modules/x402/artifact.py::build_payment_artifact`): `address` — the bare treasury address string (works with any wallet's "scan an address" import); `eip681` — an EIP-681 USDC transfer URI (`ethereum:<usdc_contract>@<chain_id>/transfer?address=<treasury>&uint256=<atomic_amount>`, pre-fills the amount for wallets that support it). USDC contract addresses reuse the SAME constants `core/wallet/onchain.py` trusts for on-chain balance reads (no second copy to drift); an unrecognized value falls back to `address`. | `modules/x402/artifact.py::invoice_qr_style` |
| `X402_SETTLEMENT_WATCH_INTERVAL_SEC` | `60` | Settlement-watcher tick interval (expire stale invoices; wake the originating session via self-wake when one settles; emits `payment_settled`/`payment_expired`). | `modules/x402/settlement_watcher.py::build_settlement_watcher` |
| `X402_SETTLE_ONCHAIN_DETECT` | OFF | Task 11 (Phase 2), the "de-Coinbase" move: the settlement watcher ADDITIONALLY scans the treasury address for plain USDC `Transfer` logs (no facilitator, no `POST /pay`) and auto-settles the matching PENDING agent invoice by exact amount — so a human payer who just sends USDC to the address the invoice instructions show gets detected instead of the invoice silently expiring. Inert unless the configured chain is mainnet (`base`) AND a treasury (`X402_PAYMENT_RECIPIENT`) is set. Equal-amount pending invoices are matched oldest-first; a transfer matching no invoice emits one `payment_unmatched` event and settles nothing. Reuses the SAME Base RPC + USDC contract constant `core/wallet/onchain.py` already trusts for balance reads (no second copy). Turning this on also FORCES `X402_INVOICE_AMOUNT_JITTER` behavior on (see that row, I2 fix). ⚠️ **C2 replay/isolation fix (Task 11 review):** each detected transfer is settled in its OWN try/except in `_settle_or_flag` — one bad transfer can never block the rest of the batch or the scan-checkpoint advance. A given on-chain `transaction_hash` settles AT MOST ONE invoice EVER: `settle_payment_request` refuses a re-used tx hash (`invoicing.transaction_hash_already_settled`), backstopped by a partial UNIQUE index on `x402_payment_requests.transaction_hash` (self-healed in `X402Tables.create_tables`, tracked by migration v1.6.0) — closes the class of bug where a mid-batch failure leaves the checkpoint un-advanced and a re-scanned/consumed transfer settles a DIFFERENT same-amount invoice on a later tick. | `modules/x402/onchain_probe.py`; `modules/x402/settlement_watcher.py::SettlementWatcher._scan_onchain`, `_settle_or_flag`; `modules/x402/invoicing.py::x402_settle_onchain_detect_enabled`, `transaction_hash_already_settled`, `settle_payment_request` |
| `X402_INVOICE_AMOUNT_JITTER` | **ON** | Amount-collision jitter for on-chain matching: when a colliding equal-amount PENDING invoice already exists for the same treasury, `create_payment_request` nudges the new amount by a deterministic sub-cent increment (never exceeding `X402_INVOICE_MAX_USD`) so on-chain amount-matching stays unambiguous. **Inert unless `X402_SETTLE_ONCHAIN_DETECT` is also on** — with detection off this flag does nothing (byte-identical legacy amounts). ⚠️ **I2 safety fix (Task 11 review):** this flag can no longer DISABLE jitter while detection is ON — that combination (auto-settlement with zero disambiguation) is unsafe, so jitter is FORCED on internally in that case (one WARN log per create) regardless of this flag's value. Setting it `false` only has effect while detection is also off (where it was already inert). The collision-check + insert is also now serialized per-treasury (in-process `asyncio.Lock`) so two concurrent creates at the same colliding amount can never both keep the unjittered amount (I1 fix). The payer-facing amount is rendered at FULL precision (never truncated to 2dp) whenever it carries sub-cent jitter — `tools/x402/invoice_tool.py`'s `x402_request` result, `modules/x402/artifact.py`'s `pay_text`, and the invoice-card PNG (`modules/pfp/cards.py`) all use the shared `format_invoice_amount` (C1 fix); the invoice card's QR additionally prefers `eip681` (exact atomic amount) over the bare-address default when detection is on and `INVOICE_QR_STYLE` is unset. | `modules/x402/invoicing.py::x402_invoice_amount_jitter_enabled`, `_jitter_should_apply`, `_dedupe_amount_for_treasury`, `_treasury_lock`; `modules/x402/artifact.py::format_invoice_amount`, `invoice_qr_style` |
| `X402_SETTLEMENT_SCAN_MAX_SPAN` | `5000` | Max blocks one settlement-watcher tick will scan for on-chain detection, even after a long gap (bounds a single `eth_getLogs` call). Only consulted when `X402_SETTLE_ONCHAIN_DETECT` is on. | `modules/x402/settlement_watcher.py::_scan_max_span` |
| `X402_SETTLEMENT_CONFIRMATIONS` | `2` | Confirmations buffer for on-chain detection — blocks within this many of the chain head are never scanned yet (a just-mined block can still reorg out). Only consulted when `X402_SETTLE_ONCHAIN_DETECT` is on. | `modules/x402/settlement_watcher.py::_scan_confirmations` |
| `SUBSCRIPTIONS_ENABLED` | OFF | Task 14 (Phase 3 R5), the first revenue wedge: watchtower subscriptions — a prepaid-period + renewal-invoice model gating a cron job's continued firing. Driven entirely from the EXISTING settlement-watcher tick (no new ticker): each tick creates renewal invoices ahead of `paid_through` (respecting `PAYMENT_APPROVAL_MODE` — `auto` invoices immediately, `approve` queues a durable owner `tool_approval` ask and invoices once approved on a later tick), moves a lapsed subscription `active -> grace -> suspended`, and (on settlement) extends `paid_through` via `modules/x402/subscriptions.py::apply_settlement` (idempotent, keyed on the invoice `request_id`). `cron/runner.py` $0-skips (`subscription_lapsed`) a job whose `payload.subscription_id` resolves to a `suspended`/`canceled` subscription; `active`/`grace` still run (grace keeps delivering while a renewal is chased). Off by default: the subscriptions table is untouched, the watcher does no subscription processing, and the cron gate never queries it. Owner CLI: `polyrob owner sub list` / `polyrob owner sub cancel <id>`. | `modules/x402/subscriptions.py::subscriptions_enabled`; `modules/x402/settlement_watcher.py::SettlementWatcher._process_subscriptions`; `cron/runner.py::make_agent_runner`; `cli/commands/owner.py::sub` |
| `WATCHTOWER_PRICE_USD` | `10.00` | Default monthly price (USD) for a watchtower subscription — `create_subscription` falls back to this when no explicit `amount_usd` is given. | `modules/x402/subscriptions.py::watchtower_price_usd` |
| `SUBSCRIPTION_RENEWAL_LEAD_DAYS` | `5` | Days before a subscription's `paid_through` the settlement watcher creates the next renewal invoice. | `modules/x402/subscriptions.py::subscription_renewal_lead_days` |
| `SUBSCRIPTION_GRACE_DAYS` | `3` | Days past `paid_through` a lapsed subscription keeps its cron job running (status `grace`, still chasing renewal) before it is `suspended` (cron job $0-skips). | `modules/x402/subscriptions.py::subscription_grace_days` |
| `X402_DEFAULT_CHAIN` | `base` | Default chain for x402. | `os.environ.get("X402_DEFAULT_CHAIN","base")` |
| `X402_FACILITATOR_URL` | `''` | x402 facilitator endpoint. | `os.environ.get("X402_FACILITATOR_URL","")` |
| `X402_PRICE_USD` | _derived_ (unset ⇒ economics-based, ~$30) | Single-source x402 per-request price (C2 SSOT) — the middleware charge, `/api/x402/pricing`, the Agent Card, and the 402 challenge all read this. **If set, it wins.** If unset (or invalid), the price is DERIVED (S6): `X402_MAX_TOKENS_PER_REQUEST × max-model-output-rate × X402_PRICE_MARKUP` — the worst-case cost of the budgeted tokens, marked up, since x402 settles before the run. | `modules/x402/x402_integration.py` `get_x402_price_usd()` |
| `X402_MAX_TOKENS_PER_REQUEST` | `200000` | Token budget one x402 request prepays for. **SSOT for BOTH the derived price AND the runtime hard cap** — `LLMUsageTracker` halts an x402 request (`InsufficientCreditsError`) once its per-session cumulative tokens exceed this, so actual cost can never exceed what was prepaid (S6 cost-amplification fix). Admin tier is exempt + uncapped. | `modules/x402/x402_integration.py` `get_x402_max_tokens_per_request()` + `modules/credits/usage_tracker.py` `_enforce_x402_budget()` |
| `X402_PRICE_MARKUP` | `2.0` | Safety multiplier on the derived x402 price (margin over worst-case token cost). Ignored when `X402_PRICE_USD` is set. | `modules/x402/x402_integration.py` `get_x402_price_usd()` |
| `AGENT_WALLET_ENABLED` | OFF | Enable the agent's native wallet. | `core/wallet/config.py:45` |
| `AGENT_WALLET_NETWORK` | `testnet` | Wallet network (`testnet`/`mainnet`). | `core/wallet/config.py:43` |
| `AGENT_WALLET_BACKEND` | `local_eoa` | Wallet key backend. | `core/wallet/config.py:47` |
| `AGENT_WALLET_MAX_PER_TX_USD` | `1000` | Per-transaction USD ceiling (catastrophic-loss guard, NOT a budget). Hardened down from `1000000` — a typo could drain funds; raise explicitly if needed. | `core/wallet/config.py:85` |
| `AGENT_WALLET_OPERATIONAL_VENUE` | `treasury` | Venue key that same-chain spend paths (x402, generic payments) SIGN with, so the funded address (`AgentWallet.address`) equals the spent-from address. "Venue" elsewhere stays a policy/accounting label; hyperliquid keeps its own delegated key. | `core/wallet/config.py::load_wallet_config` |
| `AGENT_WALLET_DERIVATION` | unset (`meta.json` wins; absent = legacy) | Recovery-hatch override for the wallet's key-derivation scheme (`legacy` \| `bip44`). Normally the scheme is recorded ONCE, write-once, in `<data-home>/wallet/meta.json` (data-home = `POLYROB_DATA_DIR`/`DATA_ROOT` if set, else `resolve_data_home()` — `cwd/.polyrob` in local mode) by `polyrob wallet init`/import — a wallet created before this metadata existed has no file and is legacy FOREVER (an existing wallet's addresses must never change). Only set this env var to recover from a corrupted/missing meta file; it is never inferred from the seed's shape. | `core/wallet/derivation.py::resolve_scheme`, `wallet_meta_path` |
| `WALLET_DAILY_CAP_USD` | unset (disabled) | Rolling 24h spend cap; unset = per-tx ceiling only. | `core/wallet/config.py:52` |
| `CREDIT_VALUE_USD` | `0.01` | USD value of one credit. | `os.environ.get("CREDIT_VALUE_USD","0.01")` |
| `WELCOME_BONUS` | `100` | New-user credit grant. | `os.environ.get("WELCOME_BONUS","100")` |
| `EIP8004_ENABLED` | OFF (`'false'`) | Enable EIP-8004 on-chain agent registration. | `api/app.py:702`, `modules/eip8004/registration.py:19` |
| `EIP8004_PAYMENT_FEEDBACK` | OFF | Task 15 (Phase 4), the ERC-8004 ⇄ x402 compose seam: on settlement of an invoice with an identifiable payer (`correspondent_ref`, an ACTIVE correspondent channel) AND a verifiable on-chain transaction hash, offer that payer a signed ERC-8004 feedback authorization + `ProofOfPayment` (built via `modules/eip8004/payment_proof.py::proof_from_settled_invoice`) they can redeem later. Never auto-submits feedback on the payer's behalf (only creates the redeemable authorization + best-effort delivers the offer as correspondent DATA, never the owner rail). Rides `EIP8004_ENABLED` (checked the same strict `'true'`-string way `get_eip8004_config`/`require_eip8004_enabled` do); a settlement with no tx hash (e.g. a manually attested `settled_no_tx` invoice) has nothing to prove and is skipped. Fail-open: any error in this hook never blocks the settlement wake/notify path. Separately, `ReputationManager.submit_feedback` verifies ANY submitted `proof_of_payment` against the real x402 ledger (`modules/x402/invoicing.py::get_payment_request_by_tx_hash`) before accepting it as verified: (1) the tx must reference a real, SETTLED payment request (a non-existent or non-settled reference is rejected, `ValueError`); (2) the proof's `toAddress` must case-insensitively match the settled invoice's own `recipient` — a proof referencing a real settlement that reached a DIFFERENT treasury is rejected, and an absent/empty `toAddress` is rejected too (strict — the sole proof-building path always populates it); (3) one-proof-one-feedback replay guard — a given `(agentId, txHash)` may back at most one feedback submission; a settled tx replayed against the same `agentId` a second time is rejected (`ValueError`, in-memory guard scoped to the process-lifetime `ReputationManager` instance, mirroring `_feedback_cache`'s existing in-memory maturity). Feedback with no proof is unaffected by any of the above. **HONEST GUARANTEE — read before flipping this ON:** `verified_purchase: True` means "a real, settled, non-replayed x402 payment that reached this agent's treasury is referenced" — it does **NOT** mean "the author of this feedback IS the payer." x402 invoices do not record a payer wallet address, so payer IDENTITY is never cryptographically bound to the feedback submitter; whoever first learns a settled `txHash` can redeem it. Do not treat `verified_purchase` as sybil-proof author identity — it closes the "a real payment happened and can't be endlessly reused" gap, not the "this feedback was written by the person who paid" gap. | `agents/task/constants.py::eip8004_payment_feedback_enabled`; `modules/x402/settlement_watcher.py::SettlementWatcher._maybe_offer_payment_feedback`; `modules/eip8004/reputation.py::ReputationManager._verify_payment_proof`, `ReputationManager.submit_feedback` |
| `EIP8004_ONCHAIN_ENABLED` | OFF (`'false'`) | Trust-mode claim only — flips the publicly-served `/eip8004/registration.json` `trustMode` from `local` (honest off-chain/simulation) to `onchain`, and (only when `EIP8004_AGENT_ID`/`EIP8004_IDENTITY_REGISTRY` are also set) emits a `registrations[]` entry attesting an on-chain Identity Registry registration. **No code in this repo ever signs or broadcasts an Identity Registry registration** — this flag is purely a public claim the *operator* is making; the emitted registration is stamped `attestation: "operator"` so a consumer can't read it as code-verified. Turning it on without an operator having actually registered on-chain is a false public claim (L11). | `modules/eip8004/registration.py:125-138` |
| `EIP8004_AGENT_PRIVATE_KEY` | unset — **SECRET, mask in logs/exports** | Signing key for ERC-8004 EIP-712 feedback authorizations (`ReputationManager`). This is a **second, independent signing key** — it is NOT derived from `AGENT_WALLET_MASTER_SEED` and has no relationship to the agent wallet's PBKDF2 derivation tree. Consequence: the wallet migration procedure (`polyrob wallet export`/`init --from-mnemonic`/`--from-seed`, see `docs/guide/payments.md` §2.3) moves the agent's money wallet only — it does **not** carry this key. Migrating the wallet to a new box without separately copying `EIP8004_AGENT_PRIVATE_KEY` silently drops the 8004 signing identity (subsequent `submit_feedback` calls fail closed with "EIP8004 signing unavailable" rather than silently misbehaving, but the identity itself is lost unless you copy the env var yourself). | `core/config.py:850`, `modules/eip8004/reputation.py:141-144` |
| `ETH_PRICE_USD_OVERRIDE` | unset | Fixed ETH/USD price for the deposit monitor — bypasses the live CoinGecko fetch entirely. For ops/testnets with no real price feed; never set on a mainnet deployment (it would misprice real deposits). | `modules/payments/price_oracle.py::get_eth_price_usd` |
| `ETH_PRICE_USD_MAX` | `50000` | Sanity upper bound on the ETH/USD price (override or live fetch); a price above this raises instead of crediting a wildly-inflated deposit — guards against an oracle schema error or an `ETH_PRICE_USD_OVERRIDE` typo. | `modules/payments/price_oracle.py::get_eth_price_usd` |
| `DEPOSIT_MONITOR_ENABLED` | OFF | Enable the deposit-monitoring background loop (requires `SEPOLIA_RPC_URL`/`ETHEREUM_RPC_URL` too). | `core/initialization.py:648`, `core/config.py:782` |
| `TREASURY_SWEEPER_ENABLED` | **OFF** | Master gate for `TreasurySweeper` — the SaaS-deposit-side component that signs and broadcasts real fund-moving transactions, sweeping per-user deposit-address balances into the treasury. Before this flag existed the sweeper started on **config presence alone** (`TREASURY_ADDRESS` + a resolved master seed + `ENABLE_AUTH`) with no dedicated kill-switch and no catalog row (M20) — a real risk on any box that happens to carry a legacy `.env` with those three set. Now it additionally requires `TREASURY_SWEEPER_ENABLED=true` (parsed via the canonical falsey-set `core/env.py::bool_env`); with it off, config presence alone only logs "treasury sweeper disabled" and starts nothing. | `core/initialization.py::_treasury_sweeper_enabled`, `initialize_auth_services` (~:690-719) |
| `TREASURY_ADDRESS` | unset | Gnosis Safe / treasury wallet address the sweeper moves swept funds to. One of the three config-presence conditions `TreasurySweeper` checks — inert on its own without `TREASURY_SWEEPER_ENABLED=true` and a resolved master seed. | `core/config.py:800` |
| `SWEEP_INTERVAL` | `3600` (1h) | Poll interval (seconds) between `TreasurySweeper` sweep passes, once enabled. | `core/config.py:807` |
| `ENABLE_AUTH` | OFF | **The real billing gate.** `core/initialization.py::initialize_auth_services()` (called unconditionally from `core/bot.py` on every boot) returns immediately when this is `False`, *before* `balance_manager`/`tier_manager`/`api_key_manager`/`wallet_generator`/the deposit monitor are ever registered on the container — so with it off, no billing service exists at all (not just "routes 404"). Independent of `ENABLE_CREDIT_SYSTEM` (defaults `True` but is only consulted *inside* this same gate, so it's inert whenever `ENABLE_AUTH` is off) and of `X402_ENABLED` (separately gates `X402PaymentMiddleware`). | `core/config.py:780` (`BotConfig.enable_auth`), `core/initialization.py::initialize_auth_services` |
| `PAYMENT_MASTER_SEED` / `MASTER_SEED` | unset | Master seed for deterministic per-user deposit-address derivation. Two names have existed historically (`PAYMENT_MASTER_SEED` is the documented convention; `MASTER_SEED` is a legacy pydantic-settings alias) — `resolve_master_seed()` is the SSOT: **`PAYMENT_MASTER_SEED` wins if set, else `MASTER_SEED`, else `None`** (unset ⇒ deposit-address generation disabled, logged as a warning, not a crash). Distinct from `AGENT_WALLET_MASTER_SEED` (the agent's own outbound wallet, `core/wallet/config.py`). | `core/payment_config.py::resolve_master_seed` |

> **"Byte-identical at defaults" is functionally true, not literally true** (2026-07-15 review §2,
> last row). With every money flag OFF, no money code ever *executes* and no money value can ever
> *move* — but four things are present on disk/wire regardless of flag state:
> 1. The empty `x402_payment_requests` and `subscriptions` tables are always created at DB init
>    (`modules/database/database_manager.py:83-84`), never populated while their flags are off.
> 2. `GET /api/x402/*` (info-only route, self-labels `payments {'enabled'/'info-only'}`) is always
>    mounted (`api/app.py`, "always register — endpoints self-check if x402 is configured").
> 3. `/eip8004/registration.json` + the reputation/validation reads are always mounted and served
>    (`api/app.py`), reporting `trustMode: "local"` and empty reputation when `EIP8004_ENABLED` is
>    off — a read, not a write.
> 4. `/api/payments/*` is always mounted (`api/app.py`); each handler self-checks
>    `container.get_service('balance_manager')` at request time and 404s/503s when `ENABLE_AUTH` is
>    off, rather than being absent from the route table.
>
> None of the four can move value or register a service — the "no money code runs" guarantee holds
> — but a port scan / route enumeration will see these paths respond even on a deployment that has
> enabled nothing. Treat "byte-identical" as "functionally inert," not "the process looks identical
> on the wire."

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
| `TWITTER_ENABLED` | OFF (`'false'`) (ON under `AUTONOMY_MODE=autonomous`) | Enable the Twitter/X write surface. | `tools/twitter_tool.py::twitter_write_enabled` |
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
| `POLYROB_PLAIN` | OFF | Force the plain (non-Rich) CLI renderer; mirrors `--plain` (the flag wins). Render toggle only — does not imply non-interactive. | `cli/commands/run.py:163` |
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
- `OWNER_DOC_WRITABLE`
- `BACKGROUND_REVIEW_ENABLED`
- `GOALS_ENABLED`
- `CURATOR_ENABLED`
- `INSIGHTS_TOOL`
- `AGENT_STATUS_TOOL`
- `VERIFY_BEFORE_DONE`
- `CODING_TOOLS_ENABLED`
- `EPISODIC_MEMORY_ENABLED`
- `EPISODIC_DIGEST_INJECT`
- `CONTINUITY_BRIDGE_ENABLED`
- `SELF_EVOLUTION_TRANSPARENCY`
- `PREFS_TOOL_ENABLED`
- `INVOICE_CARD_ENABLED`

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

| Flag | Default | What it does | Code anchor |
|------|---------|--------------|-------------|
| `WEB_FETCH_ALLOW_PRIVATE_URLS` | OFF | When true, `web_fetch` skips SSRF validation (allows loopback/private/metadata targets). Single-user/local dev ONLY — never enable in multi-tenant prod. When false (default), every redirect hop is re-validated and the connection is pinned to the validated IP (`tools/web_fetch/fetcher.py::safe_fetch`). | `tools/web_fetch/tool.py::_allow_private_urls` |

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
| `PFP_PUSH_DISCORD` | OFF | Allow `polyrob pfp push --discord` to set the Discord bot avatar (`PATCH /users/@me`, `DISCORD_BOT_TOKEN`; hash-idempotent, fail-open). | `modules/pfp/push.py` |

The frozen identity blob (`avatar/config/rob.json`; runtime `<home>/identity/{instance_id}/pfp/pfp.json`)
is the reproducibility SSOT — picture traits **and** the engine-agnostic voice signature
(`{pitch,rate,timbre}`) the future voice-interface app consumes. The still PNG is rendered headlessly
via the `[browser]` extra (Playwright/Chromium); when that is unavailable, `pfp generate` falls back to
the committed `avatar/renders/rob.png`. The CLI renders the face LIVE from the pure-Python field port
(`modules/pfp/mesh.py`) — no PNG, no Chromium.
