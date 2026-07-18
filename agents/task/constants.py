"""
Centralized constants for the Task agent system.
This provides a single source of truth for history management, trimming, and memory limits.
"""

import os

from core.env import bool_env as _core_bool_env
from core.env import float_env as _core_float_env
from core.env import int_env as _core_int_env

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
        MAX_REPETITIONS = _core_int_env('MAX_REPETITIONS', 2)  # FIX 4: Aggressive loop detection
        UNCHANGED_STATE_THRESHOLD = _core_int_env('UNCHANGED_STATE_THRESHOLD', 3)  # FIX 4: Aggressive
        STATE_CHANGE_THRESHOLD = 3  # FIX 4: Detect loops after 2-3 repetitions
        MAX_ALLOWED_REPETITIONS = 2  # FIX 4: Catch loops after just 2 repetitions
    else:
        MAX_REPETITIONS = _core_int_env('MAX_REPETITIONS', 3)  # FIX 4: Stricter for production
        UNCHANGED_STATE_THRESHOLD = _core_int_env('UNCHANGED_STATE_THRESHOLD', 4)  # FIX 4: Stricter
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
    MAX_HISTORY_SIZE = _core_int_env('MAX_MEMORY_CACHE_SIZE', 30)  # Configurable via env
    SCREENSHOT_JPEG_QUALITY = _core_int_env('SCREENSHOT_JPEG_QUALITY', 70)  # Configurable
    ENABLE_GIF_CREATION = _core_bool_env('ENABLE_GIF_CREATION', False)  # Disabled by default
    CLEAR_SCREENSHOTS_AFTER_GIF = True  # Remove base64 data after GIF creation
    MAX_SCREENSHOT_SIZE_MB = 3  # Reduced max size per screenshot
    # UNIFIED: Single source from environment, default 50
    CLEANUP_INTERVAL = _core_int_env('MEMORY_CLEANUP_INTERVAL', 50)  # Clean every N operations

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

    B5: each of the 3 real aux
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


# WS-1 (2026-07-16): the autonomy/mode/posture/payment-policy cluster + AutonomyConfig
# were relocated to the core tier (core/config_policy/policy.py) to break the
# core<->agents.task import cycle. They are re-exported here UNCHANGED so every existing
# `from agents.task.constants import ...` keeps working. See
# docs/plans/2026-07-16-ws1-config-relocation.md. New code imports from core.config_policy.
from core.config_policy import *  # noqa: F401,F403
from core.config_policy import (  # noqa: F401  (underscored + module-scope-used names)
    _FALSEY,
    _MODE_CAPABILITY_FLAGS,
    _POSTURE_FULL_FLAGS,
    _POSTURE_OWNER_VISIBLE_FLAGS,
    _SAFE_LOCAL_FLAGS,
    _bool_env,
    _int_env,
    _mode_capability_default,
    _posture_autonomy_default,
    _refreeze_compute_posture_for_tests,
    _refreeze_payment_approval_flags_for_tests,
    _safe_autonomy_default,
    reset_autonomy_mode_warnings,
)

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
        'mcp': _core_int_env('MCP_TIMEOUT_SECONDS', 180),       # MCP: network/subprocess latency
        'browser': _core_int_env('BROWSER_TIMEOUT_SECONDS', 120),   # Browser: page loads, DOM operations
        'filesystem': _core_int_env('FILESYSTEM_TIMEOUT_SECONDS', 30),  # Filesystem: fast local I/O
        'polymarket': _core_int_env('POLYMARKET_TIMEOUT_SECONDS', 60),  # Polymarket API
        'default': _core_int_env('DEFAULT_TOOL_TIMEOUT_SECONDS', 60),   # Default for unknown tools
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
        return _core_int_env('SUB_AGENT_TIMEOUT_SECONDS', 600)

    @classmethod
    def get_parallel_subtasks_timeout(cls) -> int:
        """Parallel subtasks timeout in seconds (from BotConfig)."""
        config = cls._get_config()
        if config:
            return config.parallel_subtasks_timeout
        return _core_int_env('PARALLEL_SUBTASKS_TIMEOUT_SECONDS', 900)

    @classmethod
    def get_max_concurrent_sub_agents(cls) -> int:
        """Maximum concurrent sub-agents (from BotConfig)."""
        config = cls._get_config()
        if config:
            return config.max_concurrent_sub_agents
        return _core_int_env('MAX_CONCURRENT_SUB_AGENTS', 3)

    @classmethod
    def get_max_sub_agent_depth(cls) -> int:
        """Maximum sub-agent nesting depth (from BotConfig)."""
        config = cls._get_config()
        if config:
            return config.max_sub_agent_depth
        return _core_int_env('MAX_SUB_AGENT_DEPTH', 1)

    @classmethod
    def get_max_async_sub_agents(cls) -> int:
        """Max concurrent BACKGROUND sub-agents (UP-12 delegate_task background=true).

        Bounds how many background delegation slots may be live at once. Floors at 1
        and never exceeds get_max_concurrent_sub_agents() (background + sync share the
        same SubAgentManager semaphore, so the background cap must not exceed the total).
        Env: MAX_ASYNC_SUB_AGENTS (default 2).
        """
        raw = _core_int_env('MAX_ASYNC_SUB_AGENTS', 2)
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
    STEP_TIMEOUT = _core_float_env('STEP_TIMEOUT_SECONDS', 300)       # 5 min per step
    STALL_TIMEOUT = _core_float_env('STALL_TIMEOUT_SECONDS', 300)     # 5 min stall detection

    # ========== LLM REQUEST TIMEOUTS ==========
    LLM_REQUEST_TIMEOUT = _core_int_env('LLM_REQUEST_TIMEOUT_SECONDS', 120)   # Standard LLM request
    LLM_STREAM_TIMEOUT = _core_int_env('LLM_STREAM_TIMEOUT_SECONDS', 300)     # Streaming needs longer
    LLM_BASE_TIMEOUT = _core_float_env('LLM_BASE_TIMEOUT_SECONDS', 30)        # Base (adjusted by tokens)

    # ========== BROWSER CLEANUP TIMEOUTS ==========
    BROWSER_CLOSE = _core_float_env('BROWSER_CLOSE_TIMEOUT', 3.0)
    BROWSER_CONTEXT_CLOSE = _core_float_env('BROWSER_CONTEXT_CLOSE_TIMEOUT', 8.0)
    BROWSER_INSTANCE_CLOSE = _core_float_env('BROWSER_INSTANCE_CLOSE_TIMEOUT', 5.0)
    PROCESS_KILL = _core_float_env('PROCESS_KILL_TIMEOUT', 2.0)

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

