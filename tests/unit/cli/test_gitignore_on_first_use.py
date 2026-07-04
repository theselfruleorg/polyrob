"""Phase 2 (path-concerns upgrade): auto-gitignore .polyrob/ on first CLI use.

`.polyrob/` was only added to .gitignore by `rob init`; bare `polyrob run`/`rob chat`
created `.polyrob/` but never ignored it. The helper must be idempotent, only write
inside an actual git work tree, and never raise.
"""
from pathlib import Path

from cli.gitignore import ensure_rob_gitignored


def _mkrepo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    return tmp_path


def test_appends_to_existing_gitignore(tmp_path):
    repo = _mkrepo(tmp_path)
    (repo / ".gitignore").write_text("node_modules/\n")
    ensure_rob_gitignored(repo)
    assert ".polyrob/" in (repo / ".gitignore").read_text()


def test_idempotent(tmp_path):
    repo = _mkrepo(tmp_path)
    ensure_rob_gitignored(repo)
    ensure_rob_gitignored(repo)
    assert (repo / ".gitignore").read_text().count(".polyrob/") == 1


def test_creates_gitignore_in_repo_without_one(tmp_path):
    repo = _mkrepo(tmp_path)
    ensure_rob_gitignored(repo)
    assert (repo / ".gitignore").exists()
    assert ".polyrob/" in (repo / ".gitignore").read_text()


def test_no_gitignore_outside_git_repo(tmp_path):
    # No .git marker anywhere -> must not create a spurious .gitignore.
    ensure_rob_gitignored(tmp_path)
    assert not (tmp_path / ".gitignore").exists()


def test_appends_newline_before_entry_when_missing(tmp_path):
    repo = _mkrepo(tmp_path)
    (repo / ".gitignore").write_text("foo")  # no trailing newline
    ensure_rob_gitignored(repo)
    text = (repo / ".gitignore").read_text()
    assert "foo\n.polyrob/\n" == text


def test_worktree_git_file_counts_as_repo(tmp_path):
    # git worktrees/submodules use a `.git` FILE, not a dir.
    (tmp_path / ".git").write_text("gitdir: /somewhere/.git/worktrees/x\n")
    ensure_rob_gitignored(tmp_path)
    assert ".polyrob/" in (tmp_path / ".gitignore").read_text()


def test_detects_git_in_parent(tmp_path):
    _mkrepo(tmp_path)
    sub = tmp_path / "pkg"
    sub.mkdir()
    ensure_rob_gitignored(sub)
    assert (sub / ".gitignore").exists()


def test_no_false_match_on_similar_entry(tmp_path):
    repo = _mkrepo(tmp_path)
    (repo / ".gitignore").write_text("my.polyrob/\n")  # contains substring ".polyrob/"
    ensure_rob_gitignored(repo)
    # Must still add the exact `.polyrob/` entry (line-exact check).
    lines = [ln.strip() for ln in (repo / ".gitignore").read_text().splitlines()]
    assert ".polyrob/" in lines


def test_require_git_repo_false_writes_without_git(tmp_path):
    # `rob init` opt-in path: write even without a .git marker.
    ensure_rob_gitignored(tmp_path, require_git_repo=False)
    assert ".polyrob/" in (tmp_path / ".gitignore").read_text()


def test_fail_open_on_unwritable(tmp_path, monkeypatch):
    repo = _mkrepo(tmp_path)

    def boom(*a, **k):
        raise OSError("read-only fs")

    monkeypatch.setattr(Path, "open", boom)
    # Must not raise.
    ensure_rob_gitignored(repo)
