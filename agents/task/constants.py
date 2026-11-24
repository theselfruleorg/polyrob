"""
Centralized constants for the Task agent system.
This provides a single source of truth for history management, trimming, and memory limits.
"""

import os

from core.env import bool_env as _core_bool_env
from core.env import int_env as _core_int_env

# Token counting defaults - Use modules.llm.token_counter for actual counting
# IMG_TOKENS kept for backward compatibility but should use token_counter._count_multimodal_tokens
IMG_TOKENS = 800  # Default estimate for image tokens (actual counting done by modules.llm)

# Default user ID for anonymous sessions. The canonical token lives in
# core.identity (the tenant SSOT); aliased here so the value is defined once.
from core.identity import ANON_USER_ID as DEFAULT_USER_ID  # noqa: E402

# Loop detection configuration
class LoopDetectionConfig:
    """Centralized loop detection configuration - Optimized for reliability"""
    # One threshold pair on every box (proposal 125 S34, D13). It used to read
    # ENVIRONMENT (default "development"), so every box without the name — every
    # local install — ran the aggressive 2/3 pair meant for debugging.
    MAX_REPETITIONS = _core_int_env('MAX_REPETITIONS', 3)
    UNCHANGED_STATE_THRESHOLD = _core_int_env('UNCHANGED_STATE_THRESHOLD', 4)

    # S7 (2026-08-29): the two consumed thresholds ARE the documented env knobs. They used
    # to be separate hard-coded twins with the same defaults, so MAX_REPETITIONS /
    # UNCHANGED_STATE_THRESHOLD were catalogued flags that gated nothing. Byte-identical
    # when the env is unset. (ACTION_SIMILARITY_THRESHOLD, DETECTION_WINDOW,
    # LOOP_WARNING_THRESHOLD, ALTERNATING_PATTERN_MIN had zero readers — removed.)
    MAX_ALLOWED_REPETITIONS = MAX_REPETITIONS
    STATE_CHANGE_THRESHOLD = UNCHANGED_STATE_THRESHOLD
    MEMORY_WINDOW_SIZE = 25  # Larger window for better context

# Memory management configuration
class MemoryConfig:
    """Memory optimization settings for AutoV2 - Optimized for performance"""
    MAX_SCREENSHOTS_IN_GIF = 10  # Reduced to prevent OOM
    MAX_HISTORY_SIZE = 30  # internal tuning (was the MAX_MEMORY_CACHE_SIZE env knob)
    SCREENSHOT_JPEG_QUALITY = 70
    ENABLE_GIF_CREATION = _core_bool_env('ENABLE_GIF_CREATION', False)  # Disabled by default
    CLEAR_SCREENSHOTS_AFTER_GIF = True  # Remove base64 data after GIF creation
    MAX_SCREENSHOT_SIZE_MB = 3  # Reduced max size per screenshot
    CLEANUP_INTERVAL = 50  # Clean every N operations

# History and Message Management
MAX_HISTORY_SIZE = 30  # Maximum number of messages to keep in history

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

# ========== AUX-MODEL ROUTER (A5/A1 + generalized) ==========
# Each provider's cheap auxiliary model is on its ProviderSpec (``aux_model``,
# ``aux_family_map``), used for compaction and the judge via resolve_aux_model() below.


def _spec_aux_model(provider, main_model):
    """The cheap aux model of *provider* for *main_model*, or None (=> the main model).
    ``main_model`` None => the operator's pinned model of that provider."""
    from modules.llm.provider_spec import get_spec
    spec = get_spec(provider)
    if spec is None or spec.aux_model or not spec.aux_family_map:
        return spec.aux_model if spec else None
    if not main_model:
        try:
            from core.runtime_config import operator_model_for
            main_model = operator_model_for(spec.name)
        except Exception:
            main_model = None
    name = str(main_model or "").strip().lower()
    for prefix, cheap in spec.aux_family_map:
        if name.startswith(prefix):
            return None if name == cheap else cheap
    return None


