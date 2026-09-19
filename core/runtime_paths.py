"""Location: core/runtime_paths.py

One resolver for all path roots — the single seam that separates installed
**code** (read-only), **config** (secrets), and the **runtime workspace** the
agent can write to.

The whole point (doc 01 "runtime isolation"): on the SERVER/headless path,
``realpath(workspace_root)`` must NOT live under ``realpath(code_root)`` (the
install/code tree that also holds ``config/.env.production`` secrets), so even a
confinement miss in a file tool cannot reach the agent's own source or the master
secrets. The CLI *local* mode is the single documented exception — it keeps
Claude-Code-style CWD-as-workspace (consented, single-user).

This module is intentionally dependency-light and lives in ``core/`` (NOT on the
action-registration import path), so it carries no ``from __future__ import
annotations`` landmine. ``POLYROB_DATA_DIR`` is read via ``os.getenv`` only —
never ``BotConfig.get`` (which is a getattr that silently ignores the env).
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


# code_root == the install/code root: the parent of this ``core/`` package.
# Same value as ``core/config.py``'s ``base_dir`` (dirname(dirname(__file__))).
_CODE_ROOT = Path(__file__).resolve().parent.parent

# FHS default for a systemd service's mutable state. Used on the server path when
# no explicit POLYROB_DATA_DIR is given and the location is writable/creatable.
_SERVER_DATA_HOME = Path("/var/lib/polyrob")


@dataclass(frozen=True)
class RuntimePaths:
    """The four resolved path roots. Frozen — resolve once, read everywhere."""

    code_root: Path
    config_dir: Path
    data_home: Path
    workspace_root: Path


def _server_default_data_home() -> Path:
    """Server data-home when POLYROB_DATA_DIR is unset.

    Prefer FHS ``/var/lib/polyrob`` when its parent is writable (or it already
    exists), else fall back to ``~/.polyrob`` so a non-root / locked-down service
    user still gets a writable home OUTSIDE the code tree.
    """
    try:
        if _SERVER_DATA_HOME.exists():
            return _SERVER_DATA_HOME
        parent = _SERVER_DATA_HOME.parent
        if parent.exists() and os.access(str(parent), os.W_OK):
            return _SERVER_DATA_HOME
    except Exception:
        pass
    return Path.home() / ".polyrob"


def resolve_runtime_paths(*, local: bool) -> RuntimePaths:
    """Resolve (code_root, config_dir, data_home, workspace_root).

    Precedence (LOCKED — doc 01 T1):
      - code_root     = the install/code root (parent of ``core/``).
      - data_home     = ``POLYROB_DATA_DIR`` if set; elif local → ``cwd/.polyrob``;
                        else server → ``/var/lib/polyrob`` (if writable) else
                        ``~/.polyrob``.
      - config_dir    = ``code_root/config`` (server) or ``data_home`` (local).
      - workspace_root= ``cwd`` (local, project-root mode) or ``data_home/task``.
    """
    code_root = _CODE_ROOT

    env_data_dir = os.getenv("POLYROB_DATA_DIR")
    if env_data_dir:
        data_home = Path(env_data_dir).resolve()
    elif local:
        # Local home is the project-scoped ``.polyrob`` dir (doc 02 rename).
        data_home = (Path.cwd() / ".polyrob").resolve()
        # 027 WP5: running from $HOME would collapse the data home into the
        # CONFIG home (~/.polyrob — .env/auth.json beside the sidecar DBs,
        # a collision core/paths.py declares must not happen). Redirect to a
        # data/ subdir; an explicit POLYROB_DATA_DIR always wins above.
        try:
            from core.paths import polyrob_home
            if data_home == polyrob_home().resolve():
                data_home = data_home / "data"
        except Exception:
            pass
    else:
        data_home = _server_default_data_home().resolve()

    if local:
        config_dir = data_home
        workspace_root = Path.cwd().resolve()
    else:
        config_dir = code_root / "config"
        workspace_root = data_home / "task"

    return RuntimePaths(
        code_root=code_root,
        config_dir=config_dir,
        data_home=data_home,
        workspace_root=workspace_root,
    )


def resolve_data_home() -> Path:
    """Resolve the runtime DATA HOME (goals.db/cron.db/memory.db/surface_state.db…).

    This is the FIRST of the two path axes and must not be confused with
    :func:`resolve_session_data_root` below: services key their sidecar DBs off
    the *data home* (``POLYROB_DATA_DIR`` axis), sessions key artifacts off the
    *session tree* (PathManager/``DATA_ROOT`` axis).

    One policy, shared by every admin/console read of those DBs (webview
    ``pages``/``activity`` via ``webgate.data_dir()``, ``polyrob owner``,
    ``polyrob surface``): ``POLYROB_DATA_DIR`` wins, else converge on the
    CLI/agent home (``cwd/.polyrob``). This function is the SSOT for that rule:
    ``core.bootstrap._resolve_cli_data_home`` (build_cli_container) and
    ``core.runtime_config.get_data_root`` both delegate here, so admin verbs and
    the running daemons always read the SAME files (``POLYROB_PROJECT_DIR`` moves
    only the workspace, never the data home). The server-only
    ``/var/lib/polyrob`` default is deliberately NOT applied here: a headless
    deploy always sets ``POLYROB_DATA_DIR`` explicitly, and call sites
    historically converged on the CLI resolution when it is unset.
    """
    return resolve_runtime_paths(local=True).data_home


# The PathManager's legacy default. Since T10 (2026-07-16) a bare PathManager()
# delegates HERE (the RC-1 landmine — its constructor reading DATA_ROOT only —
# is closed); this constant remains the both-envs-unset terminal fallback.
_LEGACY_SESSIONS_DEFAULT = "./data/task"


def hmem_base_path(data_path: Optional[str] = None) -> Path:
    """The H-MEM (task context) base: ``<DATA_PATH>/auto`` when a caller passes
    one, else ``<data home>/auto``. Never CWD-relative (N1): ``DATA_PATH`` is
    not a declared BotConfig field, so on a real deploy the data-home branch is
    the ONLY one — the old CWD-relative default resolved against the read-only
    install tree under the hardened service identity, and every H-MEM save
    failed (2026-09-17)."""
    if data_path:
        return Path(data_path) / "auto"
    return resolve_data_home() / "auto"


def resolve_session_data_root() -> Path:
    """Resolve the session ARTIFACT tree root (PathManager ``data_root``).

    RC-1 (2026-07-07 webview full-control handoff): the agent process resolves
    its session tree via ``build_cli_container`` (``POLYROB_DATA_DIR`` →
    ``{data_home}/sessions``) and installs it as the global ``pm()``; the
    webview process never ran that bootstrap, so its ``pm()`` fell back to the
    PathManager default (env ``DATA_ROOT`` → ``./data/task``) — a DIFFERENT
    tree. Every non-agent process that reads session artifacts must resolve
    the root through THIS function so the trees cannot diverge again.

    Resolution order:
      1. Explicit ``DATA_ROOT`` — the PathManager's own env — always wins (an
         operator who set it meant it; existing DATA_ROOT test rigs unchanged).
      2. ``POLYROB_DATA_DIR`` set (headless/server data home) →
         ``{POLYROB_DATA_DIR}/sessions`` — exactly what ``build_cli_container``
         produces for the agent process (parity pinned by test).
      3. Neither set → the legacy ``./data/task``, byte-identical to a bare
         ``PathManager()``.

    The local-dev CLI branches (``POLYROB_DATA_DIR`` unset → ``cwd/.polyrob/
    sessions``) are deliberately NOT mirrored: this function runs in OTHER
    processes whose cwd is not the CLI's, so guessing would be wrong more
    often than the byte-identical legacy default.
    """
    explicit = os.getenv("DATA_ROOT")
    if explicit and explicit.strip():
        return Path(explicit).resolve()
    data_home = os.getenv("POLYROB_DATA_DIR")
    if data_home and data_home.strip():
        return Path(data_home).resolve() / "sessions"
    return Path(_LEGACY_SESSIONS_DEFAULT).resolve()


# --- WS-3 (2026-07-16): one seam for the scattered `x or "data"` CWD-write fallbacks ---

def data_dir_or_home(value: Optional[str]) -> str:
    """Return *value* if it is a non-empty path, else the resolved data home.

    The ONE replacement for the ~10 scattered ``getattr(cfg, "data_dir", None) or
    "data"`` / ``data_dir="data"`` fallbacks: when no container/config is present the
    fallback must be the data home (``POLYROB_DATA_DIR`` else ``cwd/.polyrob``), NEVER a
    relative ``"data"`` under the current working directory (a latent CWD/tree write).
    ``config.data_dir`` is absolute after bootstrap, so passing it through is a no-op.
    """
    if value:
        return str(value)
    return str(resolve_data_home())


def container_data_home(container: Optional[Any]) -> str:
    """The data home a container-bearing seat operates in: ``config.data_dir``
    when the container carries a config, else the resolved data home.

    The ONE replacement for the per-module ``getattr(container.config, "data_dir")``
    + :func:`data_dir_or_home` pair (group admin, room actions, room reads, the
    avatar action, the goal-board room service) — five spellings of one rule
    are how two seats start disagreeing about which home they mean. Accepts a
    Controller-like object too (``.container.config``) and ``None``.
    """
    cfg = getattr(container, "config", None) if container is not None else None
    if cfg is None and container is not None:
        inner = getattr(container, "container", None)
        cfg = getattr(inner, "config", None) if inner is not None else None
    return data_dir_or_home(getattr(cfg, "data_dir", None))


def effective_data_home() -> Path:
    """``<data_home>`` with the local-vs-server default split applied.

    :func:`resolve_data_home` is the LOCAL rule (``POLYROB_DATA_DIR`` else
    ``cwd/.polyrob``); a headless deploy that left the env unset lands on the
    server default instead. Per-tenant stores that must agree with the update
    snapshot/rollback paths (``cli/update/context.py``) resolve through here.
    """
    try:
        from core.config_policy import local_mode_enabled
        local = local_mode_enabled()
    except Exception:
        local = False
    return Path(resolve_runtime_paths(local=local).data_home)


def data_home_db_path(name: str, *, env_key: Optional[str] = None,
                      data_dir: Optional[str] = None,
                      prefer_container: bool = False) -> str:
    """Resolve a sidecar db that lives beside the autonomy DBs.

    Order: the *env_key* override (the test suite's seam to keep a db out of
    the developer's real home) → an explicit *data_dir* → the running
    container's ``config.data_dir`` when *prefer_container* → the data home.

    ``prefer_container`` names the dbs that HISTORICALLY resolved under the
    container's ``config.data_dir`` — on a server that is ``$POLYROB_DATA_DIR/
    data``, one level below the ``<data_home>/<name>`` layout ``core/
    db_manifest.py`` documents (artifacts, deployed_apps, autonomy_state). They
    converge on the manifest path with the read-both/write-new rule
    :func:`sidecar_db_path` applies: the manifest path wins when it exists, an
    EXISTING legacy container-path file keeps being used (history is never
    forked across two files), and a fresh install starts at the manifest path.
    An operator moves the legacy file to finish the convergence; nothing here
    moves data.
    """
    if env_key:
        override = os.getenv(env_key)
        if override:
            return override
    if data_dir:
        return os.path.join(str(data_dir), name)
    canonical = os.path.join(str(resolve_data_home()), name)
    if prefer_container and not os.path.exists(canonical):
        try:
            from core.container import DependencyContainer
            cfg = DependencyContainer.get_instance().get_service("config")
            cfg_dir = getattr(cfg, "data_dir", None)
            if cfg_dir:
                legacy = os.path.join(str(cfg_dir), name)
                if os.path.exists(legacy) and \
                        os.path.abspath(legacy) != os.path.abspath(canonical):
                    return legacy
        except Exception:
            pass
    return canonical


def prefs_home_dir() -> str:
    """Home for the IDENTITY axis: ``preferences.toml``, SOUL/SELF docs, chat
    overlays — everything under ``identity/{instance}/user_{uid}/``.

    Always the resolved data home, never a container's ``config.data_dir``.

    2026-09-15 prod review, C10: ``BotConfig.data_dir`` defaults to the relative
    string ``"data"`` and ``_ensure_directories`` anchors a relative default to
    ``POLYROB_DATA_DIR`` — so on prod ``config.data_dir`` is
    ``/var/lib/polyrob/data`` while the data home is ``/var/lib/polyrob``. Every
    preference WRITER (console ``/config``, ``polyrob config set``, the REPL, the
    agent's ``prefs`` action) resolves the data home; the delivery rail's readers
    derived theirs from ``config.data_dir``. ``delivery.daily_cap``,
    ``delivery.rate_per_hour`` and quiet hours were therefore written to one tree
    and read from another, so the owner's own remedy for "too many messages
    suppressed" changed nothing.

    Deliberately takes no argument: an identity path has ONE answer per process,
    and letting a caller pass a home is how the two axes drifted apart.
    """
    return str(resolve_data_home())


def goals_db_path(data_dir: Optional[str] = None) -> str:
    """Absolute path to the goal-board DB (``goals.db``).

    One helper for the four sites that re-joined ``{data_dir}/goals.db`` with their own
    home resolution + relative ``"data"`` fallback (cli/commands/goals.py,
    tools/goal_tools.py, cron/digest.py, tools/controller/approval_queue.py). Pass an
    explicit *data_dir* (e.g. the CLI's ``get_data_root()``) to pin it; omit it to use
    the data home.
    """
    return os.path.join(data_dir_or_home(data_dir), "goals.db")


def cron_db_path(data_dir: Optional[str] = None) -> str:
    """Absolute path to the cron job store (``cron.db``) — the ``goals_db_path`` twin
    for the scheduler's DB (tools/cronjob_tools.py + the operator ``seed_*`` scripts).
    """
    return os.path.join(data_dir_or_home(data_dir), "cron.db")


def sidecar_db_path(name: str) -> Path:
    """Durable sidecar DB home: ``<data_home>/<name>`` — the db_manifest axis (R-2 T1).

    ``telemetry_events.db`` and the ``messages.db`` mirror historically resolved
    under ``pm().data_root`` (the SESSION artifact tree — ``<data_home>/sessions``
    on prod-shaped installs) while ``core/db_manifest.py`` expects
    ``<data_home>/<name>``, so backups silently missed the live files. This helper
    is the ONE resolution rule for both.

    Legacy fallback (read-both, write-new): if the new path does not exist but the
    old session-tree location does, return the OLD path — an existing install keeps
    appending to its real file (history is never forked across two files) until the
    one-shot boot relocation (core/sidecar_relocate.py) or the operator moves it.
    A fresh install starts at the new path.
    """
    new = resolve_data_home() / name
    if new.exists():
        return new
    try:
        legacy = Path(resolve_session_data_root()) / name
        if legacy.exists():
            return legacy
    except Exception:
        pass
    return new
