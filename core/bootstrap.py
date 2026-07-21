"""Application bootstrap — environment setup and container construction.

This is the single entry point for initializing the POLYROB platform.
Used by: api/app.py (FastAPI), cli/polyrob.py (CLI), tests.

Separating this from the FastAPI lifespan means any entry point
can construct a fully initialized container without spinning up uvicorn.
"""

import os
import sys
import importlib
import logging
from typing import Optional

from core.config_policy import embedder_needed
from dotenv import load_dotenv


_FALSEY_BACKFILL = {"0", "false", "off", "no", ""}
_SECRET_ALLOWLIST = {"ANYSITE_JWT", "MCP_GATEWAY_TOKEN"}


def _is_secret_key(name: str) -> bool:
    """A name we are willing to backfill: an API key or an allowlisted tool secret.

    Distinct from core.secrets.is_secret_key (display-redaction). This is a narrow BACKFILL selector.

    Deliberately excludes behaviour flags (POLYROB_LOCAL, *_ENABLED, worker/billing
    config) so the CLI never inherits production *flags* — only secrets.
    """
    return name.endswith("_API_KEY") or name in _SECRET_ALLOWLIST


def _backfill_provider_keys(config_dir: str = "config", env=None) -> None:
    """Copy ONLY secret keys from config/.env.{production,development} into *env*.

    Fires only when no LLM provider key is present (so the CLI would otherwise be
    stuck). override=False — never clobbers an already-set value. Reads the file
    with ``dotenv_values`` (parses to a dict, does NOT touch os.environ) and copies
    only whitelisted secret names, so production *flags* are never imported.
    """
    import os as _os
    from pathlib import Path as _Path
    from dotenv import dotenv_values
    # Lazy import: keep bootstrap's module-level import graph free of modules/llm.
    # Gate on *usable* keys so an env whose only provider key is unusable (deepseek,
    # or a malformed/too-short key that BotConfig will blank) still triggers backfill
    # of a real provider key from config/.env.* instead of being silently masked.
    from modules.llm.profiles import usable_providers_with_keys

    env = _os.environ if env is None else env
    if usable_providers_with_keys(env):
        return
    for cand in ("production", "development"):
        f = _Path(config_dir) / f".env.{cand}"
        if not f.exists():
            continue
        for k, v in dotenv_values(str(f)).items():
            if v and _is_secret_key(k) and not env.get(k):
                env[k] = v
        if usable_providers_with_keys(env):
            break


def _backfill_enabled() -> bool:
    return os.environ.get("POLYROB_ENV_KEY_BACKFILL", "1").strip().lower() not in _FALSEY_BACKFILL


def load_env(env: Optional[str] = None, config_dir: str = "config",
             local_mode: bool = False) -> str:
    """Load environment variables from .env files with proper layering.

    Server layer order (later overrides earlier): root .env >
    config/.env.{resolved} > config/.env.{resolved}.local.

    Local mode additionally loads ./.polyrob/.env then ~/.polyrob/.env FIRST, all
    with override=False so an explicit process env var always wins and project beats
    home. Precedence (high->low): process env > ./.polyrob/.env > ~/.polyrob/.env >
    legacy ~/.rob/.env (read-only transition fallback) > root .env >
    config/.env.{env} > config/.env.{env}.local.

    Environment resolution priority: CONFIG_ENV > env parameter > ENV var > 'development'
    Returns the resolved environment name.

    The candidate list + order is the SSOT ``core.paths.env_file_candidates`` (R-1);
    this function only applies the per-mode override semantics described above.
    """
    resolved = os.environ.get("CONFIG_ENV") or env or os.environ.get("ENV", "development")
    from core.paths import env_file_candidates

    if local_mode:
        # One-time ~/.rob -> ~/.polyrob home migration BEFORE any ~/.polyrob read
        # (copy-not-move, marker-gated, fail-open — never raises).
        try:
            from core.home_migration import migrate_rob_home_once
            migrate_rob_home_once()
        except Exception:
            pass
        # override=False, HIGHEST precedence first: the first file to set a key
        # keeps it, so the helper's order IS the precedence order (and an explicit
        # process env var always wins).
        for cand in env_file_candidates(resolved, local_mode=True, config_dir=config_dir):
            if cand.path.exists():
                load_dotenv(str(cand.path), override=False)
    else:
        # Server: lowest first with override=True — later loads win, so the
        # REVERSED helper list keeps config/.env.{env}.local as the top layer.
        for cand in reversed(env_file_candidates(resolved, local_mode=False, config_dir=config_dir)):
            if cand.path.exists():
                load_dotenv(str(cand.path), override=True)

    # Local-mode key backfill (Seam 3): if the CLI still has zero provider keys
    # after layering, source ONLY secret keys from config/.env.{production,development}
    # so `rob` "just works" with keys wherever they reasonably live — without ever
    # importing production flags. Server (local_mode=False) is never touched.
    if local_mode and _backfill_enabled():
        _backfill_provider_keys(config_dir)

    return resolved


