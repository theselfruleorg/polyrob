"""
Centralized constants for the Task agent system.
This provides a single source of truth for history management, trimming, and memory limits.
"""

import os

from core.env import bool_env as _core_bool_env

# Token counting defaults - Use modules.llm.token_counter for actual counting
# IMG_TOKENS kept for backward compatibility but should use token_counter._count_multimodal_tokens
IMG_TOKENS = 800  # Default estimate for image tokens (actual counting done by modules.llm)
DEFAULT_IMG_TOKENS = IMG_TOKENS  # Alias for compatibility
ESTIMATED_CHARS_PER_TOKEN = 4  # Updated to match model_registry defaults

# Default user ID for anonymous sessions. The canonical token lives in
# core.identity (the tenant SSOT); aliased here so the value is defined once.
from core.identity import ANON_USER_ID as DEFAULT_USER_ID  # noqa: E402

# Token recalibration
CALIBRATION_CHECK_INTERVAL = 5  # Recalibrate token counts every N steps

# Error message display
ERROR_PREVIEW_COUNT = 3  # Number of errors to show in previews

# Loop detection configuration
class LoopDetectionConfig:
    """Centralized loop detection configuration - Optimized for reliability"""
    # Development mode: tighter thresholds for faster loop detection
    IS_DEVELOPMENT = os.getenv('ENVIRONMENT', 'development') == 'development'

    # Use tighter thresholds in development for faster debugging
    if IS_DEVELOPMENT:
        MAX_REPETITIONS = int(os.getenv('MAX_REPETITIONS', '2'))  # FIX 4: Aggressive loop detection
        UNCHANGED_STATE_THRESHOLD = int(os.getenv('UNCHANGED_STATE_THRESHOLD', '3'))  # FIX 4: Aggressive
        STATE_CHANGE_THRESHOLD = 3  # FIX 4: Detect loops after 2-3 repetitions
        MAX_ALLOWED_REPETITIONS = 2  # FIX 4: Catch loops after just 2 repetitions
    else:
        MAX_REPETITIONS = int(os.getenv('MAX_REPETITIONS', '3'))  # FIX 4: Stricter for production
        UNCHANGED_STATE_THRESHOLD = int(os.getenv('UNCHANGED_STATE_THRESHOLD', '4'))  # FIX 4: Stricter
        STATE_CHANGE_THRESHOLD = 4  # FIX 4: Lower threshold
        MAX_ALLOWED_REPETITIONS = 3  # FIX 4: Catch loops faster in production

    ACTION_SIMILARITY_THRESHOLD = 0.95  # Strict matching to avoid false positives
    MEMORY_WINDOW_SIZE = 25  # Larger window for better context
    DETECTION_WINDOW = 15  # Longer detection window

    # Aliases for compatibility
    LOOP_WARNING_THRESHOLD = MAX_ALLOWED_REPETITIONS  # Threshold for warning about loops
    ALTERNATING_PATTERN_MIN = 4  # Minimum pattern length to detect alternating actions

# Memory management configuration
class MemoryConfig:
    """Memory optimization settings for AutoV2 - Optimized for performance"""
    MAX_SCREENSHOTS_IN_GIF = 10  # Reduced to prevent OOM
    MAX_HISTORY_SIZE = int(os.getenv('MAX_MEMORY_CACHE_SIZE', '30'))  # Configurable via env
    SCREENSHOT_JPEG_QUALITY = int(os.getenv('SCREENSHOT_JPEG_QUALITY', '70'))  # Configurable
    ENABLE_GIF_CREATION = _core_bool_env('ENABLE_GIF_CREATION', False)  # Disabled by default
    CLEAR_SCREENSHOTS_AFTER_GIF = True  # Remove base64 data after GIF creation
    MAX_SCREENSHOT_SIZE_MB = 3  # Reduced max size per screenshot
    # UNIFIED: Single source from environment, default 50
    CLEANUP_INTERVAL = int(os.getenv('MEMORY_CLEANUP_INTERVAL', '50'))  # Clean every N operations

# History and Message Management
MAX_HISTORY_SIZE = 30  # Maximum number of messages to keep in history
PROTECT_RECENT_MESSAGES = 10  # Number of recent messages to protect from trimming
PROTECT_INITIAL_MESSAGES = 3  # Number of initial messages (system prompts) to protect

# Token and Context Management - REMOVED: Use modules.llm.model_registry instead
# TOKEN_BUFFER_SIZE, MIN_CONTEXT_TOKENS, MAX_CONTEXT_PERCENTAGE moved to model_registry
# For context management, use: get_model_config(model_name).safe_input_tokens

# Tool Call Management
# Dynamic lookahead window that scales with context size
def get_tool_lookahead_window(total_messages: int = 0) -> int:
    """Get dynamic lookahead window based on context size.

    For large contexts (1M+ tokens), tool responses may be far from their calls.
    This scales the window appropriately to handle million-token contexts.

    Args:
        total_messages: Total number of messages in context

    Returns:
        Lookahead window size
    """
    if total_messages > 10000:  # Extreme scale (2M+ tokens)
        return 1000
    elif total_messages > 5000:  # Million-token scale contexts
        return 600
    elif total_messages > 2000:  # Very large context
        return 300
    elif total_messages > 1000:  # Large context
        return 150
    elif total_messages > 500:  # Medium-large context
        return 75
    elif total_messages > 200:  # Medium context
        return 40
    else:
        return 20  # Default for small contexts

TOOL_LOOKAHEAD_WINDOW = 15  # Default, use get_tool_lookahead_window() for dynamic sizing
PLACEHOLDER_CONTENT = "[Awaiting tool response]"  # Content for placeholder ToolMessages
PLACEHOLDER_SOURCE = "tool_placeholder"  # Metadata source for placeholders

# Memory and Performance
# REMOVED DUPLICATE: Use MemoryConfig.CLEANUP_INTERVAL instead
MEMORY_CHECK_INTERVAL = 10  # Check memory usage every N steps
MEMORY_MESSAGE_THRESHOLD_MB = 50  # Trim if message history uses > 50MB
MEMORY_HIGH_WATERMARK_MB = 1000  # Warn if total memory over 1GB
MAX_RESULT_PREVIEW_LENGTH = 500  # Maximum length for action result previews
LARGE_CONTENT_THRESHOLD = 50000  # Threshold for considering content "large"

# Retry and Timeout Settings
MAX_LLM_RETRIES = 3  # Maximum retries for LLM calls
MAX_LLM_CREATION_RETRIES = 2  # Retries when creating LLM instances


# ========== AUX-MODEL ROUTER (A5/A1 + generalized) ==========
# Provider -> cheap auxiliary model, used for compaction (`llm_compact_history`) and the
# judge aux task (output validation) via resolve_aux_model() below.
# NOTE: this map has no "openrouter" key (intentional) — an openrouter session's aux
# tasks resolve to None and fall back to the main model.
# (Reference parity: auxiliary.compression.model — a small/fast model for compaction.)
COMPACTION_AUX_MODEL_MAP = {
    "anthropic": "claude-haiku-4-5",
    "openai": "gpt-5-mini",
    "gemini": "gemini-flash",
    "google": "gemini-flash",
}

# Alias: the generalized router reuses the same cheap-model map for all aux tasks.
AUX_MODEL_MAP = COMPACTION_AUX_MODEL_MAP

