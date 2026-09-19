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
        if cmd[:2] == ["git", "status"]:
            return ""  # clean checkout
        if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return "main"
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
    assert ["git", "check-ref-format", "refs/tags/v0.5.0"] in cmds
    assert ["git", "fetch", "--tags", "--quiet"] in cmds
    assert ["git", "checkout", "--quiet", "--detach", "refs/tags/v0.5.0"] in cmds
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
    assert calls == []  # nothing mutated yet → nothing to undo
    r.install()
    calls.clear()
    r.rollback_code()
    cmds = [c[0] for c in calls]
    assert ["git", "reset", "--keep", "OLDSHA123"] in cmds  # --keep never deletes new work
    assert ["git", "checkout", "--quiet", "main"] in cmds
    assert ["/py", "-m", "pip", "install", "."] in cmds  # non-editable pip
    assert ["/py", "-m", "pip", "check"] not in cmds  # a stale conflict must not fail the rollback


def test_install_refuses_tracked_changes_but_not_untracked_files(tmp_path):
    calls = []

    def run(cmd, cwd):
        calls.append(cmd)

    def capture(cmd, cwd):
        calls.append(cmd)
        if cmd[:2] == ["git", "status"]:
            assert "--untracked-files=no" in cmd  # an untracked log must not block forever
            return " M core/x.py"
        return "SHA"
    ctx = InstallContext(GIT, tmp_path, tmp_path, "git")
    r = build_runners(ctx, python="/py", run=run, capture=capture)
    import pytest
    with pytest.raises(RuntimeError, match="local changes"):
        r.install()
    assert not any(c[:2] == ["git", "pull"] for c in calls)
    r.rollback_code()
    assert not any(c[:2] == ["git", "reset"] for c in calls)  # a refusal never resets


def test_target_ref_is_validated_before_any_git_mutation(tmp_path):
    """A release tag_name off the network is a ref, never an option or a branch."""
    calls, run, capture = _rec()
    ctx = InstallContext(GIT, tmp_path, tmp_path, "git")
    r = build_runners(ctx, target_ref="--upload-pack=evil", python="/py", run=run, capture=capture)
    r.install()
    cmds = [c[0] for c in calls]
    assert cmds.index(["git", "check-ref-format", "refs/tags/--upload-pack=evil"]) < \
        cmds.index(["git", "fetch", "--tags", "--quiet"])
    assert ["git", "checkout", "--quiet", "--detach", "refs/tags/--upload-pack=evil"] in cmds


def test_verify_smoke_imports(tmp_path):
    calls, run, capture = _rec()
    ctx = InstallContext(GIT, tmp_path, tmp_path, "git")
    r = build_runners(ctx, python="/py", run=run, capture=capture)
    r.verify()
    assert (["/py", "-I", "-c", "import core, cli.polyrob"], str(tmp_path)) in calls


# ---------------------------------------------------------------------------
# The update must verify that runtime ASSETS landed, not only that code imports
#
# ⚠️ `avatar/` and `assets/` are read by path at runtime (mindprint.js, the
# DejaVu invoice-card fonts). A wheel that loses a package-data glob still
# imports perfectly — `import core, cli.polyrob` passes — and the install is
# quietly faceless, with invoice cards falling back to PIL's default font. The
# whole point of a verify step is to make that roll back instead.
# ---------------------------------------------------------------------------

def test_verify_also_checks_the_runtime_assets_resolve(tmp_path):
    calls, run, capture = _rec()
    ctx = InstallContext(GIT, tmp_path, tmp_path, "git")
    r = build_runners(ctx, python="/py", run=run, capture=capture)
    r.verify()
    probes = [c[0] for c in calls if c[0][:3] == ["/py", "-I", "-c"]]
    joined = "\n".join(p[3] for p in probes)
    assert "mindprint.js" in joined, (
        "verify does not check the avatar engine resolved after install")
    assert "DejaVuSans.ttf" in joined, (
        "verify does not check the invoice-card fonts resolved after install")


def test_the_asset_probe_RUNS_and_passes_against_this_tree():
    """⚠️ EXECUTE it, do not string-match it. The first two versions of this
    probe were wrong in ways a string match could never see: the wrong
    `parents[]` depth, then `raise X if c else None`, which raises None and
    blows up on the HEALTHY path — i.e. it would have rolled back every good
    update."""
    import subprocess, sys
    from cli.update.runners import _ASSET_PROBE
    r = subprocess.run([sys.executable, "-c", _ASSET_PROBE],
                       cwd=str(Path(__file__).resolve().parents[4]),
                       capture_output=True, text=True)
    assert r.returncode == 0, (r.stdout + r.stderr)


def test_the_asset_probe_FAILS_when_an_asset_is_absent(tmp_path):
    """The other half: it must actually catch a missing asset, or it is a
    no-op that makes every update look verified."""
    import subprocess, sys, shutil
    from cli.update.runners import _ASSET_PROBE
    repo = Path(__file__).resolve().parents[4]
    fake = tmp_path / "site"
    (fake / "modules" / "pfp").mkdir(parents=True)
    (fake / "modules" / "__init__.py").write_text("")
    (fake / "modules" / "pfp" / "__init__.py").write_text("")
    # avatar/ and assets/ deliberately NOT created — the lost-package-data case.
    r = subprocess.run([sys.executable, "-c", _ASSET_PROBE],
                       cwd=str(tmp_path), env={"PYTHONPATH": str(fake),
                                               "PATH": "/usr/bin:/bin"},
                       capture_output=True, text=True)
    assert r.returncode != 0
    assert "runtime assets missing" in (r.stdout + r.stderr)


def test_the_asset_probe_is_a_real_failure_not_a_warning():
    """It must raise through `_run`, because engine.apply_update's only signal
    to roll back is a verify that raises."""
    from cli.update import runners as mod
    src = (Path(mod.__file__)).read_text(encoding="utf-8")
    block = src[src.index("def verify("):src.index("def rollback_code(")]
    assert "_run(" in block
    assert "try:" not in block, (
        "a swallowed asset probe cannot trigger the auto-rollback")