def setup_project_path() -> str:
    """Ensure project root is on sys.path. Returns the project root."""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)
    return project_root


def setup_sqlite_compat():
    """Use pysqlite3 as sqlite3 replacement if available."""
    try:
        import pysqlite3
        sys.modules["sqlite3"] = pysqlite3
    except ImportError:
        pass


async def build_bot(
    env: Optional[str] = None,
    log_level: Optional[str] = None,
):
    """Construct and return a fully initialized CoreBot.

    Use this when you need the bot reference for cleanup (e.g. FastAPI lifespan).
    Use build_container() when you only need the DependencyContainer (e.g. tests).
    """
    from core.config import BotConfig
    from core.bot import Bot as CoreBot
    from core.logging import setup_logging, get_component_logger

    resolved_env = load_env(env)
    level = log_level or os.environ.get("LOG_LEVEL", "INFO")
    setup_logging(log_level=level)

    logger = get_component_logger("bootstrap")
    logger.info(f"Bootstrapping POLYROB platform (env={resolved_env})")

    config = BotConfig()
    bot = CoreBot(config=config)
    await bot.initialize()

    if not bot.container.has_service("path_manager"):
        from agents.task.path import get_path_manager
        bot.container.register_service("path_manager", get_path_manager(
            data_root=os.environ.get("DATA_ROOT", "./data/task"),
        ))

    logger.info("Bootstrap complete — container ready")
    return bot


async def build_container(
    env: Optional[str] = None,
    log_level: Optional[str] = None,
):
    """Construct and return a fully initialized DependencyContainer.

    Convenience wrapper around build_bot() for callers that don't need
    the bot reference (CLI, tests).
    """
    bot = await build_bot(env=env, log_level=log_level)
    return bot.container


# =============================================================================
# CLI tool registrar (I-1) — one generic loop over the tool descriptors, NOT a
# growing stack of hand-written per-tool blocks. Each block added here was a distinct
# prod incident (a tool registered a global descriptor but no CLI container service,
# so a session's `load_tools_from_container` logged "✗ Tool 'x' not found in
# container" and the agent silently ran without it — e.g. a FUNDED money goal that
# reasoned it had no wallet, 2026-07-08). The server path (core/initialization.py::
# initialize_tools) is already generic over get_tool_init_order(); this makes the CLI
# path generic the same way. Adding a new optional CLI tool is now a single row in
# _CLI_OPTIONAL_REGISTRARS — no new if-block, and the parity test
# (tests/unit/core/test_tool_registration_parity.py) fails loudly on any drift.
# =============================================================================

