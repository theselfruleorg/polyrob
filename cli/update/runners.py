"""Real (shell) implementations of the update steps for :mod:`cli.update.engine`.

Only the **git / editable-git** methods get an automated apply here — they are the local
dev + git-deployed server installs, and their code-swap + revert is a deterministic
`git` operation (`pull --ff-only` / `reset --hard <old_sha>`). pip/pipx/docker/systemd
stay on the printed manual path until each has a verified, reversible runner (a bad
`pip install -U` with no clean revert is worse than an honest manual step).

Every step shells out via an injected ``run`` (defaults to a checked subprocess) so the
construction is unit-testable and the real commands are auditable in one place.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Optional

from cli.update.detect import EDITABLE_GIT, GIT, InstallContext
from cli.update.engine import UpdateRunners

RunFn = Callable[[List[str], Optional[Path]], None]
CaptureFn = Callable[[List[str], Optional[Path]], str]


def _checked_run(cmd: List[str], cwd: Optional[Path]) -> None:
    """Run ``cmd``; raise ``CalledProcessError`` (with captured output) on non-zero."""
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True,
                   capture_output=True, text=True)


def _checked_capture(cmd: List[str], cwd: Optional[Path]) -> str:
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True,
                          capture_output=True, text=True).stdout.strip()


def build_runners(
    install_ctx: InstallContext,
    *,
    target_ref: Optional[str] = None,
    python: Optional[str] = None,
    run: Optional[RunFn] = None,
    capture: Optional[CaptureFn] = None,
) -> Optional[UpdateRunners]:
    """Build real runners for a self-updatable git/editable install, else ``None``.

    ``None`` means "no automated apply for this method" — the command falls back to the
    printed manual steps. ``run``/``capture``/``python`` are injectable for tests.

    ``target_ref`` is the release ref to move to (e.g. ``"v0.5.0"``). When set (the
    ``stable``/``pre`` channels), ``install()`` **checks out that tag** — the instance
    runs a *pinned tag* (detached HEAD; see ``docs/ops/POLYROB-OSS-OPERATIONS.md §3``),
    where ``git pull --ff-only`` FAILS ("not currently on a branch") and, on a branch,
    would pull unreviewed HEAD — neither is what a release update means. ``target_ref``
    is ``None`` only for the explicit ``--channel git`` branch-tracking mode, which keeps
    the ``git pull --ff-only`` fast-forward.
    """
    if install_ctx.method not in (GIT, EDITABLE_GIT) or not install_ctx.repo_root:
        return None
    repo = Path(install_ctx.repo_root)
    py = python or sys.executable
    _run: RunFn = run or _checked_run
    _capture: CaptureFn = capture or _checked_capture
    editable = install_ctx.method == EDITABLE_GIT
    pip_install = [py, "-m", "pip", "install", "-e", "."] if editable \
        else [py, "-m", "pip", "install", "."]

    # Capture the current commit up-front so a rollback can restore it exactly.
    old_sha = _capture(["git", "rev-parse", "HEAD"], repo)

    def install() -> None:
        if target_ref:
            # Tag-pinned release update: fetch the new tags and move (detached) HEAD to
            # the released tag. Works whether HEAD was detached (prod) or on a branch.
            _run(["git", "fetch", "--tags", "--force", "--quiet"], repo)
            _run(["git", "checkout", "--quiet", target_ref], repo)
        else:
            # --channel git: track the current branch by fast-forward.
            _run(["git", "pull", "--ff-only"], repo)
        _run(pip_install, repo)

    def migrate() -> None:
        _run([py, "-m", "migrations.migrate", "upgrade"], repo)

    def verify() -> None:
        # New code must at least import cleanly (the release smoke check).
        _run([py, "-c", "import core, cli.polyrob"], repo)

    def rollback_code() -> None:
        _run(["git", "reset", "--hard", old_sha], repo)
        _run(pip_install, repo)

    return UpdateRunners(install=install, migrate=migrate,
                         verify=verify, rollback_code=rollback_code)