# Per-task explicit override env var names. Compaction keeps its legacy knob.
# UP-10 2.1: only `compaction` (live) and `judge` (wired into output validation,
# default-off/fail-open) remain. `planner`/`vision` had zero call sites and were
# removed as dead config surface.
_AUX_TASK_ENV = {
    "compaction": "COMPACTION_MODEL",
    "judge": "AUX_MODEL_JUDGE",
}


def resolve_aux_model(task, provider=None):
    """Which model should aux `task` use? (None => use the main model.)

    Precedence: 1) per-task explicit env (e.g. AUX_MODEL_JUDGE / COMPACTION_MODEL);
    2) provider cheap-map default when AUX_AUTO=true (or, for compaction only, the
    legacy COMPACTION_AUTO_AUX=true); 3) None.
    """
    explicit = os.getenv(_AUX_TASK_ENV.get(task, ""), "")
    if explicit:
        return explicit
    auto = _core_bool_env("AUX_AUTO", False)
    if task == "compaction":
        auto = auto or _core_bool_env("COMPACTION_AUTO_AUX", False)
    if auto and provider:
        return AUX_MODEL_MAP.get(provider.lower())
    return None


_AUX_SLOTS = ("compaction", "judge", "reflection")


def _parse_provider_model(token, default_provider=None):
    """Parse one fallback-chain token ('provider/model' or bare 'model')."""
    token = (token or "").strip()
    if not token:
        return None
    if "/" in token:
        head, tail = token.split("/", 1)
        tail = tail.strip()
        head = head.strip()
        return {"model": tail, "provider": head or default_provider} if tail else None
    return {"model": token, "provider": default_provider}


def resolve_aux_chain(task, provider=None):
    """Ordered aux-model candidates for `task` (primary first, then per-task fallbacks).

    B5 (Hermes `auxiliary.<task>.fallback_chain` parity): each of the 3 real aux
    call sites (compaction/judge/reflection) can be pointed at its own model+
    provider plus an ordered fallback list, instead of a single model string.
    Empty list => caller uses the main model (unchanged runtime contract).

    Env per slot (<SLOT> = task.upper()):
      AUX_MODEL_<SLOT>    primary model    (legacy: COMPACTION_MODEL / AUX_MODEL_JUDGE)
      AUX_PROVIDER_<SLOT> primary provider (legacy: COMPACTION_PROVIDER / AUX_PROVIDER)
      AUX_FALLBACK_<SLOT> comma list of provider/model (or bare model) candidates

    `reflection` inherits compaction's config (model AND provider, as a pair) when
    its own env is unset (back-compat: reflection historically reused
    _provision_compaction_llm wholesale). If reflection sets its OWN model, none of
    compaction's config is consulted — including its provider, so a compaction
    provider never silently leaks onto a reflection-specific model.

    Provider precedence for reflection specifically:
      1) AUX_PROVIDER_REFLECTION (explicit reflection override)
      2) compaction's provider (AUX_PROVIDER_COMPACTION or legacy COMPACTION_PROVIDER)
         — ONLY when reflection's model was itself inherited from compaction
      3) AUX_PROVIDER (generic aux fallback)
    """
    if task not in _AUX_SLOTS:
        # Fixed slot set by design (the 3 real aux call sites) — an unknown task
        # never gets a chain, even under AUX_AUTO (dead-config trap guard, UP-10).
        return []
    slot = task.upper()
    inherited_from_compaction = False
    if task == "reflection":
        # Reflection is NOT in _AUX_TASK_ENV, so resolve_aux_model("reflection", ...)'s
        # only possible effect is the global-AUX_AUTO cheap-map early-exit — which
        # would pre-empt the compaction inheritance below and drop the paired
        # COMPACTION_PROVIDER (pre-B5, reflection reused _provision_compaction_llm
        # wholesale, so AUX_AUTO + COMPACTION_PROVIDER resolved as a pair). Go
        # straight from own-env to the compaction branch, which itself honors
        # AUX_AUTO/COMPACTION_AUTO_AUX via resolve_aux_model("compaction", ...).
        primary_model = os.getenv("AUX_MODEL_REFLECTION", "")
        if not primary_model:
            primary_model = os.getenv("AUX_MODEL_COMPACTION", "") or resolve_aux_model("compaction", provider)
            if primary_model:
                inherited_from_compaction = True
    else:
        primary_model = os.getenv(f"AUX_MODEL_{slot}", "") or resolve_aux_model(task, provider)
    if not primary_model:
        return []

    primary_provider = os.getenv(f"AUX_PROVIDER_{slot}") or None
    if task == "compaction":
        primary_provider = primary_provider or os.getenv("COMPACTION_PROVIDER") or None
    elif task == "reflection":
        if not primary_provider and inherited_from_compaction:
            primary_provider = os.getenv("AUX_PROVIDER_COMPACTION") or os.getenv("COMPACTION_PROVIDER") or None
        primary_provider = primary_provider or os.getenv("AUX_PROVIDER") or None
    else:
        primary_provider = primary_provider or os.getenv("AUX_PROVIDER") or None

    chain = [{"model": primary_model, "provider": primary_provider}]

    raw_fb = os.getenv(f"AUX_FALLBACK_{slot}", "")
    if not raw_fb and task == "reflection" and inherited_from_compaction:
        raw_fb = os.getenv("AUX_FALLBACK_COMPACTION", "")
    for tok in raw_fb.split(","):
        cand = _parse_provider_model(tok)
        if cand:
            chain.append(cand)
    return chain


def reflection_llm_enabled_default() -> bool:
    """Whether H-MEM phase reflection synthesizes summaries via the aux LLM (UP-09).

    Default **ON** (mirrors the MEMORY_BACKEND default-on precedent). Disable with
    REFLECTION_LLM_ENABLED in {none, off, false, 0, no, ''}.

    SINGLE SOURCE OF TRUTH: both the model-provisioning site (construction.py) and the
    runtime guard (TaskContextManager.__init__) MUST read this helper. The historical bug
    (Fusion-validated 2026-06-16) was that the runtime guard read
    `BotConfig.get("REFLECTION_LLM_ENABLED", False)` — and `BotConfig.get` is
    `getattr(self, key, default)` with no such attribute, so it was ALWAYS False — while
    construction.py read os.getenv. The two sources disagreed and reflection never fired.
    """
    val = os.getenv("REFLECTION_LLM_ENABLED", "true").strip().lower()
    return val not in ("none", "off", "false", "0", "no", "")


# --- Autonomy & continuous-learning loops (Reference-parity, 2026-06-16) ---------
#
# Shared flag helpers for the four loops POLYROB lacked: self-wake re-entry,
# writable-skills + background-review, cron run-loop+delivery, durable goal board,
# and the curator. SINGLE SOURCE OF TRUTH for the falsey-set semantics, mirroring
# reflection_llm_enabled_default() above. All loops default-OFF + fail-open except
# MEMORY_SEARCH_TOOL (read-only, tenant-scoped) and CRON_RUN_LOOP (fixes a live bug
# where cron built a session but never ran the agent loop).

_FALSEY = ("none", "off", "false", "0", "no", "")