# Tools the lightweight CLI container CANNOT provide — they need the heavy server
# container (MCP clients, or paid-API creds baked into server config). This is the
# SSOT of CLI exclusions: the generic registrar skips exactly these; everything else
# in the tool descriptors is CLI-registerable.
# NOTE (2026-07-14): "email" was removed from this set — EmailTool's __init__ only
# touches config attrs (no network I/O; SMTP is tested lazily in the async
# _initialize(), which the CLI already defers until a session first loads the tool),
# so it needs no heavy server-only dependency. Leaving it excluded meant the `email`
# TOOL (agent-callable send action) never registered a container service on EITHER
# CLI-built process (`polyrob telegram` or `polyrob email`) even though creds were
# configured and the email SURFACE (harness, built separately in
# `cli/commands/email.py`) worked fine — every goal requesting `tools=["email"]` hit
# "✗ Tool 'email' not found in container" and reported a false "no credential"
# blocker (see docs/ops/rob-backlog.md 2026-07-14 ~07:1x tick).
# NOTE (2026-07-19): "browser"/"browser_manager" removed from this set for the SAME
# false-blocker reason — the actual Chromium/playwright launch was ALREADY lazy
# (`Browser._initialize()`'s own docstring: "browser instance will start on first
# use"), so excluding it bought nothing except a silent, misleading "browser tool not
# available" for every session that requested it (autonomous goals citing this as a
# blocker: sessions 53c43cad, 64e9088f, and the still-open a7ea136f3c9e). The real
# obstacle was structural, not weight: BrowserManager is a bare BaseComponent
# (`__init__(self, config=None)`), not a BaseTool, so it doesn't fit the generic
# loop's `cls(name, config, container)` calling convention — see the dedicated
# special-case block in `register_cli_tools` below (mirrors the identical special
# case already in `core/initialization.py::initialize_tools` on the server path,
# which is how this was confirmed safe rather than assumed).
# NOTE (2026-07-20, S3 dynamic tool rig — same precedent again): "mcp" removed.
# MCPTool.__init__ is config parsing only (attrs + `${VAR}` placeholder resolution,
# fail-open to defaults; server CONNECTIONS live in the deferred async initialize(),
# which the CLI already defers until a session first loads the tool). Registration
# is gated by _cli_extra_gate below (explicit MCP_ENABLED / autonomous-mode
# capability default / local server files) — absent secrets make the affected
# server connections fail loudly at load time (an owner ask), never a silent
# "not found in container".
_CLI_INCOMPATIBLE = {
    "perplexity",
    "collabland", "alchemy",
    "polymarket", "polymarket_data", "hyperliquid", "hyperliquid_data",
}

# Optional (flag/posture/creds-gated) tools the CLI can register. Each row is the
# module + the ``register_optional_tool()`` wrapper that MATERIALIZES the descriptor
# under the CURRENT env (self-gating — a no-op when its flag/posture is off), plus the
# container service name(s) it produces. This ONE table drives BOTH descriptor
# materialization (so the generic loop below sees the enabled optionals even when
# tools/__init__ was first imported with the flags off) AND the derived
# _CLI_REGISTERABLE_TOOLS capability set. `shell` registers two services (shell +
# process); `tools.x402` hosts two independent registrars.
_CLI_OPTIONAL_REGISTRARS = (
    ("tools.code_exec",        "register_code_exec_tool",    ("code_execution",)),
    ("tools.coding",           "register_coding_tool",       ("coding",)),
    ("tools.shell",            "register_shell_tools",       ("shell", "process")),
    ("tools.self_env",         "register_self_env_tool",     ("self_env",)),
    ("tools.git",              "register_git_tool",          ("git",)),
    ("tools.github",           "register_github_tool",       ("github",)),
    ("tools.cronjob_tools",    "register_cronjob_tool",      ("cronjob",)),
    ("tools.goal_tools",       "register_goal_tool",         ("goal",)),
    ("tools.knowledge_ingest", "register_knowledge_tool",    ("knowledge",)),
    ("tools.x402",             "register_x402_tool",         ("x402_pay",)),
    ("tools.x402",             "register_x402_invoice_tool", ("x402_invoice",)),
    ("tools.hf_deploy",        "register_hf_deploy_tool",    ("hf_deploy",)),
)

# Static (always-present) descriptors the CLI serves — the lightweight, dependency-free
# tools that are in tools/descriptors.py unconditionally. Kept explicit to avoid
# importing the heavy tools package at bootstrap import; the parity test asserts this
# stays == get_tool_init_order() - _CLI_INCOMPATIBLE once all descriptors materialize.
_CLI_STATIC_TOOLS = {"filesystem", "task", "web_fetch", "twitter", "anysite", "email",
                     "browser_manager", "mcp"}

# Service names produced by the optional registrars (derived from the one table above).
_CLI_OPTIONAL_TOOLS = {svc for _mod, _fn, services in _CLI_OPTIONAL_REGISTRARS for svc in services}

