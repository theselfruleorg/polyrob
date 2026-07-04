"""P0 Task 7 — GitTool over a confined temp repo (real git)."""
import logging
import subprocess

import pytest

from tools.git import register_git_tool, git_enabled
from tools.git.tool import (
    GitTool, GitStatusParams, GitAddParams, GitCommitParams, GitCloneParams, GitLogParams,
    GitCheckoutParams, GitBranchParams, GitPullParams, GitPushParams,
)


def _git(root):
    t = object.__new__(GitTool)
    t.logger = logging.getLogger("git-test")
    t._root_override = str(root)
    t._timeout = 60.0
    return t


def _init_repo(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)


# --- registration ------------------------------------------------------------

def test_flag_off_not_registered(monkeypatch):
    monkeypatch.setenv("GIT_TOOLS_ENABLED", "false")
    from tools.descriptors import TOOL_DESCRIPTORS, get_tool_class
    TOOL_DESCRIPTORS.pop("git", None)
    assert register_git_tool() is False
    assert get_tool_class("git") is None


def test_flag_on_registers(monkeypatch):
    monkeypatch.setenv("GIT_TOOLS_ENABLED", "true")
    from tools.descriptors import TOOL_DESCRIPTORS, TOOL_COMPONENTS, get_tool_class
    try:
        assert register_git_tool() is True
        assert get_tool_class("git") is GitTool
    finally:
        TOOL_DESCRIPTORS.pop("git", None)
        TOOL_COMPONENTS[:] = [(n, c) for (n, c) in TOOL_COMPONENTS if n != "git"]


def test_safe_local_default_on(monkeypatch):
    monkeypatch.delenv("GIT_TOOLS_ENABLED", raising=False)
    monkeypatch.setenv("POLYROB_LOCAL", "true")
    assert git_enabled() is True


def test_no_future_annotations():
    import __future__
    import tools.git.tool as m
    assert getattr(m, "annotations", None) is not __future__.annotations


# --- behavior ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_status_add_commit_log_flow(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("hi\n")
    t = _git(tmp_path)
    st = await t.git_status(GitStatusParams())
    assert "a.txt" in st.extracted_content
    assert (await t.git_add(GitAddParams(paths=["a.txt"]))).error is None
    assert (await t.git_commit(GitCommitParams(message="init"))).error is None
    log = await t.git_log(GitLogParams(max_count=5))
    assert "init" in log.extracted_content


@pytest.mark.asyncio
async def test_add_out_of_root_refused(tmp_path):
    _init_repo(tmp_path)
    res = await _git(tmp_path).git_add(GitAddParams(paths=["../evil"]))
    assert res.error and "escape" in res.error.lower()


@pytest.mark.asyncio
async def test_clone_out_of_root_refused(tmp_path):
    res = await _git(tmp_path).git_clone(GitCloneParams(url="https://example.com/x.git", dest="../outside"))
    assert res.error and "escape" in res.error.lower()


@pytest.mark.asyncio
async def test_clone_confined(tmp_path):
    src = tmp_path / "src"
    _init_repo(src)
    (src / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(src), "add", "f.txt"], check=True)
    subprocess.run(["git", "-C", str(src), "commit", "-qm", "c"], check=True)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    res = await _git(workspace).git_clone(GitCloneParams(url=str(src), dest="cloned"))
    assert res.error is None
    assert (workspace / "cloned" / "f.txt").read_text() == "x"


# --- P0-7 CRITICAL: ext:: / fd:: transport RCE + flag-injection regressions ---
#
# git natively supports the `ext::<command>` transport (via git-remote-ext): as soon as
# the transport opens, git forks/execs <command> on the HOST. `fd::` is a related
# transport that hands the process an already-open fd. Neither requires a real git
# server to speak the pack protocol for the command to run — the exec happens before
# any protocol negotiation. This is RCE independent of argv-list hygiene, because the
# execution happens inside git's own transport layer, not via shell=True in our code.

def test_unsafe_remote_blocks_transport_helpers_and_flags():
    """Unit-level check of the validator itself: transport helpers + flag injection."""
    assert GitTool._unsafe_remote("ext::sh -c id") is True
    assert GitTool._unsafe_remote("fd::0") is True
    assert GitTool._unsafe_remote("ext::true") is True
    assert GitTool._unsafe_remote("-x") is True
    assert GitTool._unsafe_remote("--upload-pack=touch pwned") is True


