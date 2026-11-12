"""The ONE set of directory names a tree walk never descends into.

Caches, VCS metadata and installed dependencies: they hold no work of the
agent or the owner, and one of them can hold more entries than the rest of
the tree together. Every bounded walk (the workspace change scan, the code
search, the find-by-name fallback, the data-permission audit) imports this
set; a second copy drifts.
"""
from typing import Iterable, List

WALK_SKIP_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", "venv", ".venv", "site-packages",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
})


def prune_dirs(dirnames: Iterable[str], *, dot_dirs: bool = False) -> List[str]:
    """The directory names of one ``os.walk`` level that a walk may enter,
    sorted (a capped walk must cut at the same place each time).
    ``dot_dirs=True`` also drops every dot directory."""
    return sorted(
        d for d in dirnames
        if d not in WALK_SKIP_DIRS and not (dot_dirs and d.startswith("."))
    )
