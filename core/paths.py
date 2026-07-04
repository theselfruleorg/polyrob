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
from pathlib import Path


def polyrob_home() -> Path:
    """Return the framework config-home directory.

    Defaults to ``~/.polyrob``; overridable via the ``POLYROB_HOME`` env var
    (used for test isolation and operator relocation).
    """
    return Path(os.environ.get("POLYROB_HOME", str(Path.home() / ".polyrob")))
