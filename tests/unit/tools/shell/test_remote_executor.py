"""073 W6: the ShellExecutor protocol, the remote executor (ssh ControlMaster and
pack backends) and its selection in backend_pool.

The executor semantics run against a REAL local bash through a fake transport
(the same scripts an ssh or sandbox far side runs); the ssh transport is checked
by argv with a fake runner (no network, no ssh binary needed).
"""
import asyncio
import os
import sys
from pathlib import Path

import pytest

import agents.task.constants as c
import core.config_policy as cp
import core.security.host_execution as he
from tools.code_exec import pack_backends
from tools.code_exec.backends._proc import run_group
from tools.controller.execution_context import ActionExecutionContext
from tools.shell import backend_pool
from tools.shell.executor import DockerShellExecutor
from tools.shell.executor_protocol import (NotAShellExecutor, ShellExecutor, is_shell_executor,
                                           require_shell_executor)
from tools.shell.host_executor import HostShellExecutor
from tools.shell.remote_executor import (BackendTransport, RemoteShellExecutor, SshTransport,
                                         backend_shell_executor, control_dir_for,
                                         ssh_shell_executor)
from tools.shell.state import ShellState

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX shell")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("AGENT_COMPUTE_POSTURE", "POLYROB_LOCAL", "ROB_LOCAL", "SHELL_BACKEND",
              "CODE_EXEC_SSH_HOST", "CODE_EXEC_SSH_USER", "CODE_EXEC_SSH_SANDBOXED",
              "CODE_EXEC_SSH_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(he, "host_execution_refusal", lambda: None)
    c._refreeze_compute_posture_for_tests()
    backend_pool._REMOTE.clear()
    backend_pool._HOST.clear()
    yield
    backend_pool._REMOTE.clear()
    backend_pool._HOST.clear()
    pack_backends.unregister_source("test")
    c._refreeze_compute_posture_for_tests()


def _ctx(tmp_path, **kw):
    d = dict(role="orchestrator", is_sub_agent=False, user_id="local", session_id="s1",
             metadata={"turn_kind": None, "seat": "terminal"}, workspace_dir=str(tmp_path))
    d.update(kw)
    return ActionExecutionContext(**d)


def _server(monkeypatch, *, custody=False):
    monkeypatch.setattr(cp, "local_mode_enabled", lambda: False)
    monkeypatch.setattr(he, "wallet_custody_enabled", lambda: custody)


def _local(monkeypatch):
    monkeypatch.setattr(cp, "local_mode_enabled", lambda: True)
    monkeypatch.setattr(he, "wallet_custody_enabled", lambda: False)


class LocalBash:
    """A transport whose far side is a local bash (what ssh / a sandbox runs)."""

    name = "localbash"

    def __init__(self, home, sandbox=True):
        self.home = str(home)
        self.sandbox = sandbox
        self.scripts = []
        self.closed = False

    @property
    def capabilities(self):
        return {"sandbox": self.sandbox}

    async def setup(self):
        pass

    async def run_script(self, script, *, timeout):
        self.scripts.append(script)
        rc, out, err, timed_out = await run_group(["bash", "--noprofile", "--norc", "-c", script],
                                                  stdin_bytes=None, timeout=timeout,
                                                  label="t", cwd=self.home)
        return rc, out, err, timed_out

    async def close(self):
        self.closed = True


# --- the protocol ------------------------------------------------------------------

def test_every_executor_satisfies_the_protocol(tmp_path):
    host = HostShellExecutor(workspace_dir=str(tmp_path), jobs_dir=tmp_path / "j")
    docker = DockerShellExecutor(object())
    remote = RemoteShellExecutor(LocalBash(tmp_path), kind="ssh", default_cwd="w", jobs_dir="j")
    for ex in (host, docker, remote):
        assert is_shell_executor(ex) and isinstance(ex, ShellExecutor)
    assert not is_shell_executor(object())
    with pytest.raises(NotAShellExecutor) as e:
        require_shell_executor(object(), source="x")
    assert "run_foreground" in str(e.value)


# --- the executor against a real bash ----------------------------------------------