# 112 W2: the `light` model tier reads the same aux map (core cannot import modules).
from core.runtime_config import register_light_resolver as _register_light  # noqa: E402
_register_light(_spec_aux_model)


def _owner_tier_row(tier):
    """121: the instance owner's own heavy / light model (``/model heavy <name>``)."""
    from core.instance import resolve_owner_principal
    from core.model_pins import owner_tier
    return owner_tier(resolve_owner_principal(), tier)


from core.runtime_config import register_owner_tier_resolver as _register_owner_tier  # noqa: E402
_register_owner_tier(_owner_tier_row)


def _owner_main_row():
    """The instance owner's saved model (``/model <name>``): every run nobody pinned."""
    from core.instance import resolve_owner_principal
    from core.model_pins import owner_default
    return owner_default(resolve_owner_principal())


from core.runtime_config import register_owner_main_resolver as _register_owner_main  # noqa: E402
_register_owner_main(_owner_main_row)


# Per-task explicit override env var names (the slot names, AUX_MODEL_<SLOT>).
# UP-10 2.1: only `compaction` (live) and `judge` (wired into output validation,
# default-off/fail-open) remain. `planner`/`vision` had zero call sites and were
# removed as dead config surface.
_AUX_TASK_ENV = {
    "compaction": "AUX_MODEL_COMPACTION",
    "judge": "AUX_MODEL_JUDGE",
}


def _resolve_aux_model_with_origin(task, provider=None, main_model=None):
    """``(model, from_cheap_map)`` for aux `task`. See ``resolve_aux_model``.

    The origin flag matters because the cheap-map branch is the only one whose
    provider is KNOWN (it was keyed by the session provider), and an explicit env
    can name the very same model string — ``AUX_MODEL_REFLECTION=claude-haiku-4-5``
    on an anthropic session is byte-identical to that session's map value. Deciding
    provenance by comparing strings therefore mislabels a deliberate operator
    override as an auto-route.
    """
    explicit = os.getenv(_AUX_TASK_ENV.get(task, ""), "")
    if explicit:
        return explicit, False
    auto = _core_bool_env("AUX_AUTO", False)
    if auto and provider:
        mapped = _spec_aux_model(provider, main_model)
        return mapped, bool(mapped)
    return None, False


def resolve_aux_model(task, provider=None, main_model=None):
    """Which model should aux `task` use? (None => use the main model.)

    Precedence: 1) per-task explicit env (AUX_MODEL_JUDGE / AUX_MODEL_COMPACTION);
    2) provider cheap-map default when AUX_AUTO=true (on openrouter, keyed by
    *main_model*'s family);
    3) None.
    """
    return _resolve_aux_model_with_origin(task, provider, main_model)[0]


_AUX_SLOTS = ("compaction", "judge", "reflection")


def parse_provider_model(token, default_provider=None):
    """Parse one fallback-chain token ('provider/model' or bare 'model'). A token with
    its own catalog row is one model id (`deepseek/deepseek-v4-pro`), and the head is a
    provider only when a spec knows it (`z-ai/glm-5.3-flash` is one model id too)."""
    token = (token or "").strip()
    if not token:
        return None
    if "/" in token:
        head, tail = (part.strip() for part in token.split("/", 1))
        if not tail:
            return None
        if not head:
            return {"model": tail, "provider": default_provider}
        from modules.llm.model_registry import get_model_config_exact
        from modules.llm.provider_spec import get_spec
        if get_model_config_exact(token) is None and get_spec(head) is not None:
            return {"model": tail, "provider": head}
    return {"model": token, "provider": default_provider}


