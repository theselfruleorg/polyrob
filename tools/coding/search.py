"""Pure grep/search logic for the coding tool (C2).

Python ``re`` walk over a directory tree, gitignore-aware, glob-scoped, bounded.
No subprocess here so the behaviour is deterministic and unit-testable; the tool
layer MAY prefer a ``ripgrep`` subprocess as a speed optimisation but this Python
walk is the correctness SSOT and fallback.

Gitignore support is a pragmatic subset: it always skips VCS/build noise
(``.git``/``__pycache__``/``node_modules``/``venv``/``.venv``/``.mypy_cache``)
and honours the *literal* entries + simple ``dir/`` and ``*.ext`` globs found in a
root-level ``.gitignore``. It is NOT a full gitignore implementation (no nested
ignore files, no negation) — documented intentionally.
"""
import fnmatch
import os
import re
from dataclasses import dataclass

_ALWAYS_SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", "venv", ".venv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
}


@dataclass
class Match:
    path: str
    line_no: int
    line: str


def _load_gitignore(root):
    """Return (dir_names, glob_patterns) parsed from a root-level .gitignore."""
    dir_names, globs = set(), []
    gi = os.path.join(root, ".gitignore")
    if not os.path.isfile(gi):
        return dir_names, globs
    try:
        for raw in open(gi, "r", encoding="utf-8", errors="ignore"):
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("!"):
                continue
            line = line.rstrip("/").lstrip("/") if line.endswith("/") else line.lstrip("/")
            if "/" in line:
                # only support a simple leading "dir/..." -> ignore the top dir segment
                line = line.split("/", 1)[0]
            if any(ch in line for ch in "*?["):
                globs.append(line)
            else:
                dir_names.add(line)
    except OSError:
        pass
    return dir_names, globs


def _is_ignored(name, ignored_dirs, ignored_globs):
    if name in ignored_dirs:
        return True
    return any(fnmatch.fnmatch(name, g) for g in ignored_globs)


def search_files(
    root,
    pattern,
    *,
    glob=None,
    output_mode="content",
    max_results=200,
    respect_gitignore=True,
):
    """Search ``root`` recursively for ``pattern`` (regex).

    output_mode="content" -> list[Match]; "files" -> list[str] of unique paths.
    Bounded by ``max_results``. Binary/undecodable files are skipped.
    """
    rx = re.compile(pattern)
    ignored_dirs, ignored_globs = (
        _load_gitignore(root) if respect_gitignore else (set(), [])
    )
    matches = []
    seen_files = []
    seen_set = set()

    for dirpath, dirnames, filenames in os.walk(root):
        # prune ignored / noise directories in-place
        dirnames[:] = [
            d for d in dirnames
            if d not in _ALWAYS_SKIP_DIRS
            and not (respect_gitignore and _is_ignored(d, ignored_dirs, ignored_globs))
        ]
        for fn in filenames:
            if respect_gitignore and _is_ignored(fn, ignored_dirs, ignored_globs):
                continue
            if glob and not fnmatch.fnmatch(fn, glob):
                continue
            full = os.path.join(dirpath, fn)
            try:
                with open(full, "r", encoding="utf-8") as f:
                    for i, line in enumerate(f, start=1):
                        if rx.search(line):
                            if output_mode == "files":
                                if full not in seen_set:
                                    seen_set.add(full)
                                    seen_files.append(full)
                                    if len(seen_files) >= max_results:
                                        return seen_files
                                break
                            matches.append(Match(path=full, line_no=i, line=line.rstrip("\n")))
                            if len(matches) >= max_results:
                                return matches
            except (OSError, UnicodeDecodeError):
                continue
    return seen_files if output_mode == "files" else matches
