"""T6 — `.polyrob/` is gitignored on `run`, not only `init`.

`build_cli_container` calls `ensure_rob_gitignored` on every `polyrob run`/`rob chat`
(gated POLYROB_GITIGNORE_DOTROB, default on), so a bare CLI run inside a git repo
doesn't leave `.polyrob/` showing in `git status` — no separate `init` required.

(Doc 02 renames the literal `.polyrob` → `.polyrob` later; this test pins the CURRENT
`.polyrob` behavior.)
"""
import subprocess

from cli.gitignore import ensure_rob_gitignored


def test_gitignore_added_on_run_not_only_init(tmp_path):
    # A real git work tree, created without running any `init` of ours.
    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert not (tmp_path / ".gitignore").exists()

    # The implicit `polyrob run` path (require_git_repo default True).
    ensure_rob_gitignored(tmp_path)

    gi = tmp_path / ".gitignore"
    assert gi.exists()
    lines = [ln.strip() for ln in gi.read_text().splitlines()]
    assert ".polyrob/" in lines
