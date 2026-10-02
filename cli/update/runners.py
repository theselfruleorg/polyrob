"""Real (shell) implementations of the update steps for :mod:`cli.update.engine`.

Only the **git / editable-git** methods get an automated apply here — they are the local
dev + git-deployed server installs, and their code-swap + revert is a deterministic
`git` operation (`pull --ff-only` / `reset --keep <old_sha>`). pip/pipx/docker/systemd
stay on the printed manual path until each has a verified, reversible runner (a bad
`pip install -U` with no clean revert is worse than an honest manual step).

Every step shells out via an injected ``run`` (defaults to a checked subprocess) so the
construction is unit-testable and the real commands are auditable in one place.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Optional

from cli.update.detect import EDITABLE_GIT, GIT, InstallContext
from cli.update.engine import UpdateRunners

RunFn = Callable[[List[str], Optional[Path]], None]
CaptureFn = Callable[[List[str], Optional[Path]], str]

#: Post-install asset check — run in the UPDATED interpreter, so it resolves the
#: files exactly the way the runtime will (off the installed `modules.cards` /
#: `webview` packages, never off the source tree the updater happens to sit in).
#:
#: These three are load-bearing, not decoration: the brand mark is the invoice
#: card's header when the instance has no avatar, and the two DejaVu faces are
#: what keep an invoice card from rendering in PIL's default bitmap font. Pinned
#: alongside `tests/test_shipped_assets_ratchet.py`, which enforces the same
#: files at the declaration level.
#: ⚠️ `sys.exit(...) if missing else None`, NOT `raise ... if missing else None`
#: — the latter parses as `raise (X if missing else None)` and raises None,
#: which is a TypeError on the HEALTHY path. Found by executing the probe;
#: the string-matching unit test was perfectly happy with it.
_ASSET_PROBE = (
    "import pathlib, sys, modules.cards as p; "
    "r = pathlib.Path(p.__file__).resolve().parents[2]; "
    "missing = [str(x) for x in ("
    "r / 'assets' / 'brand' / 'polyrob-mark-256.png', "
    "r / 'assets' / 'fonts' / 'dejavu' / 'DejaVuSans.ttf', "
    "r / 'assets' / 'fonts' / 'dejavu' / 'DejaVuSans-Bold.ttf') "
    "if not x.is_file()]; "
    "sys.exit('runtime assets missing after install: ' + ', '.join(missing)) "
    "if missing else None"
)


def _checked_run(cmd: List[str], cwd: Optional[Path]) -> None:
    """Run ``cmd``; raise ``CalledProcessError`` (with captured output) on non-zero."""
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True,
                   capture_output=True, text=True)


def _checked_capture(cmd: List[str], cwd: Optional[Path]) -> str:
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True,
                          capture_output=True, text=True).stdout.strip()


def git_branch_status(install_ctx: InstallContext, *,
                      capture: Optional[CaptureFn] = None) -> dict:
    """``--channel git``: is the checked-out BRANCH behind its upstream?

    The release list (GitHub tags) is the wrong oracle for this channel — a branch
    with unreleased commits reported "Already up to date" and a detached HEAD
    reported an update, then failed at ``git pull``. Returns
    ``{"ok", "reason", "behind", "head", "upstream", "branch"}``; never raises on a
    git error (``ok=False`` with the reason).
    """
    if install_ctx.method not in (GIT, EDITABLE_GIT) or not install_ctx.repo_root:
        return {"ok": False, "reason": "not a git checkout", "behind": 0}
    repo = Path(install_ctx.repo_root)
    _capture: CaptureFn = capture or _checked_capture
    try:
        branch = _capture(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo)
        if branch == "HEAD":
            return {"ok": False, "reason": "HEAD is detached (a pinned tag) — use --channel stable",
                    "behind": 0, "branch": branch}
        _capture(["git", "fetch", "--quiet"], repo)
        behind = int(_capture(["git", "rev-list", "--count", "HEAD..@{upstream}"], repo) or "0")
        head = _capture(["git", "rev-parse", "--short", "HEAD"], repo)
        upstream = _capture(["git", "rev-parse", "--short", "@{upstream}"], repo)
    except Exception as exc:  # noqa: BLE001 — a missing upstream is a plain answer
        return {"ok": False, "reason": f"git: {exc}", "behind": 0}
    return {"ok": True, "reason": None, "behind": behind, "head": head,
            "upstream": upstream, "branch": branch}


def build_runners(
    install_ctx: InstallContext,
    *,
    target_ref: Optional[str] = None,
    python: Optional[str] = None,
    run: Optional[RunFn] = None,
    capture: Optional[CaptureFn] = None,
    extras: Optional[List[str]] = None,
    packs: Optional[List[str]] = None,
) -> Optional[UpdateRunners]:
    """Build real runners for a self-updatable git/editable install, else ``None``.

    ``None`` means "no automated apply for this method" — the command falls back to the
    printed manual steps. ``run``/``capture``/``python`` are injectable for tests.

    ``target_ref`` is the release ref to move to (e.g. ``"v0.5.0"``). When set (the
    ``stable``/``pre`` channels), ``install()`` **checks out that tag** — the instance
    runs a *pinned tag* (detached HEAD — the release operating model),
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
    # 058: reinstall the SAME shape this install has — `.[<extras present>]`,
    # constrained by requirements.lock when the tree carries it. A bare
    # `pip install -e .` dropped every extra (a new dependency an extra gained
    # never arrived) and could move a pinned core package. `extras` is
    # injectable; None = detect from what is installed.
    # 066 P1 / D2: hash-checked against requirements.lock — the closure of the
    # carried extras, then the project --no-deps (see extras.install_commands).
    from cli.update.extras import install_commands, installed_extras, lock_path
    from cli.update import packs as pk
    import tempfile
    carried = list(extras) if extras is not None else installed_extras(repo)
    # 067 (one install): the packs this install carries — a retired separate
    # pack dist (its pack now ships inside polyrob) and, for a rollback target
    # with the old layout, that tree's own first-party packs. Read on the FIRST
    # pip install — AFTER the checkout moved, BEFORE anything is uninstalled —
    # and reused by the rollback. `packs` is injectable; None = detect.
    wanted: List[List[str]] = [list(packs)] if packs is not None else []
    retired: List[list] = []

    def _wanted_packs() -> List[str]:
        if not wanted:
            wanted.append(pk.carried_packs(repo))
        if not retired:
            retired.append(pk.retired_installed())
        return wanted[0]
    deps_file = Path(tempfile.gettempdir()) / f"polyrob-update-deps-{os.getpid()}.txt"

    def _pip_install() -> None:
        notes: List[str] = []
        ids = _wanted_packs()
        hashed = lock_path(repo) is not None
        if pk.tree_bundles_packs(repo):
            # The packs ship inside polyrob: carry each pack as its extra, then
            # retire the separate dists' METADATA (never a pack file) and verify
            # one provider per pack — all before the update's verify step.
            cmds = install_commands(py, repo, sorted(set(carried) | set(pk.bundled_extras(repo, ids))),
                                    editable=editable, deps_file=deps_file)
            cmds += pk.retire_commands(py, repo, retired[0])
        else:
            rows = pk.resolve_install(py, repo, ids, _capture, notes)
            cmds = install_commands(py, repo, carried, editable=editable, deps_file=deps_file,
                                    packs=[r[0] for r in rows] if hashed else None)
            cmds += pk.pack_commands(py, rows, editable=editable, hashed=hashed)
        for note in notes:
            print(f"polyrob update: {note}", file=sys.stderr)
        try:
            for cmd in cmds:
                _run(cmd, repo)
        finally:
            try:
                deps_file.unlink()
            except OSError:
                pass

    old_sha = None
    old_branch = None
    mutated = False
    pip_attempted = False

    def install() -> None:
        nonlocal old_sha, old_branch, mutated, pip_attempted
        # Run inside the update lock, immediately before mutating the checkout.
        # A refusal must not trigger a destructive rollback of somebody's work.
        # Only TRACKED modifications block: an untracked log or scratch file in a
        # server checkout must not make every update refuse forever.
        if _capture(["git", "status", "--porcelain", "--untracked-files=no"], repo):
            raise RuntimeError("checkout has local changes; commit or stash them before updating")
        old_sha = _capture(["git", "rev-parse", "HEAD"], repo)
        old_branch = _capture(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo)
        if target_ref:
            # Tag-pinned release update: fetch the new tags and move (detached) HEAD to
            # the released tag. Works whether HEAD was detached (prod) or on a branch.
            _run(["git", "check-ref-format", f"refs/tags/{target_ref}"], repo)
            _run(["git", "fetch", "--tags", "--quiet"], repo)
            mutated = True
            _run(["git", "checkout", "--quiet", "--detach", f"refs/tags/{target_ref}"], repo)
        else:
            # --channel git: track the current branch by fast-forward.
            if old_branch == "HEAD":
                raise RuntimeError("git channel requires a branch with an upstream; HEAD is detached")
            mutated = True
            _run(["git", "pull", "--ff-only"], repo)
        pip_attempted = True
        _pip_install()

    def migrate() -> None:
        _run([py, "-I", "-m", "migrations.migrate", "upgrade"], repo)

    def verify() -> None:
        # New code must at least import cleanly (the release smoke check).
        _run([py, "-m", "pip", "check"], repo)
        # 058: the import probe covers the surfaces whose dependency moved to an
        # extra — a bare `import core, cli.polyrob` passed happily on an install
        # that had lost one of them.
        _run([py, "-I", "-c", "import core, core.avatar, cli.polyrob, modules.llm.llm_factory, tools.filesystem, "
                              "modules.memory.task, modules.cards.cards, utils.gif_utils, "
                              "tools.document_parser, core.lazy_deps"], repo)
        # …and the runtime ASSETS must have landed with it. An import-only check
        # passes happily on an install that lost a package-data glob: the
        # invoice card loses its brand-mark header and silently degrades to
        # PIL's default font. Raising here is what makes engine.apply_update roll back —
        # `assets/` shipped in NO deploy script at all until
        # 2026-09-15, which is the class this closes.
        _run([py, "-I", "-c", _ASSET_PROBE], repo)

    def rollback_code() -> None:
        if not mutated:
            return
        # --keep aborts on conflicting work added during the update instead of
        # silently deleting it. Restore the original branch after a tag checkout.
        _run(["git", "reset", "--keep", old_sha], repo)
        if old_branch != "HEAD":
            _run(["git", "checkout", "--quiet", old_branch], repo)
        if pip_attempted:
            # No `pip check` here: a pre-existing, unrelated dependency conflict
            # must not make the ROLLBACK itself report as failed.
            _pip_install()

    return UpdateRunners(install=install, migrate=migrate,
                         verify=verify, rollback_code=rollback_code)