def test_unsafe_remote_allows_normal_values():
    """The validator must not false-positive on everyday remotes/urls."""
    assert GitTool._unsafe_remote("origin") is False
    assert GitTool._unsafe_remote("upstream") is False
    assert GitTool._unsafe_remote("https://github.com/foo/bar.git") is False
    assert GitTool._unsafe_remote("git@github.com:foo/bar.git") is False
    assert GitTool._unsafe_remote("ssh://git@github.com/foo/bar.git") is False


@pytest.mark.asyncio
async def test_clone_ext_transport_refused_no_side_effect(tmp_path):
    """git_clone(url='ext::...') must be refused BEFORE git ever runs — no command exec."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    marker = tmp_path / "pwned.txt"
    res = await _git(workspace).git_clone(
        GitCloneParams(url=f"ext::sh -c 'echo pwned > {marker}'", dest="cloned")
    )
    assert res.error is not None
    assert "refused" in res.error.lower()
    # The critical assertion: the shell command embedded in the `ext::` transport must
    # NEVER have executed. Pre-fix, this file would exist containing "pwned".
    assert not marker.exists()
    assert not (workspace / "cloned").exists()


@pytest.mark.asyncio
async def test_clone_ext_transport_refused_before_subprocess_spawn(tmp_path, monkeypatch):
    """The validator must reject BEFORE git is even spawned. (Empirically, this machine's
    git 2.47.1 already refuses `ext::` on its own with zero ambient config — so this test,
    not the marker-file one, is the host-git-version-independent proof that OUR validator —
    not some possibly-absent host git default — is what blocks the payload.)"""
    workspace = tmp_path / "ws"
    workspace.mkdir()

    def boom(*a, **kw):
        raise AssertionError("git must never be spawned for a rejected url")

    monkeypatch.setattr(subprocess, "run", boom)
    res = await _git(workspace).git_clone(GitCloneParams(url="ext::sh -c 'echo pwned'", dest="cloned"))
    assert res.error is not None
    assert "refused" in res.error.lower()


@pytest.mark.asyncio
async def test_clone_fd_transport_refused(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    res = await _git(workspace).git_clone(GitCloneParams(url="fd::0", dest="cloned"))
    assert res.error is not None
    assert "refused" in res.error.lower()
    assert not (workspace / "cloned").exists()


@pytest.mark.asyncio
async def test_pull_ext_transport_refused(tmp_path):
    _init_repo(tmp_path)
    res = await _git(tmp_path).git_pull(GitPullParams(remote="ext::sh -c 'echo pwned'"))
    assert res.error is not None
    assert "refused" in res.error.lower()


@pytest.mark.asyncio
async def test_push_ext_transport_refused(tmp_path):
    _init_repo(tmp_path)
    res = await _git(tmp_path).git_push(GitPushParams(remote="ext::sh -c 'echo pwned'"))
    assert res.error is not None
    assert "refused" in res.error.lower()


@pytest.mark.asyncio
async def test_checkout_flag_injection_refused(tmp_path):
    """ref='--force' must be refused, not silently passed through as a git flag."""
    _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("hi\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "init"], check=True)
    res = await _git(tmp_path).git_checkout(GitCheckoutParams(ref="--force"))
    assert res.error is not None
    assert "refused" in res.error.lower()


@pytest.mark.asyncio
async def test_branch_flag_injection_refused(tmp_path):
    _init_repo(tmp_path)
    res = await _git(tmp_path).git_branch(GitBranchParams(name="--force"))
    assert res.error is not None
    assert "refused" in res.error.lower()


@pytest.mark.asyncio
async def test_checkout_normal_ref_still_works(tmp_path):
    """Regression guard: legit branch/ref names (even with dashes mid-string) still work."""
    _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("hi\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "init"], check=True)
    t = _git(tmp_path)
    assert (await t.git_checkout(GitCheckoutParams(ref="feature-branch", create=True))).error is None


@pytest.mark.asyncio
async def test_git_allow_protocol_set_on_every_invocation(tmp_path, monkeypatch):
    """_run_git must set GIT_ALLOW_PROTOCOL so git itself refuses ext/fd transports."""
    _init_repo(tmp_path)
    captured = {}
    real_run = subprocess.run

    def spy_run(argv, **kwargs):
        captured["env"] = kwargs.get("env")
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy_run)
    res = await _git(tmp_path).git_status(GitStatusParams())
    assert res.error is None
    assert captured["env"] is not None
    assert captured["env"].get("GIT_ALLOW_PROTOCOL") == "file:git:http:https:ssh"
