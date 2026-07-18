"""Idempotent `.polyrob/` gitignore housekeeping for the CLI.

`build_cli_container` creates `<cwd>/.polyrob` on every `polyrob run`/`polyrob chat`,
but the `.gitignore` entry used to be written only by `polyrob init`. This helper
closes that gap so a bare CLI run inside a git repo doesn't leave `.polyrob/` showing
in `git status`. Single source of truth: `polyrob init` calls this too.
"""
from __future__ import annotations

from pathlib import Path


def ensure_polyrob_gitignored(project_root: Path, *, require_git_repo: bool = True) -> None:
    """Idempotently append ``.polyrob/`` to ``<project_root>/.gitignore``. Fail-open.

    With ``require_git_repo=True`` (default — the implicit ``polyrob run``/``polyrob
    chat`` path) only writes when ``project_root`` is inside a git work tree (a
    ``.git`` marker at or above it), so a bare CLI run in a non-repo directory
    (``~/Documents``, ``/tmp``) doesn't create a spurious ``.gitignore``.
    ``polyrob init`` is an explicit opt-in and passes ``require_git_repo=False`` so it
    sets up ``.gitignore`` even before ``git init``. Never raises: gitignore
    housekeeping must not block a session.
    """
    try:
        p = Path(project_root).resolve()
        if require_git_repo and not any((d / ".git").exists() for d in (p, *p.parents)):
            return
        gi = Path(project_root) / ".gitignore"
        existing = gi.read_text() if gi.exists() else ""
        # Line-exact check so a similarly-named entry (e.g. `my.polyrob/`) doesn't
        # cause a false "already ignored".
        if any(ln.strip() == ".polyrob/" for ln in existing.splitlines()):
            return
        with gi.open("a") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write(".polyrob/\n")
    except Exception:
        pass


# Back-compat alias (pre-rename callers/tests). Prefer ensure_polyrob_gitignored.
ensure_rob_gitignored = ensure_polyrob_gitignored
