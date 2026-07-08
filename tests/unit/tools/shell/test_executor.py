"""WS-2/WS-3: DockerShellExecutor — foreground run + background launch + job ops.

Foreground reuses the persistent DockerBackend's hardened `run()` (docker exec
with timeout); background needs a detached exec so a server survives the call.
Poll/log/kill are themselves short foreground bash one-liners against the same
container. Tested against a fake backend — no docker daemon.
"""
import pytest

from tools.shell.executor import DockerShellExecutor
from tools.shell.state import ShellState, STATE_SENTINEL
from tools.code_exec.result import ExecutionResult


class _FakeBackend:
    """Records run()/exec_detached() calls; returns programmable results."""

    def __init__(self):
        self.runs = []
        self.detached = []
        self._next = {}

    def program(self, substr, result):
        self._next[substr] = result

    async def run(self, request):
        self.runs.append(request)
        for substr, res in self._next.items():
            if substr in request.code:
                return res
        return ExecutionResult(stdout="", exit_code=0, backend="fake")

    async def exec_detached(self, script):
        self.detached.append(script)
        return 0


@pytest.mark.asyncio
async def test_foreground_run_wraps_and_parses_state():
    be = _FakeBackend()
    be.program("pwd", ExecutionResult(
        stdout=f"done\n{STATE_SENTINEL}\x1e__CWD__\x1e/workspace/sub\n\x1e__ENV__\x1eX=1\n",
        exit_code=0, backend="fake",
    ))
    ex = DockerShellExecutor(be)
    state = ShellState()
    clean, new_state, rc = await ex.run_foreground("mkdir sub && cd sub", state, timeout=10)
    assert clean == "done"
    assert new_state.cwd == "/workspace/sub"
    assert new_state.env == {"X": "1"}
    assert rc == 0
    # the wrapped script (not the raw command) reached the backend
    assert "pwd" in be.runs[0].code and "cd sub" in be.runs[0].code
    assert be.runs[0].dev_mode is True


@pytest.mark.asyncio
async def test_foreground_preserves_nonzero_exit_and_prev_state_on_missing_trailer():
    be = _FakeBackend()
    be.program("boom", ExecutionResult(stdout="partial", exit_code=3, backend="fake"))
    ex = DockerShellExecutor(be)
    state = ShellState(cwd="/workspace/keep", env={"A": "1"})
    clean, new_state, rc = await ex.run_foreground("boom", state, timeout=10)
    assert clean == "partial"
    assert new_state.cwd == "/workspace/keep"  # trailer absent -> cwd preserved
    assert new_state.env == {"A": "1"}
    assert rc == 3


@pytest.mark.asyncio
async def test_start_background_detaches_and_writes_pid_and_log():
    be = _FakeBackend()
    ex = DockerShellExecutor(be)
    await ex.start_background("flask run", "job-abc", ShellState(cwd="/workspace/app"))
    assert be.detached, "background must use a detached exec"
    script = be.detached[0]
    assert "job-abc" in script
    # control files live under a world-writable /tmp dir (non-root container user
    # can't write the root-owned /workspace bind mount — prod EACCES fix)
    assert "/tmp/" in script
    assert "job-abc.pid" in script and "job-abc.log" in script
    assert "flask run" in script
    assert "cd /workspace/app" in script  # runs in the session cwd


@pytest.mark.asyncio
async def test_poll_reports_running_then_done():
    be = _FakeBackend()
    ex = DockerShellExecutor(be)
    be.program("kill -0", ExecutionResult(stdout="RUNNING", exit_code=0, backend="fake"))
    status = await ex.poll("job-abc")
    assert status == "running"
    be.program("kill -0", ExecutionResult(stdout="DONE 0", exit_code=0, backend="fake"))
    assert await ex.poll("job-abc") == "done"


@pytest.mark.asyncio
async def test_read_log_returns_capped_buffer():
    be = _FakeBackend()
    ex = DockerShellExecutor(be)
    be.program("cat", ExecutionResult(stdout="log output here", exit_code=0, backend="fake"))
    out = await ex.read_log("job-abc", max_bytes=1000)
    assert out == "log output here"
    assert "job-abc.log" in be.runs[-1].code


@pytest.mark.asyncio
async def test_kill_targets_the_process_group_and_children():
    be = _FakeBackend()
    ex = DockerShellExecutor(be)
    await ex.kill("job-abc")
    killed = be.runs[-1].code
    assert "kill" in killed and "job-abc.pid" in killed
    # negative pid == process-group kill; pkill -P also reaps direct children so a
    # multi-process server (workers) doesn't orphan them
    assert "-\"$pid\"" in killed or "pkill" in killed


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_id", ["a; rm -rf /", "../etc", "$(evil)", "a b", "x`id`"])
async def test_executor_rejects_hostile_job_id(bad_id):
    """Defense-in-depth: even though the process tool membership-checks ids, the
    executor must never interpolate a job_id containing shell metacharacters."""
    be = _FakeBackend()
    ex = DockerShellExecutor(be)
    with pytest.raises(ValueError):
        await ex.poll(bad_id)
    with pytest.raises(ValueError):
        await ex.kill(bad_id)
    with pytest.raises(ValueError):
        await ex.read_log(bad_id)