def _bool_env(name: str, default: bool) -> bool:
    """Read a boolean env var with POLYROB's falsey-set semantics.

    Delegates to the repo-wide SSOT (``core.env.bool_env``) so this module shares
    one parser with everything else instead of reimplementing it (the reflection-gate
    bug was a parser/source mismatch). Kept as a thin wrapper so in-module callers
    (and this module's public ``_bool_env`` symbol) are unaffected.
    """
    return _core_bool_env(name, default)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# --- Local (terminal-native, single-user) profile -------------------------
# When POLYROB_LOCAL is truthy, the *safe* autonomy/learning flags default ON as a
# group, so a terminal user gets the W1-W7 loops without setting ~6 env vars.
# Multi-tenant server entry (main.py / uvicorn) never sets POLYROB_LOCAL, so its
# defaults are unchanged. An explicit per-flag value (e.g. GOALS_ENABLED=off)
# still wins — only the *default* moves.
#
# Excludes anything with a multi-tenant blast radius even on one machine:
# CODE_EXEC_ENABLED (not a sandbox) and the sub-agent concurrency caps.
_SAFE_LOCAL_FLAGS = frozenset({
    "SELF_WAKE_ENABLED",
    "SKILLS_WRITABLE",
    "SELF_CONTEXT_WRITABLE",
    "BACKGROUND_REVIEW_ENABLED",
    "GOALS_ENABLED",
    "CURATOR_ENABLED",
    "INSIGHTS_TOOL",
    "CODING_TOOLS_ENABLED",
    # P0-D: structured git over the confined workspace. Safe on a single-user CLI
    # (own repo); multi-tenant server stays OFF by default. git_push is separately
    # approval-gated + leaf-blocked (Task 9).
    "GIT_TOOLS_ENABLED",
    # NOTE (FL-D9): SKILL_CATALOG_INCLUDE_ALL was here, but its resolver
    # (skill_catalog_include_all(), below) hardcodes `_bool_env("SKILL_CATALOG_INCLUDE_ALL",
    # True)` directly and never consults `_safe_autonomy_default`/this set — the entry
    # was dead (default is already ON everywhere). Removed 2026-07 (behavior-neutral).
    # KB (knowledge-base) feature: safe on a single-user CLI (read/write own KB),
    # multi-tenant default stays OFF until per-tenant isolation is verified.
    "KB_ENABLED",
    # C1: context-reference expansion (@file/@folder/@diff/@url). Safe on a single-user
    # CLI where the workspace is trusted; multi-tenant server stays OFF by default so
    # accidental file-inclusion from a shared workspace is not the default.
    "CONTEXT_REFERENCES_ENABLED",
    # C9: auto-load CLAUDE.md/AGENTS.md/.cursorrules as a PROJECT_CONTEXT foundation
    # message. Safe on a single-user CLI (reads from cwd/git-root); server stays OFF
    # (multi-tenant workspaces may not have a project context file).
    "PROJECT_CONTEXT_AUTOLOAD",
    # T13: KB auto-prefetch — inject KB recall alongside memory recall at step start.
    # Safe on a single-user CLI (reads own KB); multi-tenant server stays OFF by default
    # because KB_ENABLED itself is also local-only by default.
    "KB_AUTO_PREFETCH",
    # Task 2/3/4: episodic activity ledger — durable per-run provenance rows +
    # digest injection + continuity bridge. Safe on a single-user CLI (own
    # tenant); multi-tenant server stays OFF by default.
    "EPISODIC_MEMORY_ENABLED",
    "EPISODIC_DIGEST_INJECT",
    "CONTINUITY_BRIDGE_ENABLED",
    # AU-F1.1: the goal-board dispatcher ticks under POLYROB_LOCAL (GOALS_ENABLED is
    # in this set), but without the planner nothing ever proposes an objective's next
    # goal -- the board sits idle even though the ticker runs (the "idle since Jul 1"
    # incident). Safe on a single-user CLI (own tenant's own objectives); multi-tenant
    # server stays OFF by default. Existing gates (GOALS_ENABLED, an active objective,
    # a thin ready-queue, the planner cooldown) still apply regardless of this default.
    "GOAL_PLANNER_ENABLED",
    # §7.1: self-evolution transparency — proactively notify the owner of a pending
    # identity/skill proposal + expose the approve/reject/list surface. Safe on a
    # single-user CLI (own tenant); multi-tenant server stays OFF by default (an
    # unsolicited push to a shared owner channel is opt-in there).
    "SELF_EVOLUTION_TRANSPARENCY",
    # Task 5: gated `message` action (owner/allowlist -> MessageRouter send). Safe
    # on a single-user CLI (own tenant, own owner-bound targets); multi-tenant
    # server stays OFF by default (arbitrary outbound send is opt-in there).
    "MESSAGE_TOOL_ENABLED",
})


def local_mode_enabled() -> bool:
    """True when running as the single-user terminal-native agent.

    Canonical flag: ``POLYROB_LOCAL``. ``ROB_LOCAL`` is accepted as a deprecated
    back-compat alias (older docs/scripts referenced it) — either being truthy
    enables local mode, so a doc that still says ``ROB_LOCAL`` isn't a silent no-op.
    """
    return _bool_env("POLYROB_LOCAL", False) or _bool_env("ROB_LOCAL", False)


def message_tool_enabled() -> bool:
    """Whether the gated `message` action (owner/allowlist -> MessageRouter) is
    registered. Default OFF; ON under POLYROB_LOCAL (single-user CLI) via the
    _SAFE_LOCAL_FLAGS group. An explicit MESSAGE_TOOL_ENABLED always wins.
    """
    return _bool_env("MESSAGE_TOOL_ENABLED", _safe_autonomy_default("MESSAGE_TOOL_ENABLED"))


def task_personality_block_enabled() -> bool:
    """Whether the persona/<identity> block is injected into the task agent.

    S1 (chat consolidation): injects the chat agent's character/personality into
    the unified Task agent's <identity> block so chat-mode carries persona without
    a separate ChatAgent. The persona TEXT is rendered from a Character via the
    pure agents/personality/persona_render.render_persona_block; the task-agent
    core only ever sees a str (it never imports the chat stack). This access-time
    gate is the live seam — the module-level TASK_PERSONALITY_BLOCK constant that
    used to exist was a dead decoy.

    Access-time (not import-time) so it sees POLYROB_LOCAL set via
    os.environ.setdefault in bootstrap. Defaults ON under local mode (single-user
    CLI wants its persona; persona_block resolves to "" when OFF => byte-identical
    system prompt), OFF on the multi-tenant server. An explicit
    TASK_PERSONALITY_BLOCK env always wins.
    """
    raw = os.getenv("TASK_PERSONALITY_BLOCK")
    if raw is None or raw.strip() == "":
        return local_mode_enabled()
    return _bool_env("TASK_PERSONALITY_BLOCK", False)


def memory_prefetch_cadence() -> int:
    """Steps between automatic memory re-prefetch (Phase 1.3).

    0 = prefetch on the FIRST step only (legacy, prod-safe). N>0 = ALSO prefetch
    every N steps so a long task keeps re-recalling phase-relevant memory instead of
    recalling once at step 1 and never again.

    Resolved at ACCESS time (not import) so it sees POLYROB_LOCAL even though that is
    set via os.environ.setdefault in bootstrap, which may run after this module is
    first imported. Defaults to 3 under local mode, 0 on the multi-tenant server; an
    explicit ``MEMORY_PREFETCH_CADENCE`` (incl. ``0``) always wins.
    """
    return _int_env("MEMORY_PREFETCH_CADENCE", 3 if local_mode_enabled() else 0)


