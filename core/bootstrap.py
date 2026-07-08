"""Application bootstrap — environment setup and container construction.

This is the single entry point for initializing the POLYROB platform.
Used by: api/app.py (FastAPI), cli/polyrob.py (CLI), tests.

Separating this from the FastAPI lifespan means any entry point
can construct a fully initialized container without spinning up uvicorn.
"""

import os
import sys
import logging
from typing import Optional

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
    """
    resolved = os.environ.get("CONFIG_ENV") or env or os.environ.get("ENV", "development")
    from pathlib import Path

    if local_mode:
        # One-time ~/.rob -> ~/.polyrob home migration BEFORE any ~/.polyrob read
        # (copy-not-move, marker-gated, fail-open — never raises).
        try:
            from core.home_migration import migrate_rob_home_once
            migrate_rob_home_once()
        except Exception:
            pass

        # Load with override=False, HIGHEST priority first, so an explicit process
        # env var always wins and project (./.polyrob) beats home (~/.polyrob). With
        # override=False the first file to set a key keeps it, so the load order
        # IS the precedence order.
        from core.paths import polyrob_home
        proj_env = Path.cwd() / ".polyrob" / ".env"
        if proj_env.exists():
            load_dotenv(str(proj_env), override=False)  # project beats home
        home_env = polyrob_home() / ".env"
        if home_env.exists():
            load_dotenv(str(home_env), override=False)
        # Transition fallback (read-only): always read the legacy ~/.rob/.env at the
        # LOWEST precedence so an operator's live keys survive a fail-open migration.
        # override=False means it can never clobber the new home or process env.
        legacy_home_env = Path.home() / ".rob" / ".env"
        if legacy_home_env.exists():
            load_dotenv(str(legacy_home_env), override=False)

    # Layer 1: root .env (base defaults)
    if os.path.exists(".env"):
        load_dotenv(".env", override=False if local_mode else True)

    # Layer 2: environment-specific
    env_file = os.path.join(config_dir, f".env.{resolved}")
    if os.path.exists(env_file):
        load_dotenv(env_file, override=False if local_mode else True)

    # Layer 3: local overrides (not committed to git)
    local_file = os.path.join(config_dir, f".env.{resolved}.local")
    if os.path.exists(local_file):
        load_dotenv(local_file, override=False if local_mode else True)

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


async def register_cli_tools(container) -> None:
    """Register the dependency-free tools a CLI session can actually load.

    A session's controller pulls tools via ``load_tools_from_container(tool_ids)``;
    anything not registered here is silently absent, leaving only the core
    ``done``/``send_message`` actions. ``build_cli_container`` skips heavy init
    (embeddings/RAG/browser), but ``filesystem`` and ``task`` are pure-python
    (no torch/pinecone/browser), so they fit the CLI's fast-startup contract and
    make ``polyrob run --tools filesystem`` actually able to do file operations.

    These tools declare ``rate_limit_manager`` as a required dependency and raise
    on ``initialize()`` without it, so we register (and initialize) that first.
    """
    from utils.rate_limit_manager import RateLimitManager
    from tools.filesystem import FileSystem
    from tools.task_tool import TaskTool

    if not container.has_service("rate_limit_manager"):
        rlm = RateLimitManager(name="rate_limit_manager", config=container.config)
        await rlm.initialize()
        container.register_service("rate_limit_manager", rlm)
    if not container.has_service("filesystem"):
        container.register_service("filesystem", FileSystem("filesystem", container.config, container))
    if not container.has_service("task"):
        container.register_service("task", TaskTool("task", container.config, container))

    # web_fetch — lightweight stateless web retrieval (aiohttp + markdownify), no creds / heavy
    # deps. It's in _CLI_REGISTERABLE_TOOLS but was never actually registered as a service, so a
    # session that requested `--tools web_fetch` (or an autonomous goal) silently got no web_fetch
    # action. Register it unconditionally so headless/goal sessions can fetch public pages. Fail-open.
    try:
        from tools.web_fetch import WebFetchTool
        if not container.has_service("web_fetch"):
            container.register_service(
                "web_fetch", WebFetchTool("web_fetch", container.config, container)
            )
    except Exception as e:
        logging.getLogger(__name__).debug("Could not register web_fetch CLI tool: %s", e)

    # Autonomy tools (cronjob / goal) — opt-in via CRON_ENABLED / GOALS_ENABLED,
    # off by default. Register the descriptor+class globally (mirrors the server
    # path) AND a per-session service instance so a CLI session that does
    # `--tools cronjob`/`--tools goal` can actually reach them. Each is wrapped
    # fail-open: a missing/broken autonomy tool must never break CLI startup.
    from tools.cronjob_tools import cron_enabled
    if cron_enabled():
        try:
            from tools.cronjob_tools import CronJobTool, register_cronjob_tool
            register_cronjob_tool()
            if not container.has_service("cronjob"):
                container.register_service(
                    "cronjob", CronJobTool("cronjob", container.config, container)
                )
        except Exception as e:
            logging.getLogger(__name__).debug("Could not register cronjob CLI tool: %s", e)

    from agents.task.constants import AutonomyConfig
    if AutonomyConfig.goals_enabled():
        try:
            from tools.goal_tools import GoalTool, register_goal_tool
            register_goal_tool()
            if not container.has_service("goal"):
                container.register_service(
                    "goal", GoalTool("goal", container.config, container)
                )
        except Exception as e:
            logging.getLogger(__name__).debug("Could not register goal CLI tool: %s", e)

    # Twitter / X — register when credentials are configured so a headless/CLI
    # session (e.g. an autonomous goal with `--tools twitter`) can actually reach
    # the X surface. TwitterTool only needs `rate_limit_manager` (registered above)
    # + tweepy + creds; it does NOT need the heavy server container. Write actions
    # stay gated behind TWITTER_ENABLED per-action; reads need only valid creds.
    # Fail-open: a missing dep / bad creds must never break startup.
    if os.getenv("TWITTER_API_KEY") and os.getenv("TWITTER_ACCESS_TOKEN"):
        try:
            from tools.twitter_tool import TwitterTool
            if not container.has_service("twitter"):
                container.register_service(
                    "twitter", TwitterTool("twitter", container.config, container)
                )
        except Exception as e:
            logging.getLogger(__name__).debug("Could not register twitter CLI tool: %s", e)

    # Coding + code execution (H10-B) — opt-in via CODING_TOOLS_ENABLED /
    # CODE_EXEC_ENABLED, off by default. The LocalSubprocessBackend is pure
    # subprocess (no heavy server container needed), so these CAN run under `rob` —
    # this is what makes POLYROB a coding agent from the CLI. Fail-open like the others.
    from tools.code_exec import code_exec_enabled
    if code_exec_enabled():
        try:
            from tools.code_exec import register_code_exec_tool
            from tools.code_exec.tool import CodeExecutionTool
            register_code_exec_tool()
            if not container.has_service("code_execution"):
                container.register_service(
                    "code_execution", CodeExecutionTool("code_execution", container.config, container)
                )
        except Exception as e:
            logging.getLogger(__name__).debug("Could not register code_execution CLI tool: %s", e)

    from tools.coding import coding_tools_enabled
    if coding_tools_enabled():
        try:
            from tools.coding import register_coding_tool
            from tools.coding.tool import CodingTool
            register_coding_tool()
            if not container.has_service("coding"):
                container.register_service(
                    "coding", CodingTool("coding", container.config, container)
                )
        except Exception as e:
            logging.getLogger(__name__).debug("Could not register coding CLI tool: %s", e)

    from tools.anysite import anysite_cli_enabled
    if anysite_cli_enabled():
        try:
            from tools.anysite.tool import AnysiteTool
            if not container.has_service("anysite"):
                container.register_service(
                    "anysite", AnysiteTool("anysite", container.config, container)
                )
        except Exception as e:
            logging.getLogger(__name__).debug("Could not register anysite CLI tool: %s", e)

    # Git tool (SB-02) — structured git over the confined workspace. GIT_TOOLS_ENABLED
    # is in _SAFE_LOCAL_FLAGS (ON under POLYROB_LOCAL), but the tool was registered
    # NOWHERE (no register_cli_tools block, absent from the server init path), so an
    # advertised, safety-engineered capability was 100% dead code. Register it here so a
    # local `--tools git` request can actually reach it. git_push stays approval-gated +
    # leaf-blocked (Task 9). Fail-open like the others.
    from tools.git import git_enabled
    if git_enabled():
        try:
            from tools.git import register_git_tool
            from tools.git.tool import GitTool
            register_git_tool()
            if not container.has_service("git"):
                container.register_service(
                    "git", GitTool("git", container.config, container)
                )
        except Exception as e:
            logging.getLogger(__name__).debug("Could not register git CLI tool: %s", e)

    # GitHub tool (SB-02) — OFF by default even locally (GitHub writes are opt-in and
    # separately approval-gated). Registered only when GITHUB_TOOL_ENABLED.
    from tools.github import github_enabled
    if github_enabled():
        try:
            from tools.github import register_github_tool
            from tools.github.tool import GitHubTool
            register_github_tool()
            if not container.has_service("github"):
                container.register_service(
                    "github", GitHubTool("github", container.config, container)
                )
        except Exception as e:
            logging.getLogger(__name__).debug("Could not register github CLI tool: %s", e)

    # Compute-posture tools (computer-use parity WS-2/WS-3/WS-5) — the persistent
    # `shell` + `process` tools register at AGENT_COMPUTE_POSTURE>=1, `self_env` at
    # >=2. Like git above, they registered a descriptor/class but NO container service
    # on this (headless/POLYROB_LOCAL) path, so a goal's `load_tools_from_container`
    # found nothing ("✗ Tool 'shell' not found in container", live prod 2026-07-07).
    # Every action is still compute_posture_allows-gated in-session; registering the
    # service only makes the tool REACHABLE. Fail-open like the others.
    try:
        from tools.shell import shell_tools_enabled, register_shell_tools
        if shell_tools_enabled():
            from tools.shell.tool import ShellTool
            from tools.shell.process_tool import ProcessTool
            register_shell_tools()
            if not container.has_service("shell"):
                container.register_service(
                    "shell", ShellTool("shell", container.config, container)
                )
            if not container.has_service("process"):
                container.register_service(
                    "process", ProcessTool("process", container.config, container)
                )
    except Exception as e:
        logging.getLogger(__name__).debug("Could not register shell/process CLI tools: %s", e)

    try:
        from tools.self_env import self_env_enabled, register_self_env_tool
        if self_env_enabled():
            from tools.self_env.tool import SelfEnvTool
            register_self_env_tool()
            if not container.has_service("self_env"):
                container.register_service(
                    "self_env", SelfEnvTool("self_env", container.config, container)
                )
    except Exception as e:
        logging.getLogger(__name__).debug("Could not register self_env CLI tool: %s", e)

    # Knowledge base (Task 6) — opt-in via KB_ENABLED, ON under POLYROB_LOCAL (single-user
    # CLI gets the full knowledge feature by default). Provides kb_ingest/kb_search/
    # kb_list/kb_remove over the tenant-scoped KB. Fail-open like the others.
    from agents.task.constants import AutonomyConfig as _AutonomyConfig
    if _AutonomyConfig.kb_enabled():
        try:
            from tools.knowledge_ingest import KnowledgeTool, register_knowledge_tool
            register_knowledge_tool()
            if not container.has_service("knowledge"):
                container.register_service(
                    "knowledge", KnowledgeTool("knowledge", container.config, container)
                )
        except Exception as e:
            logging.getLogger(__name__).debug("Could not register knowledge CLI tool: %s", e)

    # Startup capability self-check (structural review #1): surface which registerable tools
    # actually resolved to a container service vs. which didn't. Tonight's twitter + web_fetch
    # outages were exactly this drift — a tool in the allowlist that was never service-registered
    # is silently unavailable to the agent. Log-only, fail-open — no behaviour change.
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


# Tools the lightweight CLI container can actually register. cronjob/goal/coding/
# code_execution are added conditionally in register_cli_tools (behind their flags);
# browser/mcp/perplexity/email need the heavy server container and are never
# available under `rob`. coding/code_execution use the pure-subprocess code_exec
# backend, so the lightweight container CAN provide them. knowledge uses the
# registry routers (pure-python, no heavy deps) so the CLI CAN provide it too.
_CLI_REGISTERABLE_TOOLS = {"filesystem", "task", "cronjob", "goal", "coding", "code_execution", "anysite", "knowledge", "web_fetch", "twitter", "git", "github", "shell", "process", "self_env"}


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
    from agents.task.constants import embedder_needed

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
    env_data = os.environ.get("POLYROB_DATA_DIR")
    env_project = os.environ.get("POLYROB_PROJECT_DIR")
    # Explicit project dir: persistent shared workspace, INDEPENDENT of where runtime
    # data lives. Splits the two concerns the old binary switch conflated — "data
    # outside the code tree?" (POLYROB_DATA_DIR) vs "sessions share one workspace?"
    # (POLYROB_PROJECT_DIR). This is the headless multi-session case the battle test
    # needed (docs/plans/2026-06-29-agent-working-directory-model-ANALYSIS.md).
    if env_project:
        data_home = Path(env_data).resolve() if env_data else (Path.cwd() / ".polyrob")
        return data_home, True, str(Path(env_project).resolve())
    if env_data:
        return Path(env_data).resolve(), False, None
    return Path.cwd() / ".polyrob", True, str(Path.cwd())


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
    # learning flags default ON as a group (agents.task.constants._SAFE_LOCAL_FLAGS).
    # setdefault so an explicit `POLYROB_LOCAL=0 rob ...` can still opt out.
    os.environ.setdefault("POLYROB_LOCAL", "1")
    level = log_level or os.environ.get("LOG_LEVEL", "ERROR")
    setup_logging(log_level=level)

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
            from cli.gitignore import ensure_polyrob_gitignored
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