def resolve_aux_chain(task, provider=None, main_model=None):
    """Ordered aux-model candidates for `task` (primary first, then per-task fallbacks).

    B5: each of the 3 real aux
    call sites (compaction/judge/reflection) can be pointed at its own model+
    provider plus an ordered fallback list, instead of a single model string.
    Empty list => caller uses the main model (unchanged runtime contract). For
    `reflection`, whose caller has no main-model fallback, an empty chain with a known
    *main_model* returns that model instead (B-2).

    Env per slot (<SLOT> = task.upper()):
      AUX_MODEL_<SLOT>    primary model
      AUX_PROVIDER_<SLOT> primary provider
      AUX_FALLBACK_<SLOT> comma list of provider/model (or bare model) candidates

    `reflection` inherits compaction's config (model AND provider, as a pair) when
    its own env is unset (back-compat: reflection historically reused
    _provision_compaction_llm wholesale). If reflection sets its OWN model, none of
    compaction's config is consulted — including its provider, so a compaction
    provider never silently leaks onto a reflection-specific model.

    Provider precedence for reflection specifically:
      1) AUX_PROVIDER_REFLECTION (explicit reflection override)
      2) AUX_PROVIDER_COMPACTION — ONLY when reflection's model was itself
         inherited from compaction
    """
    if task not in _AUX_SLOTS:
        # Fixed slot set by design (the 3 real aux call sites) — an unknown task
        # never gets a chain, even under AUX_AUTO (dead-config trap guard, UP-10).
        return []
    slot = task.upper()
    inherited_from_compaction = False
    from_cheap_map = False
    if task == "reflection":
        # Reflection is NOT in _AUX_TASK_ENV, so resolve_aux_model("reflection", ...)'s
        # only possible effect is the global-AUX_AUTO cheap-map early-exit — which
        # would pre-empt the compaction inheritance below and drop the paired
        # AUX_PROVIDER_COMPACTION (pre-B5, reflection reused _provision_compaction_llm
        # wholesale, so AUX_AUTO + the compaction provider resolved as a pair). Go
        # straight from own-env to the compaction branch, which itself honors
        # AUX_AUTO via resolve_aux_model("compaction", ...).
        primary_model = os.getenv("AUX_MODEL_REFLECTION", "")
        if not primary_model:
            primary_model = os.getenv("AUX_MODEL_COMPACTION", "")
            if not primary_model:
                primary_model, from_cheap_map = _resolve_aux_model_with_origin(
                    "compaction", provider, main_model)
            if primary_model:
                inherited_from_compaction = True
    else:
        primary_model = os.getenv(f"AUX_MODEL_{slot}", "")
        if not primary_model:
            primary_model, from_cheap_map = _resolve_aux_model_with_origin(
                task, provider, main_model)
    if not primary_model:
        if task == "reflection" and main_model:
            # B-2 (2026-10-14): reflection has no "main model" fallback of its own
            # (ReflectionService with llm=None never runs), so on a box where no aux
            # model resolves — every openrouter/deepseek box — H-MEM reflection was
            # silently OFF. Run it on an isolated copy of the MAIN model instead, as
            # compaction and the judge already do.
            return [{"model": main_model, "provider": provider or None}]
        return []

    primary_provider = os.getenv(f"AUX_PROVIDER_{slot}") or None
    if task == "reflection" and not primary_provider and inherited_from_compaction:
        primary_provider = os.getenv("AUX_PROVIDER_COMPACTION") or None

    # A cheap-map model must stay on the SESSION's provider. Without this the
    # candidate carries provider=None, and `_create_llm_from_config_async` then
    # auto-detects the provider from the model registry — where every GLM id is
    # owned by `openrouter`. A zai-coding session would have silently moved its aux
    # calls OFF the flat-rate seat and onto a metered provider. Only pin a provider
    # the client registry actually knows (specs are the client keys).
    if not primary_provider and from_cheap_map:
        try:
            from modules.llm.provider_spec import get_spec
            spec = get_spec(provider)
            if spec is not None:
                primary_provider = spec.name
        except Exception:
            pass  # fail-open: legacy auto-detect

    chain = [{"model": primary_model, "provider": primary_provider}]

    raw_fb = os.getenv(f"AUX_FALLBACK_{slot}", "")
    if not raw_fb and task == "reflection" and inherited_from_compaction:
        raw_fb = os.getenv("AUX_FALLBACK_COMPACTION", "")
    for tok in raw_fb.split(","):
        cand = parse_provider_model(tok)
        if cand:
            chain.append(cand)
    return chain


