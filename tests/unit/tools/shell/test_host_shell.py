"""073 W1/W3: the host shell (posture 3) — gate, selection, executor, result shape.

Gate: each of the four conditions alone refuses (posture 3 owner turn,
POLYROB_LOCAL, no in-process signing key, a terminal seat). Selection: the host is
never a fallback. Executor: a REAL bash on this machine — cwd/env persist, a
timeout kills the group, a background job survives the call and is killable.
"""
import asyncio
import logging
import os
import sys
import types

import pytest

import agents.task.constants as c
import core.security.host_execution as he
from tools.controller.execution_context import ActionExecutionContext
from tools.shell import backend_pool
from tools.shell.host_executor import HostShellExecutor, host_child_env
from tools.shell.output import shape_output, strip_ansi
from tools.shell.process_registry import ProcessRegistry
from tools.shell.state import ShellState
from tools.shell.tool import ShellRunParams, ShellTool

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX shell")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("AGENT_COMPUTE_POSTURE", "POLYROB_LOCAL", "ROB_LOCAL", "POLYROB_OWNER_USER_ID",
              "SHELL_BACKEND"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(he, "host_execution_refusal", lambda: None)
    c._refreeze_compute_posture_for_tests()
    backend_pool._HOST.clear()
    yield
    for k in ("AGENT_COMPUTE_POSTURE", "POLYROB_LOCAL", "POLYROB_OWNER_USER_ID", "SHELL_BACKEND"):
        os.environ.pop(k, None)
    c._refreeze_compute_posture_for_tests()
    backend_pool._HOST.clear()


def _ctx(tmp_path=None, **kw):
    d = dict(role="orchestrator", is_sub_agent=False, user_id="local", session_id="s1",
             metadata={"turn_kind": None, "seat": "terminal"},
             workspace_dir=str(tmp_path) if tmp_path else None)
    d.update(kw)
    return ActionExecutionContext(**d)


def _host_on(monkeypatch):
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "3")
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    c._refreeze_compute_posture_for_tests()


# --- the gate ----------------------------------------------------------------------

def test_gate_passes_with_all_four(monkeypatch):
    _host_on(monkeypatch)
    assert he.host_shell_refusal(_ctx()) is None


def test_gate_needs_posture_3(monkeypatch):
    _host_on(monkeypatch)
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "2")
    c._refreeze_compute_posture_for_tests()
    assert "POSTURE=3" in he.host_shell_refusal(_ctx())


def test_gate_needs_local_mode(monkeypatch):
    _host_on(monkeypatch)
    monkeypatch.delenv("POLYROB_LOCAL")
    assert he.host_shell_refusal(_ctx()) is not None


def test_gate_refuses_custody(monkeypatch):
    _host_on(monkeypatch)
    monkeypatch.setattr(he, "host_execution_refusal", lambda: "custody held")
    assert he.host_shell_refusal(_ctx()) == "custody held"


@pytest.mark.parametrize("seat", [None, "telegram", "console", "TERMINAL", ""])
def test_gate_needs_terminal_seat(monkeypatch, seat):
    _host_on(monkeypatch)
    assert "terminal" in he.host_shell_refusal(_ctx(metadata={"turn_kind": None, "seat": seat}))


@pytest.mark.parametrize("kw", [
    dict(is_sub_agent=True),
    dict(role="leaf"),
    dict(metadata={"turn_kind": "self_wake", "seat": "terminal"}),
    dict(user_id="u_stranger"),
])
def test_gate_refuses_non_owner_turns(monkeypatch, kw):
    _host_on(monkeypatch)
    assert he.host_shell_refusal(_ctx(**kw)) is not None


def test_gate_fails_closed_on_none(monkeypatch):
    _host_on(monkeypatch)
    assert he.host_shell_refusal(None) is not None


def test_seat_stamp_is_strict():
    from agents.task.agent.core.step_execution import _seat_of
    assert _seat_of(types.SimpleNamespace(_terminal_attached=True)) == "terminal"
    assert _seat_of(types.SimpleNamespace()) is None
    from unittest.mock import MagicMock
    assert _seat_of(MagicMock()) is None  # a mock never reads as a terminal


# --- selection: the host is never a fallback ----------------------------------------

@pytest.mark.asyncio
async def test_auto_picks_host_when_allowed(monkeypatch, tmp_path):
    _host_on(monkeypatch)
    ex = await backend_pool.resolve_shell_executor(_ctx(tmp_path))
    assert ex.kind == "host" and ex.default_cwd == str(tmp_path)


@pytest.mark.asyncio
async def test_docker_failure_never_falls_to_host(monkeypatch, tmp_path):
    """A non-terminal turn at posture 3 gets the sandbox; a broken sandbox is a
    refusal, never the host."""
    _host_on(monkeypatch)

    async def _boom(sid):
        raise backend_pool.ShellBackendUnavailable("no docker")
    monkeypatch.setattr(backend_pool, "get_shell_backend", _boom)
    with pytest.raises(backend_pool.ShellBackendUnavailable):
        await backend_pool.resolve_shell_executor(
            _ctx(tmp_path, metadata={"turn_kind": None, "seat": None}))