# The safe-minimum toolset every agent always needs (SSOT — was spelled out as a
# literal in orchestrator.py, goals/dispatcher.py and cron/runner.py; a shared
# constant stops those three from silently diverging). Immutable tuple — callers
# that need a mutable list do list(BASE_DEFAULT_TOOLS).
BASE_DEFAULT_TOOLS = ("filesystem", "task")

# The full autonomous grant under AUTONOMY_MODE=autonomous (proposal 013 §2.3):
# every research/content/comms/coding/receivables tool. NEVER money-spend
# (x402_pay/wallet/hyperliquid/polymarket) and NEVER host/compute tools
# (code_execution/shell/self_env — those ride AGENT_COMPUTE_POSTURE).
AUTONOMOUS_MODE_TOOLS = (
    "filesystem", "task", "web_fetch", "knowledge",
    "twitter", "email", "anysite", "perplexity",
    "browser", "mcp", "coding", "x402_invoice",
    "goal", "cronjob",
)

# MCP Tool Throttling (single source of truth)
# MCP actions (scraping, searches, APIs) are expensive and execute SEQUENTIALLY
# Each MCP action takes 30-180 seconds. Limiting prevents timeout cascades.
MAX_MCP_PER_STEP = _core_int_env('MAX_MCP_PER_STEP', 3)  # Configurable via env

# Context-compaction hysteresis (flow-efficiency D3-a)
# LLM compaction (llm_compact_history) is an EXTRA LLM call. Without a cooldown it
# re-fires every step once usage stays >=85% (a single large MCP result can re-cross
# the line each step), doubling call cost on long runs. Enforce a minimum step gap
# between LLM compactions; the >=95% emergency prune (non-LLM) remains the safety net.
COMPACTION_COOLDOWN_STEPS = _core_int_env('COMPACTION_COOLDOWN_STEPS', 3)

