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
    python: Optional[str] = None,
    run: Optional[RunFn] = None,
    capture: Optional[CaptureFn] = None,
) -> Optional[UpdateRunners]:
    """Build real runners for a self-updatable git/editable install, else ``None``.

    ``None`` means "no automated apply for this method" — the command falls back to the
    printed manual steps. ``run``/``capture``/``python`` are injectable for tests.
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
