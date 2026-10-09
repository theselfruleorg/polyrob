"""Shared path-confinement helper (single source of truth).

Promoted from agents/task/agent/messages/context_references._is_within_root so the
coding + filesystem tools confine the same way. Uses realpath on both sides so an
in-root symlink (or `..` segment) can't smuggle a write outside the allowed root —
which the previous abspath().startswith(root) checks did not catch.

Also the ONE deny seam for agent file tools (security analysis 2026-09-23):

- H11 ``agent_file_refusal(..., write=True)`` refuses writes to paths the host
  or the harness EXECUTES or AUTO-LOADS: anything under a ``.git`` segment
  (``.git/config`` fsmonitor / ``.git/hooks/*`` = host RCE on the next git
  command), the project skill dirs (``.agents/skills/**``, ``.claude/skills/**``
  — auto-discovered by ``agents/task/agent/skill_discovery.py``) and the
  project-context files (``polyrob.md``/``POLYROB.md``/``AGENTS.md``/``CLAUDE.md``/
  ``.cursorrules`` — auto-loaded by ``agents/task/agent/core/project_context.py``)
  in any directory that walk visits. ``.git`` is refused for reads too (remote
  URLs carry tokens).
- H12 the data-home subtree (goals.db, cron.db, memory.db, message_history.json,
  pairing.db …) is refused for read AND write, except inside the session
  workspace when that workspace itself lives under the data home (the server
  shape). In the local CLI shape the data home is ``cwd/.polyrob`` INSIDE the
  writable root, which is exactly the case this closes.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional


def is_within_root(path: str, root: str) -> bool:
    """True iff ``path`` resolves to a location inside ``root`` (no symlink/`..` escape)."""
    try:
        rp = os.path.realpath(path)
        rr = os.path.realpath(root)
        return rp == rr or os.path.commonpath([rp, rr]) == rr
    except Exception:
        return False


# Mirrors project_context._CONTEXT_FILENAMES (agents tier; core may not import it).
# Compared lower-cased: a case-insensitive filesystem (macOS) loads `agents.md`
# as `AGENTS.md`. tests/unit/core/test_agent_file_refusal.py pins the parity.
PROJECT_CONTEXT_FILENAMES: frozenset[str] = frozenset({
    "polyrob.md", "agents.md", "claude.md", ".cursorrules",
})

# Consecutive segment pairs whose subtree is auto-discovered as skills.
SKILL_DIR_SEGMENTS: tuple[tuple[str, str], ...] = (
    (".agents", "skills"),
    (".claude", "skills"),
)


def _inside(path: str, root: str) -> bool:
    """Plain containment of two ALREADY-normalized absolute paths."""
    try:
        return path == root or os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def _candidates(path: str) -> list[str]:
    """The lexical absolute path and its realpath (an in-root symlink must not
    dodge a name rule)."""
    out = [os.path.abspath(path)]
    try:
        real = os.path.realpath(path)
        if real not in out:
            out.append(real)
    except OSError:
        pass
    return out


def _has_git_segment(parts_lower: list[str]) -> bool:
    return ".git" in parts_lower


def _in_skill_dir(parts_lower: list[str]) -> bool:
    for i in range(len(parts_lower) - 1):
        if (parts_lower[i], parts_lower[i + 1]) in SKILL_DIR_SEGMENTS:
            return True
    return False


def _is_project_context_file(path: str, root: str) -> bool:
    """A context-file NAME in a directory the project-context walk visits: the
    confined root itself, or the cwd or any of its ancestors (the walk goes from
    the cwd up to the git root)."""
    if os.path.basename(path).lower() not in PROJECT_CONTEXT_FILENAMES:
        return False
    parent = os.path.dirname(path)
    dirs = set()
    for base in (root, os.getcwd()):
        try:
            dirs.add(os.path.abspath(base))
            dirs.add(os.path.realpath(base))
        except OSError:
            continue
    if parent in dirs:
        return True
    # Ancestor of the cwd (the upward walk) — e.g. a workspace above the cwd.
    for cwd in {os.path.abspath(os.getcwd()), os.path.realpath(os.getcwd())}:
        if _inside(cwd, parent):
            return True
    return False


def default_data_homes() -> list[str]:
    """Realpaths of the resolved data home(s): the local rule
    (``POLYROB_DATA_DIR`` else ``cwd/.polyrob``) and the posture-aware one.
    Fail-open to [] — a path problem must never break the file tools."""
    homes: list[str] = []
    try:
        from core.runtime_paths import effective_data_home, resolve_data_home
        for fn in (resolve_data_home, effective_data_home):
            try:
                real = os.path.realpath(str(fn()))
            except Exception:
                continue
            if real not in homes:
                homes.append(real)
    except Exception:
        pass
    return homes


def data_home_refusal(path: str, workspace: str,
                      extra_homes: Optional[Iterable[str]] = None) -> Optional[str]:
    """H12: a reason string if *path* is inside a data home and NOT inside a
    session workspace that itself lives under that home; else None."""
    homes = default_data_homes()
    for extra in extra_homes or ():
        if not extra:
            continue
        try:
            real = os.path.realpath(str(extra))
        except OSError:
            continue
        if real not in homes:
            homes.append(real)
    if not homes:
        return None
    try:
        ws_real = os.path.realpath(workspace)
    except OSError:
        ws_real = os.path.abspath(workspace)
    for cand in _candidates(path):
        for home in homes:
            if not _inside(cand, home):
                continue
            # The workspace is the one exception, and only when the workspace
            # lives under the home (server shape). A workspace that CONTAINS the
            # home (local shape: cwd with cwd/.polyrob) grants nothing.
            if _inside(ws_real, home) and _inside(os.path.realpath(cand), ws_real):
                continue
            return "the agent's data home (runtime state is not editable by file tools)"
    return None


def agent_file_refusal(path: str, root: str, *, write: bool,
                       extra_homes: Optional[Iterable[str]] = None) -> Optional[str]:
    """The ONE deny seam for agent file tools (filesystem + coding).

    Returns a short reason when *path* must be refused, else None. *root* is the
    confined root (the session workspace). Checks the lexical path AND its
    realpath. Callers raise their own error type with the reason.
    """
    for cand in _candidates(path):
        parts_lower = [p.lower() for p in Path(cand).parts]
        if _has_git_segment(parts_lower):
            return "the .git directory"
        if write and _in_skill_dir(parts_lower):
            return "an auto-loaded skill directory (.agents/skills, .claude/skills)"
        if write and (".husky" in parts_lower or Path(cand).name.lower() in {
                ".mcp.json", ".pre-commit-config.yaml", "lefthook.yml", "lefthook.yaml"}
                or any(a == ".claude" and b in {"settings.json", "settings.local.json"}
                       for a, b in zip(parts_lower, parts_lower[1:]))):
            return "an auto-executed hook or agent configuration file"
        if write and _is_project_context_file(cand, root):
            return "an auto-loaded project-context file (AGENTS.md/CLAUDE.md/polyrob.md/.cursorrules)"
    return data_home_refusal(path, root, extra_homes)