@pytest.mark.asyncio
async def test_shell_backend_docker_never_host(monkeypatch, tmp_path):
    _host_on(monkeypatch)
    monkeypatch.setenv("SHELL_BACKEND", "docker")
    assert backend_pool.host_selected(_ctx(tmp_path)) is False


@pytest.mark.asyncio
async def test_shell_backend_host_refuses_instead_of_sandbox(monkeypatch, tmp_path):
    _host_on(monkeypatch)
    monkeypatch.setenv("SHELL_BACKEND", "host")
    with pytest.raises(backend_pool.ShellBackendUnavailable):
        backend_pool.host_selected(_ctx(tmp_path, metadata={"turn_kind": None, "seat": None}))


@pytest.mark.asyncio
async def test_host_job_is_not_managed_from_a_non_host_turn(monkeypatch, tmp_path):
    _host_on(monkeypatch)
    with pytest.raises(backend_pool.ShellBackendUnavailable):
        await backend_pool.resolve_shell_executor(
            _ctx(tmp_path, metadata={"turn_kind": None, "seat": None}), kind="host")


# --- the executor (real bash) -------------------------------------------------------

def _ex(tmp_path):
    return HostShellExecutor(workspace_dir=str(tmp_path), jobs_dir=tmp_path / "jobs")


@pytest.mark.asyncio
async def test_cwd_and_env_persist(tmp_path):
    ex = _ex(tmp_path)
    (tmp_path / "sub").mkdir()
    st = ShellState(cwd=ex.default_cwd)
    out, st, rc = await ex.run_foreground("cd sub && export FOO=bar", st, timeout=10)
    assert rc == 0 and st.cwd.endswith("/sub") and st.env.get("FOO") == "bar"
    out, st, rc = await ex.run_foreground("pwd; echo $FOO", st, timeout=10)
    lines = out.splitlines()
    assert lines[0].endswith("/sub") and lines[-1] == "bar"


@pytest.mark.asyncio
async def test_child_env_has_no_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("AGENT_WALLET_MASTER_SEED", "seed words")
    env = host_child_env()
    assert "OPENAI_API_KEY" not in env and "AGENT_WALLET_MASTER_SEED" not in env
    ex = _ex(tmp_path)
    out, _, _ = await ex.run_foreground("echo ${OPENAI_API_KEY:-none}", ShellState(cwd=str(tmp_path)),
                                        timeout=10)
    assert out.strip() == "none"


@pytest.mark.asyncio
async def test_timeout_kills_the_group(tmp_path):
    ex = _ex(tmp_path)
    out, _, rc = await ex.run_foreground("sleep 30 & sleep 30", ShellState(cwd=str(tmp_path)),
                                         timeout=1)
    assert rc == 124 and "timed out" in out


@pytest.mark.asyncio
async def test_workdir_is_one_call_only(tmp_path):
    ex = _ex(tmp_path)
    (tmp_path / "w").mkdir()
    st = ShellState(cwd=str(tmp_path))
    out, _, rc = await ex.run_foreground("pwd", st, timeout=10, workdir=str(tmp_path / "w"))
    assert rc == 0 and out.strip().endswith("/w")
    out, _, rc = await ex.run_foreground("true", st, timeout=10, workdir=str(tmp_path / "nope"))
    assert rc == 2


@pytest.mark.asyncio
async def test_background_job_survives_and_is_killable(tmp_path):
    ex = _ex(tmp_path)
    st = ShellState(cwd=str(tmp_path))
    await ex.start_background("echo started; sleep 30", "job-0001", st)
    assert await ex.poll("job-0001") == "running"
    await asyncio.sleep(0.3)
    assert "started" in await ex.read_log("job-0001")
    assert await ex.kill("job-0001") is True
    assert await ex.poll("job-0001") == "done"


@pytest.mark.asyncio
async def test_background_exit_code_is_recorded(tmp_path):
    ex = _ex(tmp_path)
    await ex.start_background("exit 3", "job-0002", ShellState(cwd=str(tmp_path)))
    for _ in range(100):
        if await ex.poll("job-0002") == "done":
            break
        await asyncio.sleep(0.05)
    for _ in range(50):
        if await ex.exit_code("job-0002") is not None:
            break
        await asyncio.sleep(0.05)
    assert await ex.exit_code("job-0002") == 3
    # job control files stay in the session dir, never /tmp
    assert (tmp_path / "jobs" / "job-0002.log").exists()


# --- the tool on the host -----------------------------------------------------------