# The CLI capability set — what `rob` CAN register when the relevant flag is on. Derived
# from the two sources above (no independently-maintained flat list). Used by
# cli_unavailable_tools() for honest "tool not available on the CLI" feedback and by the
# startup self-check. Flag-INDEPENDENT (lists a tool even when its flag is currently off,
# so the user gets "enable the flag" rather than "not available"). The one non-derived
# addition: "browser" itself is never a descriptor in get_tool_init_order() (only
# "browser_manager" is — "browser" is the runtime alias the special-case block above
# registers once browser_manager initializes), so without this explicit addition an
# agent requesting tool_ids=["browser"] would get a false "not available on the CLI"
# from cli_unavailable_tools() even though it now actually registers.
_CLI_REGISTERABLE_TOOLS = set(_CLI_STATIC_TOOLS) | set(_CLI_OPTIONAL_TOOLS) | {"browser"}


def _materialize_cli_optional_descriptors() -> set:
    """Call every optional-tool registrar and return the service names that are CURRENTLY
    enabled (registrar returned True).

    Two jobs: (1) materialize each enabled tool's descriptor under the CURRENT env so the
    generic loop can see it via get_tool_init_order(); (2) return the freshly-evaluated
    enabled set so the loop honours the per-call gate instead of a descriptor's mere
    presence. Presence alone is insufficient because ``TOOL_DESCRIPTORS`` inserts are
    STICKY (never removed) — once posture-1 materialized ``shell``, a later posture-0 run
    in the same process would still see the descriptor. Gating on the registrar's return
    value re-checks the flag/posture gate exactly like the old per-block ``if enabled():``.

    Each registrar self-gates (``register_optional_tool`` returns False when the tool's
    ``enabled_fn`` is falsy). Fail-open per registrar: a broken optional seam must never
    break CLI startup. Deterministic regardless of when tools/__init__ was first imported.
    """
    log = logging.getLogger(__name__)
    enabled: set = set()
    for module_path, fn_name, services in _CLI_OPTIONAL_REGISTRARS:
        try:
            module = importlib.import_module(module_path)
            if getattr(module, fn_name)():
                enabled.update(services)
        except Exception as e:
            log.debug("CLI optional-tool registrar %s.%s skipped: %s", module_path, fn_name, e)
    return enabled


def _cli_extra_gate(name: str) -> bool:
    """Extra per-tool enablement for STATICALLY-present descriptors whose gate is NOT a
    ``register_optional_tool()`` insert (their descriptor is always in the init order):

      - twitter: registered only when X API credentials are configured. Reads work with
        valid creds; writes stay TWITTER_ENABLED-gated per-action. Without creds the tool
        is dead weight, so we don't register a useless service (matches the pre-I-1 block).
      - anysite: gated by ``anysite_cli_enabled()`` (ANYSITE_TOOL_ENABLED, default on).
      - mcp (S3, 2026-07-20): mirrors ``BotConfig._maybe_build_mcp``'s enablement —
        explicit MCP_ENABLED, else the autonomous-mode capability default, else the
        presence of local server files.

    Every other tool returns True — its presence in the init order already IS its gate
    (the optional ones are only inserted when enabled; the remaining static ones are
    unconditional CLI tools).
    """
    if name == "twitter":
        return bool(os.getenv("TWITTER_API_KEY") and os.getenv("TWITTER_ACCESS_TOKEN"))
    if name == "anysite":
        try:
            from tools.anysite import anysite_cli_enabled
            return anysite_cli_enabled()
        except Exception:
            return False
    if name == "mcp":
        try:
            from core.env import bool_env
            from core.config_policy import _mode_capability_default
            try:
                _mode_default = _mode_capability_default("MCP_ENABLED")
            except Exception:
                _mode_default = False
            if bool_env("MCP_ENABLED", _mode_default):
                return True
            from tools.mcp.config import load_local_mcp_servers
            return bool(load_local_mcp_servers())
        except Exception:
            return False
    return True