@pytest.mark.asyncio
async def test_remote_executor_persists_cwd_and_env(tmp_path):
    tr = LocalBash(tmp_path)
    ex = RemoteShellExecutor(tr, kind="ssh", default_cwd="polyrob/s1", jobs_dir=".jobs/s1")
    st = ShellState(cwd=ex.default_cwd)
    out, st, rc = await ex.run_foreground("mkdir -p sub && cd sub && export FOO=bar", st,
                                          timeout=10)
    assert rc == 0 and st.cwd.endswith("polyrob/s1/sub") and st.env["FOO"] == "bar"
    out, st, rc = await ex.run_foreground('echo "$FOO $(pwd)"', st, timeout=10)
    assert out.startswith("bar ") and out.endswith("/sub")
    assert (tmp_path / ".jobs" / "s1").is_dir()   # prepared once, relative to the far home
    out, st2, rc = await ex.run_foreground("exit 3", st, timeout=10)
    assert rc == 3 and st2.cwd == st.cwd
    out, _, rc = await ex.run_foreground("sleep 5", st, timeout=1)
    assert rc == 124 and "timed out" in out


@pytest.mark.asyncio
async def test_remote_background_job_lifecycle(tmp_path):
    tr = LocalBash(tmp_path)
    ex = RemoteShellExecutor(tr, kind="ssh", default_cwd=str(tmp_path / "w"),
                             jobs_dir=str(tmp_path / "jobs"))
    st = ShellState(cwd=ex.default_cwd, env={"GREETING": "hi"})
    await ex.start_background('echo "$GREETING from $(pwd)"; exit 7', "job-1", st)
    for _ in range(50):
        if await ex.poll("job-1") == "done":
            break
        await asyncio.sleep(0.1)
    assert await ex.poll("job-1") == "done"
    assert await ex.exit_code("job-1") == 7
    assert "hi from" in await ex.read_log("job-1")

    await ex.start_background("sleep 30", "job-2", st)
    assert await ex.poll("job-2") == "running"
    assert await ex.kill("job-2") is True
    for _ in range(30):
        if await ex.poll("job-2") == "done":
            break
        await asyncio.sleep(0.1)
    assert await ex.poll("job-2") == "done"
    assert await ex.poll("job-9") == "unknown"
    with pytest.raises(ValueError):
        await ex.poll("../etc")
    await ex.close()
    assert tr.closed


def test_launcher_quotes_the_command_and_state(tmp_path):
    ex = RemoteShellExecutor(LocalBash(tmp_path), kind="ssh", default_cwd="w", jobs_dir="j")
    script = ex.launcher("echo '$(rm -rf /)'", "job-1", ShellState(cwd="/a b", env={"X": "$(id)"}))
    assert "nohup $__s bash" in script and "setsid" in script
    assert "'/a b'" in script and "export X=" in script and "mkfifo" in script


@pytest.mark.asyncio
async def test_remote_job_stdin_incremental_log_and_workdir(tmp_path):
    (tmp_path / "other").mkdir()
    ex = RemoteShellExecutor(LocalBash(tmp_path), kind="ssh", default_cwd="w", jobs_dir="j")
    st = ShellState(cwd="w")
    await ex.start_background('pwd; while read l; do echo "got $l"; done; echo eof',
                              "job-1", st, workdir=str(tmp_path / "other"))
    assert ex._abs_jobs == str((tmp_path / "j").resolve()) or ex._abs_jobs.endswith("/j")
    ok, msg = await ex.write_stdin("job-1", b"one\n")
    assert ok, msg
    text, off = "", 0
    for _ in range(50):
        chunk, off = await ex.read_log_from("job-1", off)
        text += chunk
        if "got one" in text:
            break
        await asyncio.sleep(0.1)
    assert text.splitlines()[0].endswith("/other") and "got one" in text
    assert (await ex.close_stdin("job-1"))[0]
    for _ in range(50):
        if await ex.poll("job-1") == "done":
            break
        await asyncio.sleep(0.1)
    assert await ex.exit_code("job-1") == 0 and "eof" in await ex.read_log("job-1")
    assert (await ex.write_stdin("job-1", b"x"))[0] is False
    with pytest.raises(ValueError, match="pty"):
        await ex.start_background("true", "job-2", st, pty=True)


# --- ssh transport ---------------------------------------------------------------------

class FakeRunner:
    def __init__(self, rc=0, out="", err=""):
        self.calls = []
        self.rc, self.out, self.err = rc, out, err

    async def __call__(self, argv, *, stdin_bytes=None, timeout=None, label=""):
        self.calls.append(argv)
        return self.rc, self.out, self.err, False


