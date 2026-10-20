"""core/paths.py — the framework config-HOME seam.

One helper for the user-level config/state home (``~/.polyrob``) that holds
``.env``, ``cli.json``, ``mcp.json``, ``history`` and other per-user CLI state.

This is the **user config-HOME** seam — distinct from
``core/runtime_paths.py::resolve_runtime_paths`` which resolves the *project-scoped
DATA* root (``cwd/.polyrob``). Both default to a ``.polyrob`` name post-rename, but
they answer different questions: "where is the operator's config?" (here) vs. "where
does this run write its data?" (runtime_paths).

Intentionally dependency-light and free of any registry-closure import path, so it
carries no ``from __future__ import annotations`` landmine. ``POLYROB_HOME`` is read
via ``os.environ`` only.
"""

import os
from dataclasses import dataclass
from pathlib import Path


def polyrob_home() -> Path:
    """Return the framework config-home directory.

    Defaults to ``~/.polyrob``; overridable via the ``POLYROB_HOME`` env var
    (used for test isolation and operator relocation).
    """
    return Path(os.environ.get("POLYROB_HOME", str(Path.home() / ".polyrob")))


@dataclass(frozen=True)
class EnvFileCandidate:
    """One .env layer: where it lives and which precedence tier it is."""
    path: Path
    tier: str


def env_file_candidates() -> "list[EnvFileCandidate]":
    """THE canonical .env candidate list, in PRECEDENCE order (highest wins first).

    This is the single source of truth for which files configure the process and
    who beats whom — ``polyrob.app.bootstrap.load_env`` iterates exactly this list, and
    the display/snapshot/guard sites (``/config check``, ``cli.keys``,
    ``cli.update.context``) derive their subsets from it (R-1).

    ONE ladder for every entry point (080 WS-1 — the server ladder, where
    ``config/.env.*`` beat the process env, is deleted): process env > the
    instance file ``<deployed data home>/.env`` (only on a box with a deployed
    instance) > ``~/.polyrob/.env`` > legacy ``~/.rob/.env`` (read-only
    transition fallback). A project checkout is untrusted input: merely entering
    it must not replace providers or keys. load_env loads these first-to-last
    with override=False, so list order IS the precedence order.
    """
    from core.runtime_paths import instance_env_path
    instance = instance_env_path()
    top = [EnvFileCandidate(instance, "instance")] if instance is not None else []
    return top + [
        EnvFileCandidate(polyrob_home() / ".env", "home"),
        EnvFileCandidate(Path.home() / ".rob" / ".env", "legacy-home"),
    ]