# WS-1 (2026-07-16): the autonomy/mode/posture/payment-policy cluster + AutonomyConfig
# were relocated to the core tier (core/config_policy/policy.py) to break the
# core<->agents.task import cycle. They are re-exported here UNCHANGED so every existing
# `from agents.task.constants import ...` keeps working. New code imports from
# core.config_policy.
from core.config_policy import *  # noqa: F401,F403
from core.config_policy import (  # noqa: F401  (underscored + module-scope-used names)
    _AUTONOMY_LOCAL_FLAGS,
    _FALSEY,
    _MODE_CAPABILITY_FLAGS,
    _POSTURE_FULL_FLAGS,
    _POSTURE_OWNER_VISIBLE_FLAGS,
    _SAFE_LOCAL_FLAGS,
    _autonomy_enabled_default,
    _autonomy_group_default,
    _bool_env,
    _int_env,
    _mode_capability_default,
    _posture_autonomy_default,
    _refreeze_compute_posture_for_tests,
    _refreeze_payment_approval_flags_for_tests,
    _safe_autonomy_default,
    reset_autonomy_mode_warnings,
)

# 067 P4 prerequisite: the payment-approval views are lazy (built on first read,
# core/config_policy/payment_tools.py); the star import above does not carry them.
from core.config_policy import payment_tools as _payment_tools  # noqa: E402
from core.lazy_views import lazy_module_getattr as _lazy, reexport_map as _reexport  # noqa: E402

__getattr__ = _lazy(__name__, {}, _reexport(_payment_tools.__name__, _payment_tools.LAZY_VIEWS))

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
        'mcp': 180,  # MCP: network/subprocess latency
        'browser': 120,  # Browser: page loads, DOM operations
        'filesystem': 30,  # Filesystem: fast local I/O
        'polymarket': 60,  # Polymarket API
        # Publishing evaluation 2026-09-05 (Wave 1): the build tools' own foreground
        # ceiling (SHELL_MAX_TIMEOUT_SEC, default 600 since 073 W3) never fired on prod — this
        # 'default' 60 s cap killed every install/build first (tool_timeout x15).
        # Sit ABOVE the ceiling so the tool's own clean kill (with output) wins.
        'shell': _core_int_env('SHELL_TIMEOUT_SECONDS', 630),
        'code_execution': _core_int_env('CODE_EXEC_TIMEOUT_SECONDS', 630),
        # Coding-agent review B2: `coding.run_tests` runs on the same code_exec
        # backend with the same 600 s ceiling; under 'default' (60 s) the
        # controller killed every real suite first. Same flag, same value.
        'coding': _core_int_env('CODE_EXEC_TIMEOUT_SECONDS', 630),
        # Default for tools without a row. Was 60 s: on prod it cut the money
        # rail's reconcile (24x) and the X approval wait (9x) in a week to
        # 2026-10-09. 180 s matches MCP and stays below the 600 s step limit.
        'default': _core_int_env('DEFAULT_TOOL_TIMEOUT_SECONDS', 180),
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
    def get_sub_agent_timeout(cls) -> int:
        """Sub-agent timeout in seconds (internal tuning constant)."""
        return cls.SUB_AGENT_TIMEOUT

    @classmethod
    def get_parallel_subtasks_timeout(cls) -> int:
        """Parallel subtasks timeout in seconds (internal tuning constant)."""
        return cls.PARALLEL_SUBTASKS_TIMEOUT

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
    MAX_SUB_AGENT_DEPTH = 1

    # The step and stall limits are AgentConfig.step_timeout_seconds and
    # stall_timeout_seconds (agents/task/agent/service.py); the LLM request
    # limit is core.llm_limits. No timeout of those kinds lives here.

    # ========== BROWSER CLEANUP TIMEOUTS ==========
    # 067 F4: plain constants. They read BROWSER_CLOSE_TIMEOUT /
    # BROWSER_CONTEXT_CLOSE_TIMEOUT / BROWSER_INSTANCE_CLOSE_TIMEOUT /
    # PROCESS_KILL_TIMEOUT from the env, undocumented, and nothing in the tree
    # consumes them — an env knob with no effect. Values unchanged.
    BROWSER_CLOSE = 3.0
    BROWSER_CONTEXT_CLOSE = 8.0
    BROWSER_INSTANCE_CLOSE = 5.0
    PROCESS_KILL = 2.0

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


