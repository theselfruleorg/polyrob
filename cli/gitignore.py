"""Back-compat shim (WS-5, 2026-07-16): the gitignore helper moved to core/gitignore.py
so core/bootstrap.py no longer imports upward into the cli tier. Import from
core.gitignore in new code."""
from core.gitignore import (  # noqa: F401
    ensure_polyrob_gitignored,
    ensure_rob_gitignored,
)
