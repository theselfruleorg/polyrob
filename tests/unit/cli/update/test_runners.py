"""`cli/update/runners.py` — real step builders (git/editable), injected shell."""
from pathlib import Path

from cli.update.detect import EDITABLE_GIT, GIT, PIP, InstallContext
from cli.update.runners import build_runners


def _rec():
    calls = []

    def run(cmd, cwd):
        calls.append((cmd, str(cwd) if cwd else None))

    def capture(cmd, cwd):
        calls.append((cmd, str(cwd) if cwd else None))
        return "OLDSHA123"
    return calls, run, capture


def test_unsupported_method_returns_none():
    ctx = InstallContext(PIP, Path("/x"), None, "wheel")
    assert build_runners(ctx) is None


def test_git_without_repo_root_returns_none():
    ctx = InstallContext(GIT, Path("/x"), None, "no repo")
    assert build_runners(ctx) is None


def test_editable_install_uses_editable_pip_and_pulls(tmp_path):
    calls, run, capture = _rec()
    ctx = InstallContext(EDITABLE_GIT, tmp_path, tmp_path, "editable")
    r = build_runners(ctx, python="/py", run=run, capture=capture)
    assert r is not None
    r.install()
    cmds = [c[0] for c in calls]
    assert ["git", "pull", "--ff-only"] in cmds
    assert ["/py", "-m", "pip", "install", "-e", "."] in cmds


def test_target_ref_checks_out_tag_not_pull(tmp_path):
    """A tag-pinned release update (stable/pre channel) must fetch tags and CHECK OUT the
    target tag — `git pull --ff-only` fails on the detached-HEAD pinned-tag prod posture."""
    calls, run, capture = _rec()
    ctx = InstallContext(GIT, tmp_path, tmp_path, "git")
    r = build_runners(ctx, target_ref="v0.5.0", python="/py", run=run, capture=capture)
    r.install()
    cmds = [c[0] for c in calls]
    assert ["git", "fetch", "--tags", "--force", "--quiet"] in cmds
    assert ["git", "checkout", "--quiet", "v0.5.0"] in cmds
    assert ["git", "pull", "--ff-only"] not in cmds  # NEVER on a tag-pinned install
    assert ["/py", "-m", "pip", "install", "."] in cmds


def test_no_target_ref_keeps_branch_fast_forward(tmp_path):
    """--channel git (target_ref=None) keeps the branch-tracking fast-forward."""
    calls, run, capture = _rec()
    ctx = InstallContext(GIT, tmp_path, tmp_path, "git")
    r = build_runners(ctx, python="/py", run=run, capture=capture)
    r.install()
    cmds = [c[0] for c in calls]
    assert ["git", "pull", "--ff-only"] in cmds
    assert not any(c[:2] == ["git", "checkout"] for c in cmds)


def test_rollback_resets_to_captured_sha(tmp_path):
    calls, run, capture = _rec()
    ctx = InstallContext(GIT, tmp_path, tmp_path, "git")
    r = build_runners(ctx, python="/py", run=run, capture=capture)
    r.rollback_code()
    cmds = [c[0] for c in calls]
    assert ["git", "reset", "--hard", "OLDSHA123"] in cmds
    assert ["/py", "-m", "pip", "install", "."] in cmds  # non-editable pip


def test_verify_smoke_imports(tmp_path):
    calls, run, capture = _rec()
    ctx = InstallContext(GIT, tmp_path, tmp_path, "git")
    r = build_runners(ctx, python="/py", run=run, capture=capture)
    r.verify()
    assert (["/py", "-c", "import core, cli.polyrob"], str(tmp_path)) in calls