# Legacy constants for backward compatibility (deprecated - use TimeoutConfig instead)
BROWSER_CLOSE_TIMEOUT = TimeoutConfig.BROWSER_CLOSE
BROWSER_CONTEXT_CLOSE_TIMEOUT = TimeoutConfig.BROWSER_CONTEXT_CLOSE
BROWSER_INSTANCE_CLOSE_TIMEOUT = TimeoutConfig.BROWSER_INSTANCE_CLOSE
PROCESS_KILL_TIMEOUT = TimeoutConfig.PROCESS_KILL

# Agent Limits (defaults, can be overridden)
DEFAULT_MAX_FAILURES = 5  # Maximum consecutive failures before stopping

# The safe-minimum toolset every agent always needs (SSOT — was spelled out as a
# literal in orchestrator.py, goals/dispatcher.py and cron/runner.py; a shared
# constant stops those three from silently diverging). Immutable tuple — callers
# that need a mutable list do list(BASE_DEFAULT_TOOLS).
BASE_DEFAULT_TOOLS = ("filesystem", "task")

# A-14: the autonomous tool grant lives in core (core/config_policy/autonomous_tools.py);
# the names stay importable here.
from core.config_policy.autonomous_tools import (  # noqa: E402,F401
    AGENT_NFT_AUTONOMOUS_TOOLS,
    AUTONOMOUS_MODE_TOOLS,
    DEFI_AUTONOMOUS_TOOLS,
    DEFI_LAUNCH_AUTONOMOUS_TOOLS,
    X402_AUTONOMOUS_TOOLS,
    _agent_nft_registered,
    _x402_pay_constructed,
    autonomous_mode_tools,
    effective_autonomous_tools,
    stable_autonomous_toolset,
)


# MCP Tool Throttling (single source of truth)
# MCP actions (scraping, searches, APIs) are expensive and execute SEQUENTIALLY
# Each MCP action takes 30-180 seconds. Limiting prevents timeout cascades.
MAX_MCP_PER_STEP = _core_int_env('MAX_MCP_PER_STEP', 3)  # Configurable via env

# The context-compaction LADDER — the ONE place these three numbers live (F11).
# They were literals inside agent/core/step.py, and a second, window-scaled policy
# (CompactionManager, 0.35/0.45) was built on every session and consulted by
# nothing; F11(d) deleted that class. Do NOT add a third threshold — extend this
# ladder. Read as PERCENT of the context window, by the one gauge
# (MessageManager.get_context_usage_percent).
COMPACTION_WARN_PCT = 70        # log only, every 5 steps
COMPACTION_LLM_PCT = 85         # the EXTRA LLM call (llm_compact_history)
COMPACTION_EMERGENCY_PCT = 95   # the non-LLM prune; the overflow safety net

# Context-compaction hysteresis (flow-efficiency D3-a; widened by F11)
# LLM compaction (llm_compact_history) is an EXTRA LLM call, and the rebuild colds
# the whole conversation prefix. Without a cooldown it re-fires every step once
# usage stays >=85% (a single large MCP result can re-cross the line each step).
# Enforce a minimum step gap; the >=95% emergency prune (non-LLM) remains the
# safety net. 3 -> 8 on 2026-09-23 (F11): at 3 the band could fire again almost
# immediately, paying a cold prefix for a cut that had not yet earned it.
COMPACTION_COOLDOWN_STEPS = _core_int_env('COMPACTION_COOLDOWN_STEPS', 8)

