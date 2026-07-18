"""Shadow-git per-file snapshot/restore (I-4 / H2, dedup decision D3).

Pure module: no tool coupling, no in-process state beyond the shadow git dir on
disk. Every subprocess call has a wall-clock timeout and a scrubbed env
(mirrors ``tools/git/tool.py``'s pattern: PATH/HOME/LANG/LC_ALL only +
``GIT_TERMINAL_PROMPT=0``).

Snapshots exactly ONE file per call — NEVER ``add -A`` / the whole tree —
because under POLYROB_LOCAL project-root mode the workspace IS the developer's
real repo. This module never decides *where* the shadow repo lives; the caller
picks ``snap_dir`` (see ``tools/coding/tool.py::_snapshot_dir`` — under default
POLYROB_LOCAL it lands in ``<cwd>/.polyrob/...``, physically under the
project-root workspace like all other ``.polyrob/`` session state). Safety
comes from mechanism, not placement: the shadow git dir is ``<snap_dir>/git``
(not ``.git``, so workspace repo auto-discovery never finds it), every call
passes an explicit ``--git-dir`` (the workspace's own ``.git`` is never
touched), ``.polyrob/`` is auto-gitignored (``core/bootstrap.py``, default-on),
and only the single touched file is ever staged.

Every function is a graceful no-op on ANY failure (git absent, timeout, weird
repo state, permission error, ...): ``snapshot_file``/``restore_file`` return
``None``/``False``, ``list_snapshots`` returns ``[]``. Nothing here ever raises.

LANDMINE: NO ``from __future__ import annotations`` — kept consistent with the
``tools/coding/`` package rule (registry param-model introspection landmine on
the action-closure module ``tool.py``), even though this module holds no
action closures itself.

⚠️ ``restore_file`` always runs ``git checkout <sha> -- <rel_path>`` with an
EXPLICIT pathspec after ``--`` — a bare trailing ``--`` detaches HEAD (known
repo landmine).
"""
import os
import subprocess
from typing import List, Optional

DEFAULT_TIMEOUT_SEC = 10.0
_GIT_SUBDIR = "git"  # <snap_dir>/git holds the shadow --git-dir (no worktree .git)


def _env() -> dict:
    """Scrubbed subprocess env: PATH/HOME/LANG/LC_ALL only (mirrors
    tools/git/tool.py), plus ``GIT_TERMINAL_PROMPT=0`` so a git call can never
    block on a credential prompt (all-local, but belt-and-suspenders)."""
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "LC_ALL") if k in os.environ}
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    return env


def _git_dir(snap_dir: str) -> str:
    return os.path.join(snap_dir, _GIT_SUBDIR)


def _run(snap_dir: str, workspace: str, args: List[str], timeout: float = DEFAULT_TIMEOUT_SEC):
    """Run ``git --git-dir=<snap_dir>/git --work-tree=<workspace> <args>``.

    Returns ``(returncode, stdout, stderr)``. ``returncode`` is ``None`` when
    git itself could not be invoked (missing binary) or the call timed out —
    callers uniformly treat ``returncode != 0`` (which ``None`` satisfies) as
    "this step failed."
    """
    argv = ["git", f"--git-dir={_git_dir(snap_dir)}", f"--work-tree={workspace}"] + list(args)
    try:
        proc = subprocess.run(
            argv, cwd=workspace, env=_env(), capture_output=True, text=True,
            timeout=timeout, check=False,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None, "", ""


def _ensure_repo(snap_dir: str, workspace: str) -> bool:
    """Init the shadow repo (``--git-dir`` form, no ``.git`` in the work tree)
    on first use. Returns True iff the repo is (now) usable."""
    if os.path.isfile(os.path.join(_git_dir(snap_dir), "HEAD")):
        return True
    try:
        os.makedirs(snap_dir, exist_ok=True)
    except OSError:
        return False
    code, _, _ = _run(snap_dir, workspace, ["init", "-q"])
    return code == 0


def _head_short_sha(snap_dir: str, workspace: str) -> Optional[str]:
    code, out, _ = _run(snap_dir, workspace, ["rev-parse", "--short", "HEAD"])
    if code != 0:
        return None
    sha = out.strip()
    return sha or None


def snapshot_file(snap_dir: str, workspace: str, rel_path: str) -> Optional[str]:
    """Commit the single file ``rel_path`` (relative to ``workspace``) into the
    shadow repo rooted at ``snap_dir``.

    Returns the short commit sha (a valid restore point). When the file is
    byte-identical to the last snapshot, nothing new is committed but the
    CURRENT HEAD short sha is still returned (a valid, if redundant, restore
    point) — this is detected via ``git diff --cached --quiet`` after
    staging, never by parsing git's English "nothing to commit" text.

    Returns ``None`` on any failure (git absent, timeout, init failure, the
    path not existing, ...) — NEVER raises.
    """
    try:
        if not _ensure_repo(snap_dir, workspace):
            return None
        code, _, _ = _run(snap_dir, workspace, ["add", "--", rel_path])
        if code != 0:
            return None
        # `diff --cached --quiet` exits 0 when the index already matches HEAD
        # for this path (nothing staged to commit) and 1 when there IS a
        # staged change to commit.
        code, _, _ = _run(snap_dir, workspace, ["diff", "--cached", "--quiet", "--", rel_path])
        if code == 0:
            return _head_short_sha(snap_dir, workspace)
        code, _, _ = _run(
            snap_dir, workspace,
            [
                "-c", "user.name=polyrob-snapshot",
                "-c", "user.email=snapshot@local",
                "commit", "-q", "-m", f"pre-edit {rel_path}", "--", rel_path,
            ],
        )
        if code != 0:
            return None
        return _head_short_sha(snap_dir, workspace)
    except Exception:
        return None


def list_snapshots(snap_dir: str, workspace: str, rel_path: Optional[str] = None) -> List[dict]:
    """Newest-first ``[{"id", "date", "subject"}, ...]`` snapshot history for
    ``rel_path`` (or the whole shadow history when ``rel_path`` is ``None``).

    ``[]`` on any failure (git absent, no repo yet, timeout, ...).
    """
    try:
        args = ["log", "--format=%h|%cI|%s"]
        if rel_path:
            args += ["--", rel_path]
        code, out, _ = _run(snap_dir, workspace, args)
        if code != 0:
            return []
        entries = []
        for line in out.splitlines():
            parts = line.split("|", 2)
            if len(parts) != 3:
                continue
            entries.append({"id": parts[0], "date": parts[1], "subject": parts[2]})
        return entries
    except Exception:
        return []


def restore_file(
    snap_dir: str, workspace: str, rel_path: str, snapshot_id: Optional[str] = None,
) -> bool:
    """Restore ``rel_path`` to ``snapshot_id`` (default: the latest snapshot
    touching it) via ``git checkout <sha> -- <rel_path>``.

    ALWAYS an explicit pathspec after ``--`` — a bare trailing ``--`` would
    detach HEAD instead of restoring one file (known repo landmine).

    Returns ``False`` on any failure, including "no snapshot exists for this
    file" — NEVER raises.
    """
    try:
        sha = snapshot_id
        if not sha:
            snaps = list_snapshots(snap_dir, workspace, rel_path)
            if not snaps:
                return False
            sha = snaps[0]["id"]
        code, _, _ = _run(snap_dir, workspace, ["checkout", sha, "--", rel_path])
        return code == 0
    except Exception:
        return False