async def register_cli_tools(container) -> None:
    """Register the tools a lightweight CLI session can actually load — generically.

    A session's controller pulls tools via ``load_tools_from_container(tool_ids)``;
    anything not registered here is silently absent, leaving only the core
    ``done``/``send_message`` actions. ``build_cli_container`` skips heavy init
    (embeddings/RAG), so this registers every tool descriptor that is NOT in
    ``_CLI_INCOMPATIBLE`` — mirroring the server path (``initialize_tools``) but WITHOUT
    calling ``initialize()`` (the CLI defers tool init for fast startup; a tool inits
    lazily when a session first loads it). Per-tool fail-open: a broken optional tool
    must never break CLI startup. ``browser_manager`` is the one deliberate exception
    to "no eager initialize()" — see the special-case block below.
    """
    from utils.rate_limit_manager import RateLimitManager
    from tools.descriptors import get_tool_init_order, get_tool_class

    log = logging.getLogger(__name__)

    # rate_limit_manager first: filesystem/task/twitter/... declare it a required
    # dependency and raise on initialize() without it. It's a util (not a tool
    # descriptor), so it's registered AND initialized here, outside the generic loop.
    if not container.has_service("rate_limit_manager"):
        rlm = RateLimitManager(name="rate_limit_manager", config=container.config)
        await rlm.initialize()
        container.register_service("rate_limit_manager", rlm)

    # browser_manager special-case (2026-07-19, mirrors core/initialization.py's
    # identical special-case on the server path): BrowserManager is a bare
    # BaseComponent (`__init__(self, config=None)`), not a BaseTool, so it doesn't fit
    # the generic loop's `cls(name, config, container)` calling convention below (that
    # call would raise TypeError, silently caught by the loop's per-tool try/except —
    # indistinguishable from "not registered" without reading the debug log). Must
    # `await bm.initialize()` here (constructs a `Browser` wrapper object only — the
    # actual Chromium/playwright launch stays lazy per `Browser._initialize()`'s own
    # docstring, "browser instance will start on first use") so `bm.browser` is
    # populated and the `browser` alias below actually resolves, exactly like the
    # server does.
    if not container.has_service("browser_manager"):
        try:
            from tools.browser.browser_manager import BrowserManager
            bm = BrowserManager(config=container.config)
            await bm.initialize()
            container.register_service("browser_manager", bm)
            browser = getattr(bm, "browser", None)
            if browser is not None:
                container.register_service("browser", browser)
        except Exception as e:
            log.debug("Could not register CLI browser tool: %s", e)

    # Materialize the enabled optional descriptors (self-gating) so the loop below can
    # see them via get_tool_init_order(), and capture the freshly-evaluated enabled set.
    # This replaces the ~13 per-tool `register_*()` + `if enabled():` the old blocks did.
    enabled_optional = _materialize_cli_optional_descriptors()

    # THE generic registrar: one loop over the tool descriptors (the SSOT).
    for name in get_tool_init_order():
        if name in _CLI_INCOMPATIBLE or container.has_service(name):
            continue
        # Optional (flag/posture-gated) tools register only when their gate is CURRENTLY
        # on — not merely because a sticky descriptor exists. Static tools skip this check.
        if name in _CLI_OPTIONAL_TOOLS and name not in enabled_optional:
            continue
        # Extra gate for statically-present, non-register_optional_tool descriptors
        # (twitter creds / anysite flag).
        if not _cli_extra_gate(name):
            continue
        cls = get_tool_class(name)
        if cls is None:
            continue
        try:
            container.register_service(name, cls(name, container.config, container))
        except Exception as e:
            log.debug("Could not register CLI tool %s: %s", name, e)

    # Startup capability self-check (structural review #1): surface which registerable
    # tools actually resolved to a container service vs. which didn't. The twitter +
    # web_fetch outages (2026-07-01) were exactly this drift — a tool in the allowlist
    # that was never service-registered is silently unavailable. Log-only, fail-open.
    try:
        _resolved = sorted(t for t in _CLI_REGISTERABLE_TOOLS if container.has_service(t))
        _missing = sorted(t for t in _CLI_REGISTERABLE_TOOLS if not container.has_service(t))
        logging.getLogger(__name__).info(
            "🔎 CLI tool self-check: resolved=%s | not-registered=%s "
            "(latter is flag-gated or unavailable — investigate if you expected one here)",
            _resolved, _missing,
        )
    except Exception as e:
        logging.getLogger(__name__).debug("tool self-check skipped: %s", e)


def cli_unavailable_tools(requested):
    """Return requested tool ids the CLI container cannot provide (for honest UX)."""
    return [t for t in (requested or []) if t not in _CLI_REGISTERABLE_TOOLS]