# F11 progress rule: the cooldown is a floor, not a wall. A compaction
# that ACTUALLY cut this fraction of the context has earned the right to run again
# inside the window; one that did not must wait it out. Reuses the savings ratio
# the anti-thrash counter already records (compactor._record_compaction_savings),
# so there is one number, not a second measurement.
COMPACTION_PROGRESS_FLOOR = 0.05

# Compaction payload tuning (Reference-parity context upgrade, 2026-06).
# These govern HOW the LLM compaction summarizes, not WHEN it fires (the >=85/95 %
# ladder in agent/core/step.py owns the trigger). Previously the summary input
# was silently truncated to the last 50 messages, each clipped to 500 chars, with
# tool results dropped entirely -> the summary lost most of what it claimed to keep.
COMPACTION_KEEP_RECENT = 10  # min recent msgs kept verbatim
COMPACTION_TAIL_TOKEN_RATIO = 0.20  # C3: tail kept by token budget too
COMPACTION_PER_MSG_CHARS = 3000  # A2: per-message head+tail budget into summarizer
COMPACTION_TOOL_RESULT_CHARS = 2000  # A2: tool-result budget (was: dropped)
COMPACTION_MAX_SUMMARY_TOKENS = 12000  # A3: ceiling for summary budget
COMPACTION_MIN_SUMMARY_TOKENS = 2000  # A3: floor for summary budget
COMPACTION_MIN_SAVINGS_PCT = 10.0  # B4: anti-thrash back-off threshold
# Bounded tool-free reasoning turns (flow-efficiency D1-a, the seed resolution).
# POLYROB forces >=1 function call every step. This allows a SMALL number of tool-free
# "planning" turns to be treated as legitimate (gentle nudge) rather than a hard
# error on the very first empty response. The 3-consecutive thinking-loop
# escalation remains the hard backstop, so the loop bound is unchanged.
ALLOWED_REASONING_TURNS = _core_int_env('ALLOWED_REASONING_TURNS', 1)

# NOTE: the catalog-include-all policy is the access-time `skill_catalog_include_all()`
# function below (defaults ON). A dead module-level constant that defaulted to 'false'
# used to live here and was a decoy — no caller read it while the function returned True
# (Phase 0.6: removed to end the "is it on or off?" confusion).


def skill_catalog_include_all() -> bool:
    """Whether the <skill-catalog> short index lists every skill this session may
    load (default ON) or only the pinned ones (``SKILL_CATALOG_INCLUDE_ALL=false``).

    Only pinned skills load at session start (081 Q2); the index (one
    ``polyrob-when`` line each, bounded by ``get_catalog_skills(max_skills)``) lets
    the agent ``load_skill`` what the task needs.
    Access-time read (not the import-bound constant) so an env override always wins.
    """
    return _bool_env("SKILL_CATALOG_INCLUDE_ALL", True)

# S1 (chat consolidation): the persona/<identity> injection gate lives in the
# access-time `task_personality_block_enabled()` function above (there is no
# module-level constant here — see that docstring for the full rationale).

# S2 (chat consolidation): tool-light "chat mode" for TaskAgent.chat_once. The
# toolset must be NON-EMPTY — an empty tool_ids list trips the orchestrator's
# comprehensive-default fallback (loads browser/mcp/etc). "task" is the harmless
# TODO tool; chat mode otherwise relies on send_message/done + conversational-exit.
CHAT_TOOL_IDS = [t.strip() for t in os.getenv('CHAT_TOOL_IDS', 'task').split(',') if t.strip()] or ['task']
# 2026-10-29: 8 → 20 — a card wake that pays and closes out, or a chat turn that reads
# then answers, did not fit in 8; the loop breaker ends a degenerate turn early.
CHAT_MAX_STEPS = _int_env('CHAT_MAX_STEPS', 20)