def hmem_tail_placement() -> bool:
    """Whether in-session hierarchical memory is placed as a dynamic SUFFIX after the
    conversation (Phase 0.1) instead of in the foundation ahead of it.

    The H-MEM block changes every step; in the foundation prefix it invalidated the
    prompt cache for everything after it (skills tail + all conversation) on every
    step. As a tail suffix, the stable foundation + growing conversation form a
    cacheable prefix and only the small H-MEM suffix is reprocessed.

    Resolved at access time. Defaults ON under POLYROB_LOCAL (where the owner soaks
    it) and OFF on the multi-tenant server (byte-identical legacy) until soaked.
    Explicit ``HMEM_TAIL_PLACEMENT`` wins.
    """
    return _bool_env("HMEM_TAIL_PLACEMENT", local_mode_enabled())


def ticker_idle_backoff_enabled() -> bool:
    """Whether idle background tickers (cron, goal dispatch) back off their poll
    interval when a tick finds no due work, instead of firing at a fixed cadence
    forever.

    A fixed 60s ticker costs nothing on a multi-tenant server with steady job
    volume, but on a single-user local CLI (POLYROB_LOCAL) it is close to the
    only thing keeping the process from ever going idle -- a real contributor
    to laptop battery drain. Backoff only kicks in on demonstrably idle ticks
    (nothing ran, nothing failed) and resets to the base interval the moment
    work resumes, so responsiveness is unaffected once something is actually
    happening.

    Resolved at access time. Defaults ON under POLYROB_LOCAL, OFF on the
    multi-tenant server (byte-identical fixed-cadence legacy) so a precisely
    time-scheduled cron job on a shared server never slips. Explicit
    ``TICKER_IDLE_BACKOFF_ENABLED`` always wins.
    """
    return _bool_env("TICKER_IDLE_BACKOFF_ENABLED", local_mode_enabled())


def ticker_idle_backoff_max_multiplier() -> int:
    """Cap on how many multiples of a ticker's base interval an idle backoff may
    reach (e.g. 5x a 60s base = 300s = 5 minutes worst-case staleness before a
    newly-due job is noticed). Explicit ``TICKER_IDLE_BACKOFF_MAX_MULTIPLIER``
    always wins; default 5.
    """
    return _int_env("TICKER_IDLE_BACKOFF_MAX_MULTIPLIER", 5)


def embedder_needed() -> bool:
    """Whether this deployment actually needs the sentence-transformers embedder (torch).

    SSOT for both the CLI (maybe_register_cli_embedder) and the server (initialize_modules):
    only build the heavy embedder when KB is enabled, MEMORY_BACKEND=local_vector (hybrid
    vector recall), or local mode. The default MEMORY_BACKEND=sqlite uses FTS5 keyword recall
    and needs no embeddings. See docs/plans/2026-06-26-runtime-architecture-finalization-FUSION.md (P1-EMB).
    """
    return (
        AutonomyConfig.kb_enabled()
        or os.getenv("MEMORY_BACKEND", "sqlite").lower() == "local_vector"
        or local_mode_enabled()
    )


def _safe_autonomy_default(flag_name: str) -> bool:
    """Default for a safe autonomy flag: ON under local mode, else OFF."""
    return local_mode_enabled() if flag_name in _SAFE_LOCAL_FLAGS else False