def maybe_register_cli_embedder(container, config) -> None:
    """Lazily register the sentence-transformers embedding model in the CLI container.

    Gated on:
      - KB_ENABLED flag, OR
      - MEMORY_BACKEND=local_vector, OR
      - local mode (POLYROB_LOCAL)
    AND the service is not already registered.

    Mirrors core/initialization.py:432-439 (server path) but fail-opens to FTS-only
    rather than warning — the server warns because it has embedding config; the CLI
    treats absence as a graceful degradation.
    """
    if not embedder_needed() or container.has_service("embedding_model"):
        return

    # Fail-open to FTS-only when sentence-transformers isn't installed. find_spec is a
    # torch-free probe (it locates the package without importing it), so this stays cheap.
    import importlib.util
    try:
        _st_missing = importlib.util.find_spec("sentence_transformers") is None
    except (ImportError, ValueError):
        _st_missing = False  # present but spec-less (e.g. injected stub) → treat as available
    if _st_missing:
        logging.getLogger(__name__).debug("sentence_transformers unavailable; FTS-only")
        return

    try:
        # Register a LAZY embedder so the heavy torch/model load (and its ~8s HF Hub
        # network validation) does not block the prompt. It builds on first actual vector
        # use, from the local cache; if the active backend never needs vectors it never loads.
        from core.embedding import LazyEmbedder
        name = (config.get_embedding_config() or {}).get("model_name") or "all-MiniLM-L6-v2"
        container.register_service("embedding_model", LazyEmbedder(name))
    except Exception as e:
        logging.getLogger(__name__).debug("CLI embedder skipped, FTS-only: %s", e)


def _resolve_cli_data_home():
    """Resolve (data_home, workspace_is_project_root, project_root) for the CLI/local container.

    The isolation switch (doc 01/06): when ``POLYROB_DATA_DIR`` is set — the headless/
    server case — the runtime data home (goals.db/cron.db/memory.db + agent workspaces)
    lives THERE, OUTSIDE the code tree, and the workspace is under it (NOT cwd). This is
    what keeps a headless agent run out of ``/opt/polyrob`` (its own source + config/.env.*).

    Unset — the local-dev case — keeps today's behavior byte-identical: ``cwd/.polyrob`` as
    the data home and the workspace == cwd (the consented Claude-Code-style exception).

    Read via os.environ only (BotConfig.get is a getattr trap).
    """
    from pathlib import Path

    from core.runtime_paths import resolve_data_home

    # The data-home VALUE has ONE rule — core.runtime_paths.resolve_data_home
    # (POLYROB_DATA_DIR wins, else cwd/.polyrob). POLYROB_PROJECT_DIR never moves
    # the data home; it only picks the WORKSPACE placement below. That splits the
    # two concerns the old binary switch conflated — "data outside the code tree?"
    # (POLYROB_DATA_DIR) vs "sessions share one workspace?" (POLYROB_PROJECT_DIR).
    # This is the headless multi-session case the battle test needed
    # (docs/plans/2026-06-29-agent-working-directory-model-ANALYSIS.md).
    data_home = resolve_data_home()
    env_project = os.environ.get("POLYROB_PROJECT_DIR")
    if env_project:
        return data_home, True, str(Path(env_project).resolve())
    if os.environ.get("POLYROB_DATA_DIR"):
        return data_home, False, None
    return data_home, True, str(Path.cwd())


def _resolve_workspace_lock_dir(data_home, ws_is_project_root: bool, project_root) -> str:
    """Where the cross-process workspace turn lock lives.

    The lock must be keyed to the WORKSPACE it guards. In project-root mode that is
    the project folder — use a stable ``<project_root>/.polyrob`` so two processes
    sharing one POLYROB_PROJECT_DIR but with different POLYROB_DATA_DIR still
    serialize on the SAME lock file (MT-6). cwd-default: ``<cwd>/.polyrob`` == the
    data home, so this is byte-identical to legacy. Otherwise (per-session ephemeral)
    the lock lives at the data home, unchanged.
    """
    from pathlib import Path
    if ws_is_project_root and project_root:
        return str(Path(project_root) / ".polyrob")
    return str(data_home)