def _tool():
    t = object.__new__(ShellTool)
    t.logger = logging.getLogger("host-shell-test")
    t._registry = ProcessRegistry()
    t._states = {}
    t._lock = asyncio.Lock()
    return t


@pytest.mark.asyncio
async def test_tool_runs_on_host_and_audits(monkeypatch, tmp_path):
    _host_on(monkeypatch)
    seen = []
    monkeypatch.setattr("core.event_log.emit", lambda kind, **kw: seen.append((kind, kw)))
    t = _tool()
    r = await t.shell_run(ShellRunParams(command="echo hi > f.txt && cat f.txt"),
                          execution_context=_ctx(tmp_path))
    assert r.error is None and r.extracted_content.strip() == "hi"
    assert (tmp_path / "f.txt").exists()
    assert seen and seen[0][0] == "host_exec"
    attrs = seen[0][1]["attrs"]
    assert attrs["exit_code"] == 0 and "cmd_sha256" in attrs and "echo" not in str(attrs)


@pytest.mark.asyncio
async def test_tool_refuses_sudo_on_host(monkeypatch, tmp_path):
    _host_on(monkeypatch)
    r = await _tool().shell_run(ShellRunParams(command="sudo ls"), execution_context=_ctx(tmp_path))
    assert r.error and "sudo" in r.error


@pytest.mark.asyncio
async def test_tool_refuses_floor_before_resolving(monkeypatch, tmp_path):
    _host_on(monkeypatch)
    t = _tool()

    async def _never(ctx):
        raise AssertionError("the floor must refuse before any executor resolves")
    t._resolve_executor = _never
    r = await t.shell_run(ShellRunParams(command="rm -rf ~"), execution_context=_ctx(tmp_path))
    assert r.error and "floor" in r.error


@pytest.mark.asyncio
async def test_tool_nonzero_exit_note(monkeypatch, tmp_path):
    _host_on(monkeypatch)
    r = await _tool().shell_run(ShellRunParams(command="definitely-not-a-command-xyz"),
                                execution_context=_ctx(tmp_path))
    assert r.error and "exited 127" in r.error and "not found" in r.error


# --- result shaping ----------------------------------------------------------------

def test_shape_truncates_head_and_tail_and_saves(tmp_path):
    text = "HEAD" + "x" * 5000 + "TAIL"
    out, path = shape_output(text, max_chars=1000, save_dir=tmp_path)
    assert out.startswith("HEAD") and out.endswith("TAIL") and "omitted" in out
    assert path and open(path).read() == text


def test_shape_save_dir_is_lazy():
    calls = []
    out, path = shape_output("short", save_dir=lambda: calls.append(1))
    assert out == "short" and path is None and calls == []


def test_shape_strips_ansi_and_redacts():
    assert strip_ansi("\x1b[31mred\x1b[0m") == "red"
    out, _ = shape_output("key=sk-ant-api03-" + "a" * 40)
    assert "a" * 40 not in out


@pytest.mark.asyncio
async def test_host_background_then_process_wait(monkeypatch, tmp_path):
    """shell_run(background) on the host -> process_wait reports the exit code."""
    _host_on(monkeypatch)
    from tools.shell.process_tool import ProcessTool, ProcessWaitParams
    reg = ProcessRegistry()
    t = _tool()
    t._registry = reg
    r = await t.shell_run(ShellRunParams(command="echo done-here; exit 4", background=True),
                          execution_context=_ctx(tmp_path))
    assert r.error is None and "job-" in r.extracted_content
    job = reg.list("s1")[0]
    assert job.backend == "host"
    p = object.__new__(ProcessTool)
    p.logger = logging.getLogger("process-test")
    p._registry = reg
    w = await p.process_wait(ProcessWaitParams(job_id=job.id, timeout=10),
                             execution_context=_ctx(tmp_path))
    assert w.error is None
    assert "done (exit 4" in w.extracted_content and "done-here" in w.extracted_content


@pytest.mark.asyncio
async def test_host_init_files_and_agent_forwarding(monkeypatch, tmp_path):
    rc = tmp_path / "rc.sh"
    rc.write_text("export FROM_RC=yes\necho noisy\n")
    monkeypatch.setenv("SHELL_HOST_INIT_FILES", f"{rc}, ~/does-not-exist")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent.sock")
    ex = _ex(tmp_path)
    out, _, rc_ = await ex.run_foreground("echo ${FROM_RC:-no} ${SSH_AUTH_SOCK:-none}",
                                          ShellState(cwd=str(tmp_path)), timeout=10)
    assert rc_ == 0 and out.strip() == "yes none"  # rc sourced quietly; agent socket off
    monkeypatch.setenv("SHELL_HOST_FORWARD_AGENT", "1")
    out, _, _ = await ex.run_foreground("echo ${SSH_AUTH_SOCK:-none}", ShellState(cwd=str(tmp_path)),
                                        timeout=10)
    assert out.strip() == "/tmp/agent.sock"