class AutonomyConfig:
    """Feature flags + caps for the autonomy/continuous-learning loops.

    Read through this class (not raw os.getenv) so every loop shares one parser and
    the defaults are documented in one place. Evaluated at access time (classmethod /
    property-free) so tests can monkeypatch env between calls.
    """

    # W1 — self-wake rail
    @staticmethod
    def self_wake_enabled() -> bool:
        return _bool_env("SELF_WAKE_ENABLED", _safe_autonomy_default("SELF_WAKE_ENABLED"))

    @staticmethod
    def self_wake_max_reentries() -> int:
        return _int_env("SELF_WAKE_MAX_REENTRIES", 3)

    @staticmethod
    def self_wake_idle_backoff_sec() -> float:
        try:
            return float(os.getenv("SELF_WAKE_IDLE_BACKOFF_SEC", "30"))
        except (TypeError, ValueError):
            return 30.0

    # W2 — writable skills + background review
    @staticmethod
    def skills_writable() -> bool:
        return _bool_env("SKILLS_WRITABLE", _safe_autonomy_default("SKILLS_WRITABLE"))

    @staticmethod
    def skills_writable_require_review() -> bool:
        return _bool_env("SKILLS_WRITABLE_REQUIRE_REVIEW", True)

    @staticmethod
    def skill_overwrite_protect() -> bool:
        # An agent/background overwrite of an existing ACTIVE skill becomes a .pending
        # proposal (owner promotes); all overwrites archive the prior body. Default ON.
        return _bool_env("SKILL_OVERWRITE_PROTECT", True)

    # polyrob C-write — evolving SELF identity (agent-writable per-(instance,user) doc)
    @staticmethod
    def self_context_writable() -> bool:
        return _bool_env("SELF_CONTEXT_WRITABLE", _safe_autonomy_default("SELF_CONTEXT_WRITABLE"))

    @staticmethod
    def self_context_require_review() -> bool:
        return _bool_env("SELF_CONTEXT_REQUIRE_REVIEW", True)

    # §7.1 — self-evolution transparency + owner control loop
    @staticmethod
    def self_evolution_transparency() -> bool:
        return _bool_env("SELF_EVOLUTION_TRANSPARENCY",
                         _safe_autonomy_default("SELF_EVOLUTION_TRANSPARENCY"))

    @staticmethod
    def background_review_enabled() -> bool:
        return _bool_env("BACKGROUND_REVIEW_ENABLED", _safe_autonomy_default("BACKGROUND_REVIEW_ENABLED"))

    @staticmethod
    def bg_review_interval() -> int:
        return _int_env("BG_REVIEW_INTERVAL", 10)

    @staticmethod
    def bg_review_max_steps() -> int:
        return _int_env("BG_REVIEW_MAX_STEPS", 8)

    # W3 — cron run-loop + delivery
    @staticmethod
    def cron_run_loop() -> bool:
        return _bool_env("CRON_RUN_LOOP", True)

    @staticmethod
    def cron_delivery_enabled() -> bool:
        return _bool_env("CRON_DELIVERY_ENABLED", False)

    # W4 — durable goal board
    @staticmethod
    def goals_enabled() -> bool:
        return _bool_env("GOALS_ENABLED", _safe_autonomy_default("GOALS_ENABLED"))

    @staticmethod
    def goal_max_retries() -> int:
        return _int_env("GOAL_MAX_RETRIES", 2)

    @staticmethod
    def goal_claim_ttl_sec() -> int:
        return _int_env("GOAL_CLAIM_TTL_SEC", 900)

    @staticmethod
    def goal_max_run_seconds() -> int:
        """H11: hard wall-clock cap on a single goal run (mirrors cron's per-job cap).
        A goal is otherwise bounded only by max_steps, so one hung step (tool/LLM/browser)
        blocks forever and permanently occupies a GOAL_MAX_CONCURRENT slot."""
        return _int_env("GOAL_MAX_RUN_SECONDS", 1800)

    @staticmethod
    def goal_dispatch_interval_sec() -> int:
        return _int_env("GOAL_DISPATCH_INTERVAL_SEC", 60)

    @staticmethod
    def goal_max_concurrent() -> int:
        return _int_env("GOAL_MAX_CONCURRENT", 2)

    @staticmethod
    def goal_dedup_threshold() -> float:
        try:
            return float(os.getenv("GOAL_DEDUP_THRESHOLD", "0.6"))
        except (TypeError, ValueError):
            return 0.6

    @staticmethod
    def goal_planner_enabled() -> bool:
        return _bool_env("GOAL_PLANNER_ENABLED", _safe_autonomy_default("GOAL_PLANNER_ENABLED"))

    @staticmethod
    def goal_planner_min_ready() -> int:
        return _int_env("GOAL_PLANNER_MIN_READY", 2)

    @staticmethod
    def goal_planner_cooldown_sec() -> int:
        return _int_env("GOAL_PLANNER_COOLDOWN_SEC", 3600)

    @staticmethod
    def goal_planner_history_n() -> int:
        return _int_env("GOAL_PLANNER_HISTORY_N", 10)

    @staticmethod
    def goal_daily_quota() -> int:
        """Max goal runs started per trailing 24h; <=0 disables the rail."""
        return _int_env("GOAL_DAILY_QUOTA", 6)

    @staticmethod
    def goal_self_wake_enabled() -> bool:
        # Was unconditional; redundant-cost finding (grok livetest 2026-06-27).
        return _bool_env("GOAL_SELF_WAKE_ENABLED", False)

    # §3.2 (goal-completion-verification, 2026-07-05) — judge a completed goal's
    # acceptance with a cheap aux model. Default OFF (verify on prod, then flip);
    # 'unmet' -> record_failure, 'unclear'/error/timeout -> pass (fail-open).
    @staticmethod
    def goal_completion_judge() -> bool:
        return _bool_env("GOAL_COMPLETION_JUDGE", False)

    @staticmethod
    def goal_judge_timeout_sec() -> int:
        return _int_env("GOAL_JUDGE_TIMEOUT_SEC", 60)

    # §7.2 — blocker → owner escalation. When a goal trips the circuit breaker
    # (status='blocked') OR the pipeline drains, surface a concrete ask to the owner
    # instead of dying silently. Default OFF (an unsolicited owner push is opt-in).
    @staticmethod
    def goal_blocker_escalation() -> bool:
        return _bool_env("GOAL_BLOCKER_ESCALATION", False)

    @staticmethod
    def goal_empty_pipeline_escalate_after() -> int:
        """Consecutive planner runs that leave the ready queue EMPTY before the
        stall escalates to the owner (rides GOAL_BLOCKER_ESCALATION)."""
        return _int_env("GOAL_EMPTY_PIPELINE_ESCALATE_AFTER", 2)

    # §7.5 — autonomous continuity bridge. Carry a recent-activity summary INTO a
    # goal/cron tick (opposite scoping to the chat digest) so autonomous runs stop
    # re-deriving "nothing new" every tick. Default OFF (additive context; verify
    # token cost before flipping on).
    @staticmethod
    def autonomous_continuity_bridge() -> bool:
        return _bool_env("AUTONOMOUS_CONTINUITY_BRIDGE", False)

    # W5 — curator
    @staticmethod
    def curator_enabled() -> bool:
        return _bool_env("CURATOR_ENABLED", _safe_autonomy_default("CURATOR_ENABLED"))

    @staticmethod
    def curator_interval_hours() -> int:
        return _int_env("CURATOR_INTERVAL_HOURS", 168)

    @staticmethod
    def curator_stale_days() -> int:
        return _int_env("CURATOR_STALE_DAYS", 30)

    @staticmethod
    def curator_archive_days() -> int:
        return _int_env("CURATOR_ARCHIVE_DAYS", 90)

    # (curator_llm_merge / CURATOR_LLM_MERGE removed 2026-06-29 — the Phase-2 merge step
    #  it gated was a logged no-op with no merge policy. Re-add under its own flag when a
    #  concrete policy exists.)

    # W6 — cross-session search tool (read-only, default-on)
    @staticmethod
    def memory_search_tool() -> bool:
        return _bool_env("MEMORY_SEARCH_TOOL", True)

    # W7 — insights tool (read-only authored-skill reuse metric)
    @staticmethod
    def insights_tool() -> bool:
        return _bool_env("INSIGHTS_TOOL", _safe_autonomy_default("INSIGHTS_TOOL"))

    # KB — knowledge-base feature gate (Task 2 / local_vector prerequisite)
    @staticmethod
    def kb_enabled() -> bool:
        return _bool_env("KB_ENABLED", _safe_autonomy_default("KB_ENABLED"))

    # C1 — context-reference expansion (@file/@folder/@diff/@url)
    # Default ON under POLYROB_LOCAL (single-user CLI), OFF on the server.
    @staticmethod
    def context_references_enabled() -> bool:
        return _bool_env(
            "CONTEXT_REFERENCES_ENABLED",
            _safe_autonomy_default("CONTEXT_REFERENCES_ENABLED"),
        )

    # C9 — auto-load CLAUDE.md/AGENTS.md/.cursorrules as a PROJECT_CONTEXT foundation
    # message. Default ON under POLYROB_LOCAL (single-user CLI), OFF on the server.
    @staticmethod
    def project_context_autoload() -> bool:
        return _bool_env(
            "PROJECT_CONTEXT_AUTOLOAD",
            _safe_autonomy_default("PROJECT_CONTEXT_AUTOLOAD"),
        )

    @staticmethod
    def project_context_max_tokens() -> int:
        return _int_env("PROJECT_CONTEXT_MAX_TOKENS", 20000)

    # Phase 2 — server-side project-context opt-in. When ON (and NOT local mode),
    # the loader runs on the server and the file is injected UNTRUSTED-WRAPPED
    # (framed as DATA, not instructions). Default OFF and deliberately NOT a
    # safe-local flag — POLYROB_LOCAL must not flip it on, so the multi-tenant
    # server stays byte-identical unless an operator explicitly opts in.
    @staticmethod
    def project_context_server_mode() -> bool:
        return _bool_env("PROJECT_CONTEXT_SERVER_MODE", False)

    # T13 — KB auto-prefetch (inject KB recall alongside memory recall at step start)
    # Default ON under POLYROB_LOCAL (single-user CLI), OFF on multi-tenant server.
    @staticmethod
    def kb_auto_prefetch() -> bool:
        return _bool_env("KB_AUTO_PREFETCH", _safe_autonomy_default("KB_AUTO_PREFETCH"))

    # Task 2 — episodic activity ledger (durable per-run provenance rows).
    # Default ON under POLYROB_LOCAL (single-user CLI), OFF on the server.
    @staticmethod
    def episodic_memory_enabled() -> bool:
        return _bool_env("EPISODIC_MEMORY_ENABLED", _safe_autonomy_default("EPISODIC_MEMORY_ENABLED"))

    # Task 3 — inject a recent-episodes digest into the session.
    @staticmethod
    def episodic_digest_inject() -> bool:
        return _bool_env("EPISODIC_DIGEST_INJECT", _safe_autonomy_default("EPISODIC_DIGEST_INJECT"))

    # Task 4 — cross-session continuity bridge (thread_key stitching).
    @staticmethod
    def continuity_bridge_enabled() -> bool:
        return _bool_env("CONTINUITY_BRIDGE_ENABLED", _safe_autonomy_default("CONTINUITY_BRIDGE_ENABLED"))

    # Task 4 — LLM-generated continuity summary at reset. Intentionally NOT in
    # _SAFE_LOCAL_FLAGS: OFF everywhere by default (adds latency at reset).
    @staticmethod
    def continuity_llm_summary() -> bool:
        return _bool_env("CONTINUITY_LLM_SUMMARY", False)  # OFF everywhere (latency at reset)

    # Task 2 — episodic row retention window (days); pruning consumer TBD.
    @staticmethod
    def episodic_retention_days() -> int:
        return _int_env("EPISODIC_RETENTION_DAYS", 90)

    # T16 — interrupt-and-redirect: Ctrl-C mid-turn prompts for a redirect instruction
    # that becomes the next turn instead of silently aborting. Default OFF; NOT in
    # _SAFE_LOCAL_FLAGS (must be opt-in — changes SIGINT UX for all local users).
    @staticmethod
    def interrupt_redirect_enabled() -> bool:
        return _bool_env("INTERRUPT_REDIRECT", False)


