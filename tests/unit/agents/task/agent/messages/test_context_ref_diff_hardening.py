"""H01 (2026-09-23 harness security analysis): `@diff` must not be a host-exec rail.

`@diff:<arg>` went to host `git diff <arg>` with the full env: an argument read
as a git option, an unconfined path, no custody refusal, and the repo's own
`.git/config` could name a command git runs. Also pins the Low item: the
`@file`/`@folder` secret guard fails CLOSED when it raises.
"""
import shutil
import subprocess

import pytest

from agents.task.agent.messages import context_references as cr
from agents.task.agent.messages.context_references import preprocess_context_references

_HAS_GIT = shutil.which("git") is not None


@pytest.fixture(autouse=True)
def _no_custody(monkeypatch):
    for name in ("AGENT_WALLET_ENABLED", "AGENT_WALLET_MASTER_SEED",
                 "PAYMENT_MASTER_SEED", "MASTER_SEED"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def no_git(monkeypatch):
    calls = []

    def _boom(*a, **kw):
        calls.append(a)
        raise AssertionError("git must not run")
    monkeypatch.setattr(subprocess, "run", _boom)
    return calls


def test_option_shaped_argument_is_refused(tmp_path, no_git):
    out = preprocess_context_references("@diff:--output=x", root=str(tmp_path))
    assert "refused" in out and "git option" in out
    assert no_git == []


def test_path_outside_root_is_refused(tmp_path, no_git):
    root = tmp_path / "ws"
    root.mkdir()
    out = preprocess_context_references("@diff:../elsewhere", root=str(root))
    assert "path outside allowed root; refused" in out
    assert no_git == []


def test_wallet_custody_refuses_host_git(tmp_path, monkeypatch, no_git):
    monkeypatch.setenv("AGENT_WALLET_ENABLED", "true")
    out = preprocess_context_references("@diff", root=str(tmp_path))
    assert "host execution refused" in out
    assert no_git == []


def test_git_runs_hardened_with_a_scrubbed_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SOME_API_KEY", "not-for-git")
    calls = []

    class _Proc:
        returncode = 1   # `git config --get-regexp` found no filter key
        stdout = "diff body"

    def _fake_run(cmd, **kw):
        calls.append((cmd, kw))
        return _Proc()
    monkeypatch.setattr(subprocess, "run", _fake_run)

    out = preprocess_context_references("@diff:src/a.py", root=str(tmp_path))

    assert "diff body" in out
    diff_cmd, kw = calls[-1]
    env = kw["env"]
    assert "SOME_API_KEY" not in env
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"]
    for flag in ("core.fsmonitor=", "core.hooksPath=/dev/null", "diff.external=",
                 "--no-ext-diff", "--no-textconv"):
        assert flag in diff_cmd
    assert diff_cmd[-2:] == ["--", "src/a.py"]
    assert kw["cwd"] == str(tmp_path)


@pytest.mark.skipif(not _HAS_GIT, reason="git not installed")
def test_a_repo_that_defines_a_filter_driver_is_refused(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "filter.x.clean", "cat"],
                   check=True)
    out = preprocess_context_references("@diff", root=str(tmp_path))
    assert "defines a filter driver" in out


@pytest.mark.skipif(not _HAS_GIT, reason="git not installed")
def test_a_plain_repo_still_diffs(tmp_path):
    git = ["git", "-C", str(tmp_path), "-c", "user.email=a@b", "-c", "user.name=a"]
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "f.txt").write_text("a\n")
    subprocess.run(git + ["add", "f.txt"], check=True)
    subprocess.run(git + ["commit", "-qm", "x"], check=True)
    (tmp_path / "f.txt").write_text("a\nb\n")

    out = preprocess_context_references("@diff:f.txt", root=str(tmp_path))
    assert "+b" in out


def test_file_secret_guard_error_fails_closed(tmp_path, monkeypatch):
    (tmp_path / "doc.txt").write_text("private body")

    def _raise(*a, **kw):
        raise RuntimeError("guard broken")
    monkeypatch.setattr("core.security.secret_guard.is_secret_path", _raise)

    out = preprocess_context_references("@file:doc.txt", root=str(tmp_path))
    assert "private body" not in out
    assert "secret guard unavailable; refused" in out


def test_folder_secret_guard_error_redacts_entries(tmp_path, monkeypatch):
    (tmp_path / "creds.txt").write_text("x")

    def _raise(*a, **kw):
        raise RuntimeError("guard broken")
    monkeypatch.setattr("core.security.secret_guard.is_secret_path", _raise)

    listing = cr._load_folder(str(tmp_path), root=str(tmp_path))
    assert listing == "creds.txt <redacted: secret guard unavailable>"