async def build_cli_container(
    env: Optional[str] = None,
    log_level: Optional[str] = None,
):
    """Lightweight container for CLI: config + LLM + TaskAgent only.

    Skips heavy initialization (embeddings, Pinecone, browser, RAG,
    character loading, torch, transformers) for fast startup.
    Only registers what TaskAgent needs to create and run sessions.
    """
    from core.config import BotConfig
    from core.container import DependencyContainer
    from core.logging import setup_logging

    # B2: capture whether the operator set the cap as a real launch-time override
    # (`MAX_SESSIONS_PER_USER=60 polyrob run …`) BEFORE load_env merges the .env file
    # default (MAX_SESSIONS_PER_USER=10) into os.environ — otherwise the .env value
    # is indistinguishable from a deliberate override.
    _cap_set_at_launch = "MAX_SESSIONS_PER_USER" in os.environ

    load_env(env, local_mode=True)
    # Terminal-native, single-user: mark the process local so the SAFE autonomy/
    # learning flags default ON as a group (core.config_policy._SAFE_LOCAL_FLAGS).
    # setdefault so an explicit `POLYROB_LOCAL=0 rob ...` can still opt out.
    os.environ.setdefault("POLYROB_LOCAL", "1")
    console_level = log_level or os.environ.get("LOG_LEVEL", "ERROR")
    # The terminal is a UI surface, not a log sink: console stays at
    # console_level (ERROR unless --verbose) while bot.log keeps LOG_LEVEL
    # (default INFO) diagnostics.
    setup_logging(
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        console_level=console_level,
    )

    config = BotConfig()
    # local CLI is single-user ('local'); the multi-tenant per-user session cap
    # (default 10) is the wrong constraint here and only causes false "Session
    # limit reached" friction after a handful of runs. Raise it for the local
    # container unless the operator overrode the cap at launch.
    if not _cap_set_at_launch:
        try:
            config.max_sessions_per_user = 1000
        except Exception:
            pass
    DependencyContainer._instance = None
    container = DependencyContainer.get_instance(config)

    # Project-scoped paths: cwd is the workspace, .polyrob/sessions holds artifacts.
    from pathlib import Path
    from agents.task.path import get_path_manager, set_path_manager
    # Isolation switch: POLYROB_DATA_DIR set (headless/server) → data home OUTSIDE the
    # code tree + workspace under it; unset (local dev) → cwd/.polyrob + workspace==cwd.
    rob_dir, _ws_is_project_root, _project_root = _resolve_cli_data_home()

    # Auto-gitignore .polyrob/ on first CLI use (not only on `rob init`) so a bare
    # `polyrob run`/`rob chat` inside a git repo doesn't leave .polyrob/ in `git status`.
    # Gated (default on), git-repo-guarded, fail-open.
    if os.environ.get("POLYROB_GITIGNORE_DOTROB", "1").strip().lower() not in ("0", "false", "off", "no"):
        try:
            from core.gitignore import ensure_polyrob_gitignored
            ensure_polyrob_gitignored(Path.cwd())
        except Exception:
            pass

    # Keep autonomy/memory state (goals.db, cron.db, memory.db) under the same
    # project-scoped .polyrob/ root as session artifacts, instead of a split-off ./data.
    try:
        config.data_dir = str(rob_dir)
    except Exception:
        pass

    # Cross-process workspace turn lock lives under .polyrob/ (C2). setdefault so an
    # operator override wins; only set in the CLI/local container, so the server
    # (which never calls build_cli_container) leaves the lock disabled.
    _lock_dir = _resolve_workspace_lock_dir(rob_dir, _ws_is_project_root, _project_root)
    os.makedirs(_lock_dir, exist_ok=True)
    os.environ.setdefault("POLYROB_WORKSPACE_LOCK_DIR", _lock_dir)

    path_manager = get_path_manager(
        data_root=str(rob_dir / "sessions"),
        workspace_is_project_root=_ws_is_project_root,
        project_root=_project_root,
    )
    # SEC-1: launching the persistent workspace in a secrets/code tree (cwd default)
    # exposes config/.env.* + source to the agent. Warn + steer to an explicit
    # --project/POLYROB_PROJECT_DIR away from the code tree. Opt-in hard refusal.
    if _ws_is_project_root and _project_root == str(Path.cwd()):
        try:
            from core.secret_scan import looks_like_secrets_tree
            hits = looks_like_secrets_tree(_project_root)
            if hits:
                msg = ("Persistent workspace is the current directory, which contains "
                       f"{hits}. The agent can read these. Point --project / "
                       "POLYROB_PROJECT_DIR at a dedicated folder outside your code tree.")
                if os.environ.get("POLYROB_PROJECT_SECRET_REFUSE", "0").strip().lower() \
                        not in ("0", "false", "off", "no", ""):
                    raise RuntimeError(msg)
                logging.getLogger(__name__).warning("SEC-1: %s", msg)
        except RuntimeError:
            raise
        except Exception:
            pass

    container.register_service("path_manager", path_manager)
    # Single source of truth: install the SAME instance as the process-global pm()
    # singleton, so the ~100 pm() utility call sites (clean_session_id, session
    # roots) resolve under .rob with no DATA_ROOT env shim and no mass migration.
    # The orchestrator pulls path_manager from the container; everything else uses
    # pm() — both are now this one project-scoped manager. (Server never calls
    # build_cli_container, so its global pm() stays the default ./data/task.)
    set_path_manager(path_manager)

    # One tenant oracle for the CLI (ME-D3): resolve_identity() prefers the bound
    # owner principal (POLYROB_OWNER_USER_ID/...) and falls back to "local" — the
    # same resolution `polyrob goals`/objectives use. Registering a fixed
    # ConstantIdentity here (seeded at container-build time, i.e. after env
    # setdefault has run) keeps chat sessions and the goal board on the SAME
    # tenant key instead of "local" vs the owner principal diverging.
    from core.identity import ConstantIdentity, resolve_identity
    container.register_service("identity", ConstantIdentity(resolve_identity()))

    # Register LLMManager — skip connection validation for fast startup.
    # Clients will validate lazily on first actual API call.
    from modules.llm.llm_manager import LLMManager
    from modules.llm.llm_client import LLMClient
    LLMClient._skip_validate = True
    llm = LLMManager(name='llm', config=config, container=container)
    await llm.initialize()
    LLMClient._skip_validate = False
    container.register_service('llm', llm)

    # Lazily register the embedding model when KB / local_vector / local-mode is active.
    maybe_register_cli_embedder(container, config)

    # §6.1/§6.2 (intelligence-stack finalization): register the database manager
    # so headless/CLI autonomous runs have (a) the x402 payment-request store —
    # modules/x402/invoicing._resolve_db raised "payment-request store
    # unavailable (no database service)" on the FIRST real invoice attempt — and
    # (b) metering truth: with a db present the orchestrator builds a
    # metering-only usage tracker (records real api_cost_usd into usage_records,
    # which the $-per-day autonomy budget gate reads; no credit deduction
    # without a balance_manager). Fail-open: a db init failure must never kill
    # CLI startup — those subsystems then degrade exactly as before.
    try:
        from core.initialization import MODULE_COMPONENTS
        database = MODULE_COMPONENTS['database_manager'](
            name='database_manager', config=config, container=container)
        await database.initialize()
        container.register_service('database_manager', database)
    except Exception:
        logging.getLogger(__name__).warning(
            "CLI database_manager init failed — x402 invoicing/metering degraded",
            exc_info=True)

    # Register the dependency-free tools a CLI session can load (filesystem, task).
    # Without these, `polyrob run --tools filesystem` has only core done/send_message.
    await register_cli_tools(container)

    # Register TaskAgent
    from agents.task_agent_lite import TaskAgent
    task_agent = TaskAgent(name="task_agent", config=config, container=container)
    await task_agent._initialize()
    container.register_agent("task_agent", task_agent)

    return container


async def build_server_bot(
    env: Optional[str] = None,
    log_level: Optional[str] = None,
):
    """Construct a CoreBot and register server-scope services on top.

    This is the entry point for the full server (api/app.py): it builds
    the pure-agent core (phases 1-5) and then registers auth, billing, and
    payment services (phase 6). CLI callers should use build_bot() instead
    to keep the container free of server-only services.
    """
    bot = await build_bot(env=env, log_level=log_level)
    await bot.initialize_server_services()
    return bot