class TimeoutConfig:
    """Centralized timeout configuration - SINGLE SOURCE OF TRUTH.

    All timeout values should be accessed through this class to ensure
    consistency across the codebase. Tool-specific timeouts account for
    the different latency characteristics of each tool type.

    Sub-agent settings are loaded from BotConfig (core/config.py) which supports
    both config file and environment variable sources.
    """

    # Lazy-loaded config reference
    _config = None

    @classmethod
    def _get_config(cls):
        """Get BotConfig instance (lazy load to avoid circular imports)."""
        if cls._config is None:
            try:
                from core.config import BotConfig
                cls._config = BotConfig()
            except Exception:
                cls._config = None
        return cls._config

    # ========== TOOL EXECUTION TIMEOUTS ==========
    # These are applied in controller/service.py multi_act()
    TOOL_TIMEOUTS = {
        'mcp': int(os.getenv('MCP_TIMEOUT_SECONDS', '180')),       # MCP: network/subprocess latency
        'browser': int(os.getenv('BROWSER_TIMEOUT_SECONDS', '120')),   # Browser: page loads, DOM operations
        'filesystem': int(os.getenv('FILESYSTEM_TIMEOUT_SECONDS', '30')),  # Filesystem: fast local I/O
        'polymarket': int(os.getenv('POLYMARKET_TIMEOUT_SECONDS', '60')),  # Polymarket API
        'default': int(os.getenv('DEFAULT_TOOL_TIMEOUT_SECONDS', '60')),   # Default for unknown tools
    }

    # ========== SUB-AGENT CONTROLS ==========
    # Loaded from BotConfig (core/config.py) - supports config file + env vars
    # FIX (Jan 2026): Sub-agent system disabled by default - too chaotic
    # Use the getter methods for dynamic config-based values

    @classmethod
    def get_sub_agents_enabled(cls) -> bool:
        """Check if sub-agents are enabled (from BotConfig)."""
        config = cls._get_config()
        if config:
            return config.sub_agents_enabled
        return _core_bool_env('SUB_AGENTS_ENABLED', True)

    @classmethod
    def get_subagent_least_privilege(cls) -> bool:
        """UP-05: narrow a delegated child's toolset (drop code_execution/cronjob
        tool_ids + suppress delegation actions on leaf children via a dedicated
        child Controller). Default ON — the prior behaviour leaked the full parent
        toolset to children. Set SUBAGENT_LEAST_PRIVILEGE=false for the legacy
        shared-controller path (byte-identical to pre-UP-05)."""
        return _core_bool_env('SUBAGENT_LEAST_PRIVILEGE', True)

    @classmethod
    def get_sub_agent_timeout(cls) -> int:
        """Sub-agent timeout in seconds (from BotConfig)."""
        config = cls._get_config()
        if config:
            return config.sub_agent_timeout
        return int(os.getenv('SUB_AGENT_TIMEOUT_SECONDS', '600'))

    @classmethod
    def get_parallel_subtasks_timeout(cls) -> int:
        """Parallel subtasks timeout in seconds (from BotConfig)."""
        config = cls._get_config()
        if config:
            return config.parallel_subtasks_timeout
        return int(os.getenv('PARALLEL_SUBTASKS_TIMEOUT_SECONDS', '900'))

    @classmethod
    def get_max_concurrent_sub_agents(cls) -> int:
        """Maximum concurrent sub-agents (from BotConfig)."""
        config = cls._get_config()
        if config:
            return config.max_concurrent_sub_agents
        return int(os.getenv('MAX_CONCURRENT_SUB_AGENTS', '3'))

    @classmethod
    def get_max_sub_agent_depth(cls) -> int:
        """Maximum sub-agent nesting depth (from BotConfig)."""
        config = cls._get_config()
        if config:
            return config.max_sub_agent_depth
        return int(os.getenv('MAX_SUB_AGENT_DEPTH', '1'))

    @classmethod
    def get_max_async_sub_agents(cls) -> int:
        """Max concurrent BACKGROUND sub-agents (UP-12 delegate_task background=true).

        Bounds how many background delegation slots may be live at once. Floors at 1
        and never exceeds get_max_concurrent_sub_agents() (background + sync share the
        same SubAgentManager semaphore, so the background cap must not exceed the total).
        Env: MAX_ASYNC_SUB_AGENTS (default 2).
        """
        raw = int(os.getenv('MAX_ASYNC_SUB_AGENTS', '2'))
        ceiling = cls.get_max_concurrent_sub_agents()
        return max(1, min(raw, ceiling))

    # Legacy attribute access - for backward compatibility
    # These call the getter methods
    SUB_AGENTS_ENABLED = True  # C-DELEG: ON by default; use get_sub_agents_enabled() for dynamic value
    SUB_AGENT_TIMEOUT = 600
    PARALLEL_SUBTASKS_TIMEOUT = 900
    MAX_CONCURRENT_SUB_AGENTS = 3
    MAX_SUB_AGENT_DEPTH = 1

    # ========== STEP/SESSION TIMEOUTS ==========
    STEP_TIMEOUT = float(os.getenv('STEP_TIMEOUT_SECONDS', '300'))       # 5 min per step
    STALL_TIMEOUT = float(os.getenv('STALL_TIMEOUT_SECONDS', '300'))     # 5 min stall detection

    # ========== LLM REQUEST TIMEOUTS ==========
    LLM_REQUEST_TIMEOUT = int(os.getenv('LLM_REQUEST_TIMEOUT_SECONDS', '120'))   # Standard LLM request
    LLM_STREAM_TIMEOUT = int(os.getenv('LLM_STREAM_TIMEOUT_SECONDS', '300'))     # Streaming needs longer
    LLM_BASE_TIMEOUT = float(os.getenv('LLM_BASE_TIMEOUT_SECONDS', '30'))        # Base (adjusted by tokens)

    # ========== BROWSER CLEANUP TIMEOUTS ==========
    BROWSER_CLOSE = float(os.getenv('BROWSER_CLOSE_TIMEOUT', '3.0'))
    BROWSER_CONTEXT_CLOSE = float(os.getenv('BROWSER_CONTEXT_CLOSE_TIMEOUT', '8.0'))
    BROWSER_INSTANCE_CLOSE = float(os.getenv('BROWSER_INSTANCE_CLOSE_TIMEOUT', '5.0'))
    PROCESS_KILL = float(os.getenv('PROCESS_KILL_TIMEOUT', '2.0'))

    @classmethod
    def get_tool_timeout(cls, tool_name: str) -> int:
        """Get timeout for a specific tool.

        Args:
            tool_name: Name of the tool (e.g., 'mcp', 'browser', 'filesystem')

        Returns:
            Timeout in seconds for the tool
        """
        if not tool_name:
            return cls.TOOL_TIMEOUTS['default']
        return cls.TOOL_TIMEOUTS.get(tool_name.lower(), cls.TOOL_TIMEOUTS['default'])

    @classmethod
    def get_llm_timeout(cls, streaming: bool = False) -> int:
        """Get timeout for LLM requests.

        Args:
            streaming: Whether this is a streaming request

        Returns:
            Timeout in seconds for the LLM request
        """
        return cls.LLM_STREAM_TIMEOUT if streaming else cls.LLM_REQUEST_TIMEOUT