# Chat consolidation (HANDOFF-C, 2026-06-19): the HTTP /api/chat/message endpoint
# is now served SOLELY by the unified task agent (TaskAgent.chat_once); the legacy
# ChatAgent was retired, so there is no toggle/fallback target anymore.
#
# The chat path does NO credit pre-check (matching the legacy behavior), so
# chat_once skips create_session's pre-check by default (per-call billing still
# applies). Set CHAT_SKIP_CREDIT_CHECK=off to apply the credit gate to chat.
CHAT_SKIP_CREDIT_CHECK = os.getenv('CHAT_SKIP_CREDIT_CHECK', 'true').strip().lower() not in ('0', 'false', 'no', 'off', 'none', '')

# Session Management
# NOTE (B3): the per-user session cap SSOT is BotConfig.max_sessions_per_user
# (core/config.py, default 10), read at runtime in task_agent_lite. A duplicate
# `MAX_SESSIONS_PER_USER = 100` used to live here but had ZERO call sites and only
# misled readers — removed. Do not reintroduce a second cap constant here.

# ---------------------------------------------------------------------------
# P-1: per-model-family operational guidance.
# Short, high-signal nudges appended to the system prompt for non-Anthropic
# families whose tool-calling/agent behavior benefits from explicit framing.
# Kept terse so the cached system prefix stays small. Anthropic/Claude needs
# none (the prompt is authored for it).
# ---------------------------------------------------------------------------
GEMINI_FAMILY_INSTRUCTIONS = """## MODEL NOTE (Gemini)
- Call exactly one function per step unless several are truly independent; after an
  optional brief planning turn, include at least one function call each step.
- Use the exact tool names from the schema; do not invent wrapper names.
- To answer the user, call send_message(text=...), then call done in the same step.
  done(text) is your internal record; the user never reads it.
"""

OPENAI_FAMILY_INSTRUCTIONS = """## MODEL NOTE (GPT)
- Emit native function calls, not JSON-in-text. After an optional brief planning
  turn, include at least one function call every step.
- Prefer one decisive action per step over long deliberation.
- To answer the user, call send_message(text=...), then call done in the same step.
  done(text) is your internal record; the user never reads it.
"""

KIMI_FAMILY_INSTRUCTIONS = """## MODEL NOTE (Kimi)
- Return tool calls as structured function calls only — never write tool-call
  delimiter tokens (e.g. <|tool_call_begin|>) into your text content.
- One function call per step is enough; don't repeat the same message across steps.
- To greet or answer, call send_message(text=...), then call done in the same step.
  done(text) is your internal record; the user never reads it.
"""

# Substring → guidance. First match on the lowercased model name wins.
MODEL_FAMILY_INSTRUCTIONS = [
    ('kimi', KIMI_FAMILY_INSTRUCTIONS),
    ('gemini', GEMINI_FAMILY_INSTRUCTIONS),
    ('gpt', OPENAI_FAMILY_INSTRUCTIONS),
]

# T1-10: families that reliably emit the brain-state JSON without a per-step
# reminder. The prompt is authored for Claude (MODEL_FAMILY_INSTRUCTIONS gives it
# no family note), so the native-mode format nag is pure noise
# there — one wasted uncached message per step. Same needle mechanism as the
# family notes: matched on the model name (plus the resolved provider).
FORMAT_NAG_EXEMPT_FAMILIES = ('claude',)


def format_nag_exempt(model_name: str, provider: str = "") -> bool:
    """Whether the per-step brain-state format reminder should be skipped."""
    name = (model_name or "").lower()
    if any(needle in name for needle in FORMAT_NAG_EXEMPT_FAMILIES):
        return True
    return (provider or "").lower() == "anthropic"
