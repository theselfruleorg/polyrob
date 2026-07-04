"""Task 20: git URL + `owner/repo[/subdir]` resolver (sandboxed clone) tests.

Covers: clone into a sandboxed temp dir → `_audit_tree` (symlink/traversal/byte/file
caps) → resolved commit SHA recorded → handed to Task 19's `install_local` with
`source="git:<spec>"` so a git install NEVER auto-approves, even with `--trust local`.

All fixtures are local bare git repos (`file://`) — no network access required.
"""
import subprocess

import pytest

from cli.commands.skill_install import _reject_symlink_blobs, _resolve_git_spec, install_git


@pytest.fixture(autouse=True)
def _local_mode(monkeypatch):
    """Task 23 gates every install route on ``local_mode_enabled()`` — pin it ON
    for this pipeline suite (see test_skill_install_local.py for rationale)."""
    from agents.task import constants

    monkeypatch.setattr(constants, "local_mode_enabled", lambda: True)


def _bare_repo_with_skill(tmp_path):
    work = tmp_path / "work"
    skill = work / "myskill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: myskill\ndescription: A git skill. Use when git.\n---\n# b\nx"
    )
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(work), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "init"],
        check=True,
    )
    bare = tmp_path / "repo.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True)
    return bare


def test_install_git_subdir_quarantines(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    bare = _bare_repo_with_skill(tmp_path)
    res = install_git(f"file://{bare}/myskill", user_id="7", trust="prompt")
    assert res.name == "myskill" and res.approved is False  # remote never auto-approved
    assert res.resolved_sha and len(res.resolved_sha) >= 7  # SHA recorded
    assert ".pending" in str(res.staged_path)


def test_install_git_rejects_symlink_escape(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    work = tmp_path / "work"
    skill = work / "evil"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: evil\ndescription: d\n---\n# b")
    (skill / "leak").symlink_to("/etc/passwd")
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(work), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "i"],
        check=True,
    )
    bare = tmp_path / "e.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True)
    with pytest.raises(Exception) as ei:
        install_git(f"file://{bare}/evil", user_id="7", trust="prompt")
    assert "symlink" in str(ei.value).lower() or "audit" in str(ei.value).lower()


def test_install_git_trust_local_never_auto_approves(tmp_path, monkeypatch):
    """A remote (git) install must never auto-approve, even with --trust local —
    the auto-approve gate in install_local only fires for source=="local"."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    bare = _bare_repo_with_skill(tmp_path)
    res = install_git(f"file://{bare}/myskill", user_id="7", trust="local")
    assert res.approved is False


# --- Finding 1: fail-closed on a broken `git ls-tree` audit ----------------

def test_reject_symlink_blobs_fails_closed_on_ls_tree_error(tmp_path, monkeypatch):
    """``ls-tree`` is the ONLY effective symlink detector once
    ``core.symlinks=false`` is set (it makes git materialize a committed
    symlink as an inert plain-text file, defeating the filesystem
    ``is_symlink()`` fallback). If the ``ls-tree`` subprocess itself fails or
    errors, the audit must REFUSE the install, not silently no-op."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "f.txt").write_text("x")
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(work), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "i"],
        check=True,
    )

    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if "ls-tree" in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(Exception) as ei:
        _reject_symlink_blobs(work)
    assert "audit" in str(ei.value).lower() or "ls-tree" in str(ei.value).lower()


def test_install_git_fails_closed_when_ls_tree_errors(tmp_path, monkeypatch):
    """End-to-end: a broken `ls-tree` audit must abort `install_git`, not
    silently continue staging the clone."""
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    bare = _bare_repo_with_skill(tmp_path)

    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if "ls-tree" in cmd:
            return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="fatal: boom")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(Exception) as ei:
        install_git(f"file://{bare}/myskill", user_id="7", trust="prompt")
    assert "audit" in str(ei.value).lower() or "ls-tree" in str(ei.value).lower()


# --- Finding 2: git-ref parsing must not mangle a real SSH URL -------------

def test_resolve_git_spec_ssh_shorthand_not_mangled():
    """`git@host:owner/repo.git` legitimately contains `@` as URL syntax, not a
    ref separator — it must pass through completely unchanged."""
    url, subdir, ref = _resolve_git_spec("git@github.com:o/r.git")
    assert url == "git@github.com:o/r.git"
    assert subdir == ""
    assert ref is None


def test_resolve_git_spec_https_url_with_at_sign_not_mangled():
    url, subdir, ref = _resolve_git_spec("https://user@github.com/o/r.git")
    assert url == "https://user@github.com/o/r.git"
    assert ref is None


def test_resolve_git_spec_shorthand_with_subdir_and_ref():
    """`owner/repo/subdir@ref` — the trailing `@ref` is split off BEFORE
    owner/repo/subdir splitting, so it lands on `ref`, not baked into the repo
    URL or the subdir."""
    url, subdir, ref = _resolve_git_spec("anthropics/skills/pdf@v1.2")
    assert url == "https://github.com/anthropics/skills.git"
    assert subdir == "pdf"
    assert ref == "v1.2"


def test_resolve_git_spec_shorthand_subdir_no_ref():
    """Existing `owner/repo/subdir` (no ref) behavior must still resolve."""
    url, subdir, ref = _resolve_git_spec("anthropics/skills/pdf")
    assert url == "https://github.com/anthropics/skills.git"
    assert subdir == "pdf"
    assert ref is None


def test_resolve_git_spec_shorthand_ref_no_subdir():
    url, subdir, ref = _resolve_git_spec("anthropics/skills@main")
    assert url == "https://github.com/anthropics/skills.git"
    assert subdir == ""
    assert ref == "main"


# --- Finding 3: absolute/double-slash subdir must be refused ---------------

def test_resolve_git_spec_double_slash_produces_absolute_subdir():
    """A double-slash shorthand spec (`owner/repo//etc/passwd`) resolves to an
    absolute-looking subdir string — the join-site guard below must refuse it."""
    url, subdir, ref = _resolve_git_spec("owner/repo//etc/passwd")
    assert subdir == "/etc/passwd"


def test_install_git_rejects_absolute_subdir(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    bare = _bare_repo_with_skill(tmp_path)
    with pytest.raises(Exception) as ei:
        install_git(f"file://{bare}//etc/passwd", user_id="7", trust="prompt")
    assert "subdir" in str(ei.value).lower()


# --- Finding 4: rev-parse failure must not silently yield an empty sha -----

def test_install_git_raises_when_rev_parse_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYROB_DATA_DIR", str(tmp_path / "home"))
    bare = _bare_repo_with_skill(tmp_path)

    real_run = subprocess.run

    def fake_run(cmd, *args, **kwargs):
        if "rev-parse" in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="fatal: boom")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(Exception) as ei:
        install_git(f"file://{bare}/myskill", user_id="7", trust="prompt")
    assert "rev-parse" in str(ei.value).lower()
