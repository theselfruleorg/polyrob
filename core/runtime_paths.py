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


# The PathManager's own legacy default (agents/task/path.py reads DATA_ROOT,
# NOT POLYROB_DATA_DIR — the landmine at the heart of RC-1, 2026-07-07).
_LEGACY_SESSIONS_DEFAULT = "./data/task"


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