@pytest.mark.asyncio
async def test_ssh_transport_uses_one_control_master(monkeypatch, tmp_path):
    monkeypatch.setenv("CODE_EXEC_SSH_HOST", "box.example")
    monkeypatch.setenv("CODE_EXEC_SSH_USER", "agent")
    runner = FakeRunner(out="hi")
    tr = SshTransport(control_dir=tmp_path, runner=runner)
    await tr.setup()
    rc, out, err, timed_out = await tr.run_script("echo $(whoami)", timeout=30)
    argv = runner.calls[0]
    assert argv[0] == "ssh" and "ControlMaster=auto" in argv and "ControlPersist=600" in argv
    assert f"ControlPath={tmp_path}/ssh-%C" in argv
    i = argv.index("--")
    assert argv[i + 1] == "agent@box.example"
    assert argv[-1].startswith("timeout --signal=KILL 30 bash --noprofile --norc -c ")
    assert "'echo $(whoami)'" in argv[-1]          # quoted: inert to the remote shell
    await tr.close()
    assert runner.calls[-1][-2:] == ["--", "agent@box.example"] and "-O" in runner.calls[-1]


@pytest.mark.asyncio
async def test_ssh_transport_maps_timeout_and_refuses_option_hosts(monkeypatch, tmp_path):
    monkeypatch.setenv("CODE_EXEC_SSH_HOST", "box")
    tr = SshTransport(control_dir=tmp_path, runner=FakeRunner(rc=137))
    assert (await tr.run_script("sleep 9", timeout=1))[3] is True
    monkeypatch.setenv("CODE_EXEC_SSH_HOST", "-oProxyCommand=evil")
    with pytest.raises(Exception, match="begins with '-'"):
        await SshTransport(control_dir=tmp_path, runner=FakeRunner()).setup()
    monkeypatch.delenv("CODE_EXEC_SSH_HOST")
    with pytest.raises(Exception, match="CODE_EXEC_SSH_HOST"):
        await SshTransport(control_dir=tmp_path, runner=FakeRunner()).setup()


def test_control_dir_moves_to_a_short_private_dir(tmp_path):
    short = control_dir_for(Path("/tmp") / "pr-short")
    assert str(short) == "/tmp/pr-short"
    long_dir = tmp_path / ("x" * 80)
    moved = control_dir_for(long_dir)
    assert len(str(moved / "ssh-%C")) + 40 < 104 and moved.is_dir()
    assert oct(os.stat(moved).st_mode & 0o777) == "0o700"


def test_ssh_executor_paths_are_per_session(tmp_path):
    ex = ssh_shell_executor(session_id="abc", session_dir=tmp_path)
    assert ex.kind == "ssh" and ex.default_cwd == "polyrob/abc"
    weird = ssh_shell_executor(session_id="a/b c", session_dir=tmp_path)
    assert "/" not in weird.default_cwd.split("polyrob/", 1)[1]


# --- selection ---------------------------------------------------------------------------

def test_choice_values(monkeypatch):
    for raw, want in (("", "auto"), ("SSH", "ssh"), ("modal", "modal"),
                      ("not a name!", "auto"), ("docker", "docker")):
        monkeypatch.setenv("SHELL_BACKEND", raw)
        assert backend_pool.shell_backend_choice() == want


@pytest.mark.asyncio
async def test_ssh_is_explicit_and_never_the_host(monkeypatch, tmp_path):
    _local(monkeypatch)
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "3")
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    c._refreeze_compute_posture_for_tests()
    monkeypatch.setenv("SHELL_BACKEND", "ssh")
    monkeypatch.setenv("CODE_EXEC_SSH_HOST", "box")
    assert backend_pool.host_selected(_ctx(tmp_path)) is False
    ex = await backend_pool.resolve_shell_executor(_ctx(tmp_path))
    assert ex.kind == "ssh" and isinstance(ex, RemoteShellExecutor)
    # the `process` tool pins the job's kind and gets the SAME pooled executor
    assert await backend_pool.resolve_shell_executor(_ctx(tmp_path), kind="ssh") is ex
    await backend_pool.teardown_session("s1")
    assert not backend_pool._REMOTE