# Compaction payload tuning (Reference-parity context upgrade, 2026-06).
# These govern HOW the LLM compaction summarizes, not WHEN it fires (the tiered
# thresholds in CompactionManager own the trigger). Previously the summary input
# was silently truncated to the last 50 messages, each clipped to 500 chars, with
# tool results dropped entirely -> the summary lost most of what it claimed to keep.
COMPACTION_KEEP_RECENT = _core_int_env('COMPACTION_KEEP_RECENT', 10)  # min recent msgs kept verbatim
COMPACTION_TAIL_TOKEN_RATIO = _core_float_env('COMPACTION_TAIL_TOKEN_RATIO', 0.20)  # C3: tail kept by token budget too
COMPACTION_PER_MSG_CHARS = _core_int_env('COMPACTION_PER_MSG_CHARS', 3000)  # A2: per-message head+tail budget into summarizer
COMPACTION_TOOL_RESULT_CHARS = _core_int_env('COMPACTION_TOOL_RESULT_CHARS', 2000)  # A2: tool-result budget (was: dropped)
COMPACTION_MAX_SUMMARY_TOKENS = _core_int_env('COMPACTION_MAX_SUMMARY_TOKENS', 12000)  # A3: ceiling for summary budget
COMPACTION_MIN_SUMMARY_TOKENS = _core_int_env('COMPACTION_MIN_SUMMARY_TOKENS', 2000)  # A3: floor for summary budget
COMPACTION_MIN_SAVINGS_PCT = _core_float_env('COMPACTION_MIN_SAVINGS_PCT', 10.0)  # B4: anti-thrash back-off threshold
COMPACTION_CHECKPOINT = _core_bool_env('COMPACTION_CHECKPOINT', True)  # C2: dump pre-compaction trajectory
# A5: route the (expensive, repeated) summarization call to a cheaper auxiliary model.
# Empty -> use the main model (backward-compatible). Format: "provider:model" or "model".
COMPACTION_AUX_MODEL = os.getenv('COMPACTION_AUX_MODEL', '')

# Bounded tool-free reasoning turns (flow-efficiency D1-a, the seed resolution).
# POLYROB forces >=1 function call every step. This allows a SMALL number of tool-free
# "planning" turns to be treated as legitimate (gentle nudge) rather than a hard
# error on the very first empty response. The 3-consecutive thinking-loop
# escalation remains the hard backstop, so the loop bound is unchanged.
ALLOWED_REASONING_TURNS = _core_int_env('ALLOWED_REASONING_TURNS', 1)

# S-1: progressive skill disclosure. When ON (the DEFAULT — see the function below),
# only a compact <skill-catalog> (ids + one-line descriptions, ~0.15k) is injected and
# the agent pulls a skill's full body on demand via the load_skill(skill_id) tool. When
# OFF, skills are eager-injected full-body as a pinned user message (~3.2k tok).
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
- Call exactly one function per step unless several are truly independent; after an
  optional brief planning turn, include at least one function call each step.
- Use the exact tool names from the schema; do not invent wrapper names.
- To reply to the user and finish, call done(text=...). A single non-blocking
  send_message does not end your turn; if you only reply without acting for a couple of
  steps the runtime ends the turn for you.
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
- To greet/answer and finish, call done(text=...). A single non-blocking send_message
  does not end your turn; if you only reply without acting for a couple of steps the
  runtime ends the turn for you, so prefer done() to finish deliberately.
"""

# Substring → guidance. First match on the lowercased model name wins.
MODEL_FAMILY_INSTRUCTIONS = [
    ('kimi', KIMI_FAMILY_INSTRUCTIONS),
    ('gemini', GEMINI_FAMILY_INSTRUCTIONS),
    ('gpt', OPENAI_FAMILY_INSTRUCTIONS),
]

# T1-10: families that reliably emit the brain-state JSON without a per-step
# reminder. The prompt is authored for Claude (MODEL_FAMILY_INSTRUCTIONS gives it
# no family note), so the INJECT_FORMAT_HINT_EARLY native-mode nag is pure noise
# there — one wasted uncached message per step. Same needle mechanism as the
# family notes: matched on the model name (plus the resolved provider).
FORMAT_NAG_EXEMPT_FAMILIES = ('claude',)


def format_nag_exempt(model_name: str, provider: str = "") -> bool:
    """Whether the per-step brain-state format reminder should be skipped."""
    name = (model_name or "").lower()
    if any(needle in name for needle in FORMAT_NAG_EXEMPT_FAMILIES):
        return True
    return (provider or "").lower() == "anthropic"

# OpenRouter models that may need extra help
OPENROUTER_MODELS_NEEDING_HELP = [
    'grok',
    'mistral',
    'mixtral',
]