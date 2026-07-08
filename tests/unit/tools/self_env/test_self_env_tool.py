"""WS-5: the `self_env` self-maintenance tool (posture 2).

Distinct approvable verbs (never raw bash): install_dep / read_source /
patch_source / restart_service / git_pull. Every verb is gated
compute_posture_allows(ctx, 2) AND (via the Controller's posture-2 wiring) approval.
patch_source is realpath-confined to the install tree and HARD-DENIES env/config
files; git_pull is ff-only with ext:: rejected; restart_service refuses when the
process is not supervised. Every call emits a self_modification audit event.
"""
import logging
from pathlib import Path

import pytest

import agents.task.constants as c
from tools.self_env.tool import (
    SelfEnvTool, InstallDepParams, ReadSourceParams, PatchSourceParams,
    RestartParams, GitPullParams,
)
from tools.controller.execution_context import ActionExecutionContext


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("AGENT_COMPUTE_POSTURE", "POLYROB_LOCAL", "POLYROB_OWNER_USER_ID",
              "POLYROB_INSTALL_TREE", "POLYROB_SUPERVISED"):
        monkeypatch.delenv(k, raising=False)
    c._refreeze_compute_posture_for_tests()
    yield
    c._refreeze_compute_posture_for_tests()


def _tool(install_root, events=None):
    t = object.__new__(SelfEnvTool)
    t.logger = logging.getLogger("self-env-test")
    t._install_root_override = str(install_root)
    if events is not None:
        t._emit = lambda **kw: events.append(kw)
    return t


def _owner_ctx(**kw):
    d = dict(role="orchestrator", is_sub_agent=False, user_id="rob",
             session_id="s1", metadata={"turn_kind": None})
    d.update(kw)
    return ActionExecutionContext(**d)


def _posture(monkeypatch, v):
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", v)
    c._refreeze_compute_posture_for_tests()


# --- gating ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_denied_below_posture_2(monkeypatch, tmp_path):
    _posture(monkeypatch, "1")  # posture 1 is NOT enough for self_env
    t = _tool(tmp_path)
    res = await t.self_env_read_source(ReadSourceParams(path="x.py"),
                                       execution_context=_owner_ctx())
    assert res.error and "posture" in res.error.lower()


@pytest.mark.asyncio
async def test_denied_for_leaf_at_posture_2(monkeypatch, tmp_path):
    _posture(monkeypatch, "2")
    t = _tool(tmp_path)
    res = await t.self_env_read_source(ReadSourceParams(path="x.py"),
                                       execution_context=_owner_ctx(role="leaf"))
    assert res.error


# --- read/patch confinement ------------------------------------------------------

@pytest.mark.asyncio
async def test_read_source_within_tree(monkeypatch, tmp_path):
    _posture(monkeypatch, "2")
    (tmp_path / "mod.py").write_text("print('hi')\n")
    t = _tool(tmp_path)
    res = await t.self_env_read_source(ReadSourceParams(path="mod.py"),
                                       execution_context=_owner_ctx())
    assert not res.error and "print('hi')" in res.extracted_content


@pytest.mark.asyncio
async def test_patch_source_edits_within_tree(monkeypatch, tmp_path):
    _posture(monkeypatch, "2")
    (tmp_path / "mod.py").write_text("VALUE = 1\n")
    t = _tool(tmp_path)
    res = await t.self_env_patch_source(
        PatchSourceParams(path="mod.py", old_string="VALUE = 1", new_string="VALUE = 2"),
        execution_context=_owner_ctx())
    assert not res.error
    assert (tmp_path / "mod.py").read_text() == "VALUE = 2\n"


@pytest.mark.asyncio
async def test_patch_source_rejects_path_outside_tree(monkeypatch, tmp_path):
    _posture(monkeypatch, "2")
    t = _tool(tmp_path)
    res = await t.self_env_patch_source(
        PatchSourceParams(path="../../../etc/hosts", old_string="a", new_string="b"),
        execution_context=_owner_ctx())
    assert res.error and ("outside" in res.error.lower() or "escape" in res.error.lower()
                          or "confine" in res.error.lower())


@pytest.mark.asyncio
async def test_patch_source_hard_denies_env_file(monkeypatch, tmp_path):
    _posture(monkeypatch, "2")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / ".env.production").write_text("SECRET=1\n")
    t = _tool(tmp_path)
    res = await t.self_env_patch_source(
        PatchSourceParams(path="config/.env.production", old_string="SECRET=1", new_string="SECRET=2"),
        execution_context=_owner_ctx())
    assert res.error and ("credential" in res.error.lower() or "config" in res.error.lower()
                          or "secret" in res.error.lower())
    # the file is untouched
    assert (tmp_path / "config" / ".env.production").read_text() == "SECRET=1\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("rel", [
    "data/database/bot.db",   # main app DB — may hold tokens/PII
    "data/wallet.sqlite",     # wallet material
    "data/database/bot.db-wal",
    ".git/config",            # git config → RCE at git_pull time
])
async def test_confine_denies_in_tree_secrets_and_git(monkeypatch, tmp_path, rel):
    """self_env must not read/patch in-tree secrets (bot.db/*.sqlite) or the .git dir
    (a patched .git/config runs code at git_pull time) even though they live under the
    install tree — the narrow credential list missed them."""
    _posture(monkeypatch, "2")
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("secret")
    t = _tool(tmp_path)
    r_read = await t.self_env_read_source(ReadSourceParams(path=rel),
                                          execution_context=_owner_ctx())
    assert r_read.error, f"read_source should deny {rel}"
    r_patch = await t.self_env_patch_source(
        PatchSourceParams(path=rel, old_string="secret", new_string="x"),
        execution_context=_owner_ctx())
    assert r_patch.error, f"patch_source should deny {rel}"


@pytest.mark.asyncio
async def test_read_source_emits_audit_on_confine_denied(monkeypatch, tmp_path):
    _posture(monkeypatch, "2")
    events = []
    t = _tool(tmp_path, events=events)
    await t.self_env_read_source(ReadSourceParams(path="../../etc/passwd"),
                                 execution_context=_owner_ctx())
    assert any(e.get("action") == "read_source" and e.get("ok") is False for e in events)


@pytest.mark.asyncio
async def test_patch_source_hard_denies_polyrob_env(monkeypatch, tmp_path):
    _posture(monkeypatch, "2")
    (tmp_path / "polyrob.env").write_text("AGENT_COMPUTE_POSTURE=3\n")
    t = _tool(tmp_path)
    res = await t.self_env_patch_source(
        PatchSourceParams(path="polyrob.env", old_string="3", new_string="0"),
        execution_context=_owner_ctx())
    assert res.error
    assert (tmp_path / "polyrob.env").read_text() == "AGENT_COMPUTE_POSTURE=3\n"


# --- install_dep validation ------------------------------------------------------

@pytest.mark.asyncio
async def test_install_dep_rejects_shell_metachars(monkeypatch, tmp_path):
    _posture(monkeypatch, "2")
    t = _tool(tmp_path)
    res = await t.self_env_install_dep(InstallDepParams(package="flask; rm -rf /"),
                                       execution_context=_owner_ctx())
    assert res.error and "package" in res.error.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("pkg", [
    "-rfoo.txt",        # pip requirements-file injection (-r foo.txt)
    "-e.",              # editable install
    "-eevil",           # editable, attached value
    "--index-url",      # index override flag
    "--extra-index-url",
    "--pre",            # allow pre-releases
    "--editable",
])
async def test_install_dep_rejects_pip_flag_injection(monkeypatch, tmp_path, pkg):
    """A package spec starting with '-' is a pip FLAG, not a package — it must be
    refused (else install_dep('-rfoo.txt') installs an arbitrary requirements file,
    bypassing the single-pinned-package intent). Mirrors the git-tool leading-'-' guard."""
    _posture(monkeypatch, "2")
    called = []
    t = _tool(tmp_path)

    async def _fake_run(argv):
        called.append(argv)
        return 0, "", ""
    t._run_subprocess = _fake_run

    res = await t.self_env_install_dep(InstallDepParams(package=pkg),
                                       execution_context=_owner_ctx())
    assert res.error and "package" in res.error.lower(), f"{pkg!r} should be rejected"
    assert called == [], f"pip must NOT run for {pkg!r}"


@pytest.mark.asyncio
async def test_install_dep_runs_pip_with_sys_executable(monkeypatch, tmp_path):
    _posture(monkeypatch, "2")
    calls = []

    async def _fake_run(argv):
        calls.append(argv)
        return 0, "Successfully installed flask-3.0.0", ""

    t = _tool(tmp_path)
    t._run_subprocess = _fake_run
    res = await t.self_env_install_dep(InstallDepParams(package="flask==3.0.0"),
                                       execution_context=_owner_ctx())
    assert not res.error
    import sys
    assert calls and calls[0][0] == sys.executable
    assert "pip" in calls[0] and "install" in calls[0] and "flask==3.0.0" in calls[0]


# --- git_pull --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_git_pull_is_ff_only(monkeypatch, tmp_path):
    _posture(monkeypatch, "2")
    calls = []

    async def _fake_run(argv):
        calls.append(argv)
        return 0, "Already up to date.", ""

    t = _tool(tmp_path)
    t._run_subprocess = _fake_run
    res = await t.self_env_git_pull(GitPullParams(), execution_context=_owner_ctx())
    assert not res.error
    flat = " ".join(calls[0])
    assert "pull" in flat and "--ff-only" in flat


# --- restart_service -------------------------------------------------------------

@pytest.mark.asyncio
async def test_restart_service_refuses_when_unsupervised(monkeypatch, tmp_path):
    _posture(monkeypatch, "2")
    t = _tool(tmp_path)
    res = await t.self_env_restart_service(RestartParams(), execution_context=_owner_ctx())
    assert res.error and "supervis" in res.error.lower()


@pytest.mark.asyncio
async def test_restart_service_schedules_when_supervised(monkeypatch, tmp_path):
    _posture(monkeypatch, "2")
    monkeypatch.setenv("POLYROB_SUPERVISED", "1")
    scheduled = []
    t = _tool(tmp_path)
    t._schedule_restart = lambda: scheduled.append(True)
    res = await t.self_env_restart_service(RestartParams(), execution_context=_owner_ctx())
    assert not res.error and scheduled == [True]


# --- audit events ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_every_verb_emits_self_modification_event(monkeypatch, tmp_path):
    _posture(monkeypatch, "2")
    (tmp_path / "mod.py").write_text("A = 1\n")
    events = []
    t = _tool(tmp_path, events=events)

    async def _fake_run(argv):
        return 0, "ok", ""
    t._run_subprocess = _fake_run

    await t.self_env_patch_source(
        PatchSourceParams(path="mod.py", old_string="A = 1", new_string="A = 2"),
        execution_context=_owner_ctx())
    await t.self_env_install_dep(InstallDepParams(package="flask"), execution_context=_owner_ctx())
    assert any(e.get("action") == "patch_source" for e in events)
    assert any(e.get("action") == "install_dep" for e in events)


# --- delegation / registration guardrails ----------------------------------------

def test_self_env_is_delegation_blocked():
    from tools.controller.delegation import DELEGATE_BLOCKED_TOOLS
    assert "self_env" in DELEGATE_BLOCKED_TOOLS


def test_self_env_registered_only_at_posture_2(monkeypatch):
    from tools.self_env import self_env_enabled
    monkeypatch.delenv("AGENT_COMPUTE_POSTURE", raising=False)
    c._refreeze_compute_posture_for_tests()
    assert self_env_enabled() is False
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "2")
    c._refreeze_compute_posture_for_tests()
    assert self_env_enabled() is True