@pytest.mark.asyncio
async def test_ssh_on_a_server_needs_the_sandbox_attestation(monkeypatch, tmp_path):
    _server(monkeypatch)
    monkeypatch.setenv("SHELL_BACKEND", "ssh")
    monkeypatch.setenv("CODE_EXEC_SSH_HOST", "box")
    with pytest.raises(backend_pool.ShellBackendUnavailable, match="CODE_EXEC_SSH_SANDBOXED"):
        await backend_pool.resolve_shell_executor(_ctx(tmp_path))
    backend_pool._REMOTE.clear()
    monkeypatch.setenv("CODE_EXEC_SSH_SANDBOXED", "true")
    ex = await backend_pool.resolve_shell_executor(_ctx(tmp_path))
    assert ex.kind == "ssh"


@pytest.mark.asyncio
async def test_local_custody_also_needs_a_sandbox(monkeypatch, tmp_path):
    monkeypatch.setattr(cp, "local_mode_enabled", lambda: True)
    monkeypatch.setattr(he, "wallet_custody_enabled", lambda: True)
    monkeypatch.setenv("SHELL_BACKEND", "ssh")
    monkeypatch.setenv("CODE_EXEC_SSH_HOST", "box")
    with pytest.raises(backend_pool.ShellBackendUnavailable):
        await backend_pool.resolve_shell_executor(_ctx(tmp_path))


@pytest.mark.asyncio
async def test_an_unknown_backend_refuses_and_never_degrades(monkeypatch, tmp_path):
    _local(monkeypatch)
    monkeypatch.setenv("SHELL_BACKEND", "nosuch")
    with pytest.raises(backend_pool.ShellBackendUnavailable, match="names no loaded"):
        await backend_pool.resolve_shell_executor(_ctx(tmp_path))
    assert backend_pool.host_selected(_ctx(tmp_path)) is False


class FakeBackend:
    name = "fakebox"

    def __init__(self, *, session_id=None, dev_mode=False, sandbox=True):
        self.session_id, self.sandbox = session_id, sandbox
        self.torn = False

    @property
    def capabilities(self):
        return {"sandbox": self.sandbox}

    async def setup(self):
        pass

    async def run(self, request):
        from tools.code_exec.result import ExecutionResult
        rc, out, err, to = await run_group(["bash", "-c", request.code], stdin_bytes=None,
                                           timeout=request.timeout, label="t")
        return ExecutionResult(stdout=out, stderr=err, exit_code=rc, timed_out=to)

    async def teardown(self):
        self.torn = True


@pytest.mark.asyncio
async def test_a_pack_backend_resolves_through_the_seam(monkeypatch, tmp_path):
    _server(monkeypatch)
    made = {}

    def shell_factory(*, session_id, workspace_dir, logs_dir):
        made["b"] = FakeBackend(session_id=session_id)
        return backend_shell_executor(made["b"], kind="fakebox", session_id=session_id,
                                      default_cwd=str(tmp_path / "w"),
                                      jobs_root=str(tmp_path / "jobs"))

    pack_backends.register_exec_backend(pack_backends.ExecBackendContribution(
        name="fakebox", backend=FakeBackend, shell_executor=shell_factory), source="test")
    monkeypatch.setenv("SHELL_BACKEND", "fakebox")
    ex = await backend_pool.resolve_shell_executor(_ctx(tmp_path))
    assert ex.kind == "fakebox" and isinstance(ex.transport, BackendTransport)
    out, st, rc = await ex.run_foreground("echo hello", ShellState(cwd=ex.default_cwd),
                                          timeout=10)
    assert rc == 0 and out == "hello" and st.cwd == str(tmp_path / "w")
    await backend_pool.teardown_session("s1")
    assert made["b"].torn


@pytest.mark.asyncio
async def test_a_pack_factory_that_returns_junk_is_refused(monkeypatch, tmp_path):
    _local(monkeypatch)
    pack_backends.register_exec_backend(pack_backends.ExecBackendContribution(
        name="junkbox", backend=FakeBackend,
        shell_executor=lambda **kw: object()), source="test")
    monkeypatch.setenv("SHELL_BACKEND", "junkbox")
    with pytest.raises(backend_pool.ShellBackendUnavailable, match="not a shell executor"):
        await backend_pool.resolve_shell_executor(_ctx(tmp_path))


@pytest.mark.asyncio
async def test_teardown_stops_the_sessions_watchers(monkeypatch):
    import tools.shell.watch as watch
    seen = []
    monkeypatch.setattr(watch, "stop_watches", lambda sid=None: seen.append(sid) or [])
    await backend_pool.teardown_session("s7")
    assert seen == ["s7"]