# Legacy constants for backward compatibility (deprecated - use TimeoutConfig instead)
DEFAULT_STEP_TIMEOUT = TimeoutConfig.STEP_TIMEOUT
DEFAULT_STALL_TIMEOUT = TimeoutConfig.STALL_TIMEOUT
LLM_BASE_TIMEOUT = TimeoutConfig.LLM_BASE_TIMEOUT
BROWSER_CLOSE_TIMEOUT = TimeoutConfig.BROWSER_CLOSE
BROWSER_CONTEXT_CLOSE_TIMEOUT = TimeoutConfig.BROWSER_CONTEXT_CLOSE
BROWSER_INSTANCE_CLOSE_TIMEOUT = TimeoutConfig.BROWSER_INSTANCE_CLOSE
PROCESS_KILL_TIMEOUT = TimeoutConfig.PROCESS_KILL

# Agent Limits (defaults, can be overridden)
DEFAULT_MAX_FAILURES = 5  # Maximum consecutive failures before stopping
DEFAULT_MAX_ERROR_LENGTH = 400  # Maximum length for error messages
DEFAULT_MAX_ACTIONS_PER_STEP = 10  # Maximum actions per step
DEFAULT_MIN_INPUT_TOKENS = 1000  # Minimum safe input tokens

# MCP Tool Throttling (single source of truth)
# MCP actions (scraping, searches, APIs) are expensive and execute SEQUENTIALLY
# Each MCP action takes 30-180 seconds. Limiting prevents timeout cascades.
MAX_MCP_PER_STEP = int(os.getenv('MAX_MCP_PER_STEP', '3'))  # Configurable via env

# Context-compaction hysteresis (flow-efficiency D3-a)
# LLM compaction (llm_compact_history) is an EXTRA LLM call. Without a cooldown it
# re-fires every step once usage stays >=85% (a single large MCP result can re-cross
# the line each step), doubling call cost on long runs. Enforce a minimum step gap
# between LLM compactions; the >=95% emergency prune (non-LLM) remains the safety net.
COMPACTION_COOLDOWN_STEPS = int(os.getenv('COMPACTION_COOLDOWN_STEPS', '3'))

# Compaction payload tuning (Reference-parity context upgrade, 2026-06).
# These govern HOW the LLM compaction summarizes, not WHEN it fires (the tiered
# thresholds in CompactionManager own the trigger). Previously the summary input
# was silently truncated to the last 50 messages, each clipped to 500 chars, with
# tool results dropped entirely -> the summary lost most of what it claimed to keep.
COMPACTION_KEEP_RECENT = int(os.getenv('COMPACTION_KEEP_RECENT', '10'))  # min recent msgs kept verbatim
COMPACTION_TAIL_TOKEN_RATIO = float(os.getenv('COMPACTION_TAIL_TOKEN_RATIO', '0.20'))  # C3: tail kept by token budget too
COMPACTION_PER_MSG_CHARS = int(os.getenv('COMPACTION_PER_MSG_CHARS', '3000'))  # A2: per-message head+tail budget into summarizer
COMPACTION_TOOL_RESULT_CHARS = int(os.getenv('COMPACTION_TOOL_RESULT_CHARS', '2000'))  # A2: tool-result budget (was: dropped)
COMPACTION_MAX_SUMMARY_TOKENS = int(os.getenv('COMPACTION_MAX_SUMMARY_TOKENS', '12000'))  # A3: ceiling for summary budget
COMPACTION_MIN_SUMMARY_TOKENS = int(os.getenv('COMPACTION_MIN_SUMMARY_TOKENS', '2000'))  # A3: floor for summary budget
COMPACTION_MIN_SAVINGS_PCT = float(os.getenv('COMPACTION_MIN_SAVINGS_PCT', '10.0'))  # B4: anti-thrash back-off threshold
COMPACTION_CHECKPOINT = _core_bool_env('COMPACTION_CHECKPOINT', True)  # C2: dump pre-compaction trajectory
# A5: route the (expensive, repeated) summarization call to a cheaper auxiliary model.
# Empty -> use the main model (backward-compatible). Format: "provider:model" or "model".
COMPACTION_AUX_MODEL = os.getenv('COMPACTION_AUX_MODEL', '')

# Bounded tool-free reasoning turns (flow-efficiency D1-a, the seed resolution).
# POLYROB forces >=1 function call every step. This allows a SMALL number of tool-free
# "planning" turns to be treated as legitimate (gentle nudge) rather than a hard
# error on the very first empty response. The 3-consecutive thinking-loop
# escalation remains the hard backstop, so the loop bound is unchanged.
ALLOWED_REASONING_TURNS = int(os.getenv('ALLOWED_REASONING_TURNS', '1'))

# S-1: progressive skill disclosure. When OFF (default) skills are eager-injected
# full-body as a pinned user message every step (~3.2k tok). When ON, only a compact
# <skill-catalog> (ids + one-line descriptions, ~0.15k) is injected and the agent
# pulls a skill's full body on demand via the load_skill(skill_id) tool. Off keeps
# production behavior byte-identical.
def skill_progressive_disclosure() -> bool:
    """Access-time gate (mirrors skill_catalog_include_all). Default ON.

    Falsey-set SSOT (`core.env.bool_env`): only none/off/false/0/no/'' disable.
    Access-time read (not an import-bound constant) so an env override always wins.
    """
    return _core_bool_env('SKILL_PROGRESSIVE_DISCLOSURE', True)

# NOTE: the catalog-include-all policy is the access-time `skill_catalog_include_all()`
# function below (defaults ON). A dead module-level constant that defaulted to 'false'
# used to live here and was a decoy — no caller read it while the function returned True
# (Phase 0.6: removed to end the "is it on or off?" confusion).


def skill_catalog_include_all() -> bool:
    """Whether the <skill-catalog> lists ALL skills (not just trigger-matched).

    P2-1a: defaults **ON everywhere** (model-chosen disclosure). The substring/regex
    trigger matcher is lossy — a paraphrased task can match zero skills, and on the
    server an unmatched skill is then undiscoverable (neither in the catalog nor
    load_skill-able). Listing the full compact catalog (id + one-line desc, bounded by
    ``get_catalog_skills(max_skills)``, ~20-30 tok each, cache-stable) lets the agent
    discover and ``load_skill`` what it judges relevant. ~600 tok at the current library
    size. Set ``SKILL_CATALOG_INCLUDE_ALL=false`` to restore trigger-matched-only.
    Access-time read (not the import-bound constant) so an env override always wins.
    """
    return _bool_env("SKILL_CATALOG_INCLUDE_ALL", True)

# UP-06: wrap untrusted tool-result content (mcp/browser/web/perplexity) in
# <untrusted_tool_result> delimiters before it enters message history, + a <security>
# system-prompt line teaching the model the wrapped content is DATA, not instructions.
# Security control — default ON. OFF => byte-identical to legacy (no wrapper, no <security>).
UNTRUSTED_TOOL_RESULT_WRAP = _core_bool_env('UNTRUSTED_TOOL_RESULT_WRAP', True)

# S1 (chat consolidation): the persona/<identity> injection gate lives in the
# access-time `task_personality_block_enabled()` function above (there is no
# module-level constant here — see that docstring for the full rationale).

# S2 (chat consolidation): tool-light "chat mode" for TaskAgent.chat_once. The
# toolset must be NON-EMPTY — an empty tool_ids list trips the orchestrator's
# comprehensive-default fallback (loads browser/mcp/etc). "task" is the harmless
# TODO tool; chat mode otherwise relies on send_message/done + conversational-exit.
CHAT_TOOL_IDS = [t.strip() for t in os.getenv('CHAT_TOOL_IDS', 'task').split(',') if t.strip()] or ['task']
CHAT_MAX_STEPS = _int_env('CHAT_MAX_STEPS', 8)

# Chat consolidation (HANDOFF-C, 2026-06-19): the HTTP /api/chat/message endpoint
# is now served SOLELY by the unified task agent (TaskAgent.chat_once); the legacy
# ChatAgent was retired, so there is no toggle/fallback target anymore.
#
# The chat path does NO credit pre-check (matching the legacy behavior), so
# chat_once skips create_session's pre-check by default (per-call billing still
# applies). Set CHAT_SKIP_CREDIT_CHECK=off to apply the credit gate to chat.
CHAT_SKIP_CREDIT_CHECK = os.getenv('CHAT_SKIP_CREDIT_CHECK', 'true').strip().lower() not in ('0', 'false', 'no', 'off', 'none', '')

# Human-in-the-Loop Defaults
DEFAULT_RECENT_MESSAGES_LIMIT = 10  # Number of recent messages to fetch for context

# Screenshot and GIF Settings
MAX_SCREENSHOTS_FOR_GIF = 20  # Maximum screenshots to include in GIF

# User Interaction Limits
DEFAULT_MAX_USER_GUIDANCE_TOKENS = 1000  # Maximum tokens for user guidance messages
DEFAULT_MAX_USER_MESSAGES_PER_STEP = 3  # Maximum user messages to process per step

# Session Management
# NOTE (B3): the per-user session cap SSOT is BotConfig.max_sessions_per_user
# (core/config.py, default 10), read at runtime in task_agent_lite. A duplicate
# `MAX_SESSIONS_PER_USER = 100` used to live here but had ZERO call sites and only
# misled readers — removed. Do not reintroduce a second cap constant here.
SESSION_CLEANUP_DELAY = 2.0  # Delay before cleaning up completed sessions
STALL_CHECK_INTERVAL = 30.0  # Check for stalls every 30 seconds

# Model-specific Tool Calling Instructions
# Some models (Grok, certain OpenRouter models) struggle with nested argument placement
# These instructions are injected into the system prompt for affected models
GROK_TOOL_CALL_INSTRUCTIONS = """
## 🚨 CRITICAL: MCP TOOL CALL FORMAT 🚨

**Do NOT send empty parameters — a tool with no params does nothing and fails.**

MCP tools are registered as DIRECT actions with FLAT parameters (there is no
nested `arguments={...}` wrapper). Call the tool by its `{server}_{tool}` name
and pass its parameters directly:

### ✅ CORRECT (direct, flat params):
```python
anysite_api(endpoint="/api/linkedin/search", params={"keywords": "CEO founder", "count": 10})
webparser_parse(url="https://example.com")
```

### ❌ WRONG (the deprecated nested form — DO NOT use):
```python
mcp_execute_tool(server_name="anysite", tool_name="search", arguments={})
```

### THE RULE:
- Call the tool DIRECTLY by its `{server}_{tool}` name; pass params inline/flat.
- NEVER pass empty params — if you don't know what a tool needs, discover it
  first (e.g. `mcp_list_tools`) before calling.
"""

# Models that need explicit tool argument instructions
MODELS_NEEDING_TOOL_INSTRUCTIONS = [
    'grok',
    'x-ai',
    'xai',
]

# ---------------------------------------------------------------------------
# P-1: per-model-family operational guidance.
# Short, high-signal nudges appended to the system prompt for non-Anthropic
# families whose tool-calling/agent behavior benefits from explicit framing.
# Kept terse so the cached system prefix stays small. Anthropic/Claude needs
# none (the prompt is authored for it); Grok keeps its dedicated argument-format
# block (MODELS_NEEDING_TOOL_INSTRUCTIONS) above and is not duplicated here.
# ---------------------------------------------------------------------------
GEMINI_FAMILY_INSTRUCTIONS = """## MODEL NOTE (Gemini)
- Call exactly one function per step unless several are truly independent; do not
  narrate a plan instead of calling a function — a step with no function call is rejected.
- Use the exact tool names from the schema; do not invent wrapper names.
- To reply to the user and finish, call done(text=...). A non-blocking send_message
  does not end your turn.
"""

OPENAI_FAMILY_INSTRUCTIONS = """## MODEL NOTE (GPT)
- Emit native function calls, not JSON-in-text. Every step must include ≥1 function call.
- Prefer one decisive action per step over long deliberation.
- To reply and finish in one shot, use done(text=...); reserve non-blocking
  send_message for a status update you immediately follow with more tool calls.
"""

KIMI_FAMILY_INSTRUCTIONS = """## MODEL NOTE (Kimi)
- Return tool calls as structured function calls only — never write tool-call
  delimiter tokens (e.g. <|tool_call_begin|>) into your text content.
- One function call per step is enough; don't repeat the same message across steps.
- To greet/answer and finish, call done(text=...). A non-blocking send_message does
  not end your turn and will make you repeat yourself.
"""

# Substring → guidance. First match on the lowercased model name wins.
MODEL_FAMILY_INSTRUCTIONS = [
    ('kimi', KIMI_FAMILY_INSTRUCTIONS),
    ('gemini', GEMINI_FAMILY_INSTRUCTIONS),
    ('gpt', OPENAI_FAMILY_INSTRUCTIONS),
]

# OpenRouter models that may need extra help
OPENROUTER_MODELS_NEEDING_HELP = [
    'grok',
    'mistral',
    'mixtral',
]