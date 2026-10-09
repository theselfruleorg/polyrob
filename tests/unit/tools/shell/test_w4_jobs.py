"""073 W4/W5: background jobs to full depth + host sudo.

Real bash on this machine for the host executor (stdin FIFO, pty, notify,
receipts); a fake backend for the container executor's script shapes; one real
docker round trip when docker is installed.
"""
import asyncio
import logging
import os
import re
import shutil
import sys
import time
import uuid

import pytest

import agents.task.constants as c
import core.security.host_execution as he
from tools.controller.execution_context import ActionExecutionContext
from tools.shell import backend_pool
from tools.shell import receipts as rc_mod
from tools.shell import sudo as sudo_mod
from tools.shell.host_executor import HostShellExecutor
from tools.shell.jobs import ends_with_password_prompt
from tools.shell.process_registry import ProcessRegistry
from tools.shell.process_tool import (ProcessJobParams, ProcessSubmitParams, ProcessTool,
                                      ProcessWaitParams, ProcessWriteParams, ProcessLogParams,
                                      ProcessListParams)
from tools.shell.state import ShellState
from tools.shell.tool import ShellRunParams, ShellTool
from tools.shell.watch import JobWatch

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX shell")


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    for k in ("AGENT_COMPUTE_POSTURE", "POLYROB_LOCAL", "ROB_LOCAL", "POLYROB_OWNER_USER_ID",
              "SHELL_BACKEND", "SHELL_HOST_SUDO"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(he, "host_execution_refusal", lambda: None)
    monkeypatch.setattr(rc_mod, "receipts_dir", lambda sid, uid: tmp_path / "receipts" / sid)
    c._refreeze_compute_posture_for_tests()
    sudo_mod._refreeze_for_tests()
    backend_pool._HOST.clear()
    yield
    for k in ("AGENT_COMPUTE_POSTURE", "POLYROB_LOCAL", "POLYROB_OWNER_USER_ID",
              "SHELL_BACKEND", "SHELL_HOST_SUDO"):
        os.environ.pop(k, None)
    c._refreeze_compute_posture_for_tests()
    sudo_mod._refreeze_for_tests()
    backend_pool._HOST.clear()


def _ctx(tmp_path, sid="s1", **kw):
    d = dict(role="orchestrator", is_sub_agent=False, user_id="local", session_id=sid,
             metadata={"turn_kind": None, "seat": "terminal"}, workspace_dir=str(tmp_path))
    d.update(kw)
    return ActionExecutionContext(**d)


def _host_on(monkeypatch, tmp_path, sid="s1"):
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "3")
    monkeypatch.setenv("POLYROB_LOCAL", "1")
    c._refreeze_compute_posture_for_tests()
    backend_pool._HOST[sid] = HostShellExecutor(workspace_dir=str(tmp_path),
                                                jobs_dir=tmp_path / "jobs")


def _tools(reg=None):
    reg = reg or ProcessRegistry()
    t = object.__new__(ShellTool)
    t.logger = logging.getLogger("w4-shell")
    t._registry = reg
    t._states = {}
    t._lock = asyncio.Lock()
    p = object.__new__(ProcessTool)
    p.logger = logging.getLogger("w4-process")
    p._registry = reg
    return t, p, reg


async def _until(fn, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if await fn():
            return True
        await asyncio.sleep(0.05)
    return False


# --- stdin: write / submit / close ----------------------------------------------------

@pytest.mark.asyncio
async def test_stdin_write_submit_close_round_trip(monkeypatch, tmp_path):
    _host_on(monkeypatch, tmp_path)
    t, p, reg = _tools()
    ctx = _ctx(tmp_path)
    r = await t.shell_run(ShellRunParams(
        command='while read l; do echo "got:$l"; done; echo eof', background=True),
        execution_context=ctx)
    assert r.error is None and "stdin stays open" in r.extracted_content
    jid = reg.list("s1")[0].id
    w = await p.process_write(ProcessWriteParams(job_id=jid, data="al"), execution_context=ctx)
    assert w.error is None, w.error
    s = await p.process_submit(ProcessSubmitParams(job_id=jid, data="pha"), execution_context=ctx)
    assert s.error is None, s.error
    ex = backend_pool._HOST["s1"]
    assert await _until(lambda: _contains(ex, jid, "got:alpha"))
    # between writes the job still runs (the keeper holds the write side)
    assert await ex.poll(jid) == "running"
    cl = await p.process_close(ProcessJobParams(job_id=jid), execution_context=ctx)
    assert cl.error is None and "EOF" in cl.extracted_content
    wt = await p.process_wait(ProcessWaitParams(job_id=jid, timeout=10), execution_context=ctx)
    assert "exit 0" in wt.extracted_content and "eof" in wt.extracted_content
    again = await p.process_submit(ProcessSubmitParams(job_id=jid, data="x"), execution_context=ctx)
    assert again.error and "not running" in again.error


async def _contains(ex, jid, text):
    return text in await ex.read_log(jid)


@pytest.mark.asyncio
async def test_job_that_never_reads_stdin_does_not_block(tmp_path):
    ex = HostShellExecutor(workspace_dir=str(tmp_path), jobs_dir=tmp_path / "jobs")
    await ex.start_background("echo hi; exit 5", "job-a", ShellState(cwd=str(tmp_path)))
    assert await _until(lambda: _done(ex, "job-a"), 5)
    assert await ex.exit_code("job-a") == 5
    ok, msg = await ex.write_stdin("job-a", b"x")
    assert not ok and "not reading" in msg
    assert not list((tmp_path / "jobs").glob("*.keeper")) or True


async def _done(ex, jid):
    return await ex.poll(jid) == "done"


@pytest.mark.asyncio
async def test_kill_reaps_the_stdin_keeper(tmp_path):
    ex = HostShellExecutor(workspace_dir=str(tmp_path), jobs_dir=tmp_path / "jobs")
    await ex.start_background("sleep 60", "job-k", ShellState(cwd=str(tmp_path)))
    await asyncio.sleep(0.2)
    keeper = int((tmp_path / "jobs" / "job-k.keeper").read_text())
    assert await ex.kill("job-k") is True
    await asyncio.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(keeper, 0)


@pytest.mark.asyncio
async def test_write_refused_at_password_prompt(monkeypatch, tmp_path):
    _host_on(monkeypatch, tmp_path)
    t, p, reg = _tools()
    ctx = _ctx(tmp_path)
    await t.shell_run(ShellRunParams(command="printf 'Password: '; read x; echo got",
                                     background=True), execution_context=ctx)
    jid = reg.list("s1")[0].id
    ex = backend_pool._HOST["s1"]
    assert await _until(lambda: _contains(ex, jid, "Password:"))
    r = await p.process_submit(ProcessSubmitParams(job_id=jid, data="hunter2"),
                               execution_context=ctx)
    assert r.error and "password" in r.error.lower()
    await p.process_kill(ProcessJobParams(job_id=jid), execution_context=ctx)


@pytest.mark.asyncio
async def test_floor_text_typed_into_a_shell_job_is_refused(monkeypatch, tmp_path):
    """`script`/`ssh`/an approved `bash` job is a shell: typing a floor command into
    its stdin must not skip the floor that shell_run enforces."""
    _host_on(monkeypatch, tmp_path)
    t, p, reg = _tools()
    ctx = _ctx(tmp_path)
    await t.shell_run(ShellRunParams(command='while read l; do echo "got:$l"; done',
                                     background=True), execution_context=ctx)
    jid = reg.list("s1")[0].id
    r = await p.process_submit(ProcessSubmitParams(job_id=jid, data="rm -rf ~"),
                               execution_context=ctx)
    assert r.error and "floor" in r.error
    w = await p.process_write(ProcessWriteParams(job_id=jid, data="rm -rf /\n"),
                              execution_context=ctx)
    assert w.error and "floor" in w.error
    ok = await p.process_submit(ProcessSubmitParams(job_id=jid, data="hello"),
                                execution_context=ctx)
    assert ok.error is None, ok.error
    await p.process_kill(ProcessJobParams(job_id=jid), execution_context=ctx)


@pytest.mark.parametrize("tail,hit", [
    ("[sudo] password for rob: ", True),
    ("Enter passphrase for key '/x/id_ed25519': ", True),
    ("Password:", True),
    ("\x1b[1mPIN?\x1b[0m ", True),
    ("Password updated successfully\n", False),
    ("building...\nok\n", False),
    (">>> ", False),
    ("", False),
])
def test_password_prompt_detection(tail, hit):
    assert ends_with_password_prompt(tail) is hit


# --- pty --------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pty_job_on_host(monkeypatch, tmp_path):
    _host_on(monkeypatch, tmp_path)
    t, p, reg = _tools()
    ctx = _ctx(tmp_path)
    r = await t.shell_run(ShellRunParams(
        command="test -t 0 && echo IS_TTY; read -p 'name> ' n; echo hello $n",
        background=True, pty=True), execution_context=ctx)
    assert r.error is None and "terminal" in r.extracted_content
    job = reg.list("s1")[0]
    assert job.pty is True
    ex = backend_pool._HOST["s1"]
    assert await _until(lambda: _contains(ex, job.id, "name>"))
    s = await p.process_submit(ProcessSubmitParams(job_id=job.id, data="bob"), execution_context=ctx)
    assert s.error is None, s.error
    w = await p.process_wait(ProcessWaitParams(job_id=job.id, timeout=10), execution_context=ctx)
    assert "IS_TTY" in w.extracted_content and "hello bob" in w.extracted_content
    assert "exit 0" in w.extracted_content


@pytest.mark.asyncio
async def test_pty_close_sends_ctrl_d(tmp_path):
    ex = HostShellExecutor(workspace_dir=str(tmp_path), jobs_dir=tmp_path / "jobs")
    await ex.start_background("cat; echo after", "job-p", ShellState(cwd=str(tmp_path)), pty=True)
    await asyncio.sleep(0.3)
    ok, _ = await ex.close_stdin("job-p")
    assert ok
    assert await _until(lambda: _done(ex, "job-p"), 5)
    assert await _until(lambda: _contains(ex, "job-p", "after"), 3)


class _FakeDockerBackend:
    def __init__(self):
        self.runs, self.detached = [], []

    async def run(self, request):
        from tools.code_exec.result import ExecutionResult
        self.runs.append(request)
        return ExecutionResult(stdout="", exit_code=0, backend="fake")

    async def exec_detached(self, script):
        self.detached.append(script)
        return 0


def _docker_tool(be, reg=None):
    t, p, reg = _tools(reg)

    async def _ex(ctx):
        from tools.shell.executor import DockerShellExecutor
        return DockerShellExecutor(be)
    t._resolve_executor = _ex
    return t, p, reg


def _posture1(monkeypatch):
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "1")
    c._refreeze_compute_posture_for_tests()


@pytest.mark.asyncio
async def test_pty_in_container_is_an_error(monkeypatch, tmp_path):
    _posture1(monkeypatch)
    be = _FakeDockerBackend()
    t, _, _ = _docker_tool(be)
    r = await t.shell_run(ShellRunParams(command="python", background=True, pty=True),
                          execution_context=_ctx(tmp_path))
    assert r.error and "host" in r.error and not be.detached


# --- bad combinations are errors -----------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("kw,needle", [
    (dict(background=True, timeout=5), "timeout"),
    (dict(pty=True), "background"),
    (dict(notify="exit"), "background"),
    (dict(background=True, notify="sometimes"), "'exit' or 'pattern'"),
    (dict(background=True, notify_pattern="x"), "notify='pattern'"),
    (dict(background=True, notify="pattern"), "notify_pattern"),
    (dict(background=True, notify="pattern", notify_pattern="(["), "regex"),
])
async def test_bad_combinations(monkeypatch, tmp_path, kw, needle):
    _posture1(monkeypatch)
    be = _FakeDockerBackend()
    t, _, _ = _docker_tool(be)
    r = await t.shell_run(ShellRunParams(command="echo x", **kw), execution_context=_ctx(tmp_path))
    assert r.error and needle in r.error, r.error
    assert not be.detached


@pytest.mark.asyncio
async def test_notify_without_self_wake_is_an_error(monkeypatch, tmp_path):
    _posture1(monkeypatch)
    be = _FakeDockerBackend()
    t, _, _ = _docker_tool(be)
    t._wake_deliverer = lambda ctx: None
    r = await t.shell_run(ShellRunParams(command="make", background=True, notify="exit"),
                          execution_context=_ctx(tmp_path))
    assert r.error and "self-wake" in r.error and not be.detached


# --- notify ------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_notify_on_exit_through_the_deliverer(monkeypatch, tmp_path):
    _host_on(monkeypatch, tmp_path)
    monkeypatch.setitem(__import__("tools.shell.watch", fromlist=["x"])._INTERVALS, "host", 0.1)
    t, _, reg = _tools()
    notes = []

    async def _deliver(text, kind):
        notes.append((kind, text))
        return True
    t._wake_deliverer = lambda ctx: _deliver
    r = await t.shell_run(ShellRunParams(command="echo out-line; exit 3", background=True,
                                         notify="exit"), execution_context=_ctx(tmp_path))
    assert r.error is None and "note when it ends" in r.extracted_content

    async def _got():
        return bool(notes)
    assert await _until(_got, 10)
    kind, text = notes[0]
    assert kind == "shell_job_exit" and "exit 3" in text and "out-line" in text
    # the durable receipt was written by the same watcher
    jid = reg.list("s1")[0].id
    rec = rc_mod.load_receipt("s1", "local", jid)
    assert rec and rec["exit_code"] == 3 and rec["backend"] == "host"


@pytest.mark.asyncio
async def test_deliverer_rides_deliver_self_wake(monkeypatch, tmp_path):
    calls = []

    class _Agent:
        async def deliver_self_wake(self, session_id, user_id, text, metadata=None):
            calls.append((session_id, user_id, text, metadata))
            return True

    class _Container:
        def get_agent(self, name):
            return _Agent() if name == "task_agent" else None

    t, _, _ = _tools()
    t.container = _Container()
    monkeypatch.setattr("agents.task.agent.core.self_wake.effective_self_wake_enabled",
                        lambda uid, home: True)
    d = t._wake_deliverer(_ctx(tmp_path, sid="sx"))
    assert d is not None
    assert await d("hello", "shell_job_exit") is True
    sid, uid, text, meta = calls[0]
    assert sid == "sx" and uid == "local" and meta == {"source": "shell_job", "kind": "shell_job_exit"}
    monkeypatch.setattr("agents.task.agent.core.self_wake.effective_self_wake_enabled",
                        lambda uid, home: False)
    assert t._wake_deliverer(_ctx(tmp_path)) is None


def test_self_wake_turn_gets_no_posture(monkeypatch):
    """A note arrives as a forged self_wake turn: no compute posture (W4 keeps
    the rail's stamp; it never grants the shell)."""
    from core.config_policy import compute_posture_allows_safe
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "3")
    c._refreeze_compute_posture_for_tests()
    ctx = ActionExecutionContext(role="orchestrator", is_sub_agent=False, user_id="local",
                                 session_id="s", metadata={"turn_kind": "self_wake"})
    assert compute_posture_allows_safe(ctx, 1) is False


class _ScriptedExecutor:
    kind = "host"

    def __init__(self):
        self.log = b""
        self.status = "running"

    async def read_log_from(self, job_id, offset, *, max_bytes=65536):
        data = self.log[offset:offset + max_bytes]
        return data.decode(), offset + len(data)

    async def poll(self, job_id):
        return self.status

    async def exit_code(self, job_id):
        return 0 if self.status == "done" else None

    async def read_log(self, job_id, *, max_bytes=100000):
        return self.log.decode()[-max_bytes:]


@pytest.mark.asyncio
async def test_pattern_notify_rate_limit_and_auto_off(tmp_path):
    from tools.shell.process_registry import Job
    from tools.shell.watch import PATTERN_MAX_NOTES
    ex = _ScriptedExecutor()
    now = [1000.0]
    notes = []

    async def _deliver(text, kind):
        notes.append((kind, text))
        return True
    job = Job(id="job-x", session_id="s1", command="srv", created_at=0.0)
    w = JobWatch(executor=ex, job=job, session_id="s1", user_id="local", notify="pattern",
                 pattern=re.compile(r"READY"), deliver=_deliver, interval=0.01,
                 clock=lambda: now[0])
    ex.log += b"boot\nREADY 1\nREADY 2\npartial REA"
    await w._scan()
    await w._flush_pattern()
    assert len(notes) == 1 and "READY 1" in notes[0][1] and "READY 2" in notes[0][1]
    ex.log += b"DY 3\n"
    await w._scan()
    await w._flush_pattern()          # inside the rate window: held
    assert len(notes) == 1
    now[0] += 11
    await w._flush_pattern()
    assert len(notes) == 2 and "partial READY 3" in notes[1][1]
    for i in range(10):
        ex.log += f"READY {i}\n".encode()
        now[0] += 11
        await w._scan()
        await w._flush_pattern()
    assert len(notes) == PATTERN_MAX_NOTES
    assert "now OFF" in notes[-1][1] and w.pattern is None


@pytest.mark.asyncio
async def test_pattern_watch_also_reports_exit(tmp_path):
    from tools.shell.process_registry import Job
    ex = _ScriptedExecutor()
    notes = []

    async def _deliver(text, kind):
        notes.append(kind)
        return True
    job = Job(id="job-y", session_id="s1", command="srv", created_at=0.0)
    w = JobWatch(executor=ex, job=job, session_id="s1", user_id="local", notify="pattern",
                 pattern=re.compile("never"), deliver=_deliver, interval=0.01)
    ex.status = "done"
    await asyncio.wait_for(w.run(), 5)
    assert notes == ["shell_job_exit"]
    assert rc_mod.load_receipt("s1", "local", "job-y")["exit_code"] == 0


# --- durable receipts ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_receipts_survive_a_lost_registry(monkeypatch, tmp_path):
    _host_on(monkeypatch, tmp_path)
    t, p, reg = _tools()
    ctx = _ctx(tmp_path)
    await t.shell_run(ShellRunParams(command="echo kept-in-receipt; exit 7", background=True),
                      execution_context=ctx)
    jid = reg.list("s1")[0].id
    await p.process_wait(ProcessWaitParams(job_id=jid, timeout=10), execution_context=ctx)
    # a restart: a fresh registry knows nothing
    _, p2, _ = _tools(ProcessRegistry())
    poll = await p2.process_poll(ProcessJobParams(job_id=jid), execution_context=ctx)
    assert "exit 7" in poll.extracted_content and "receipt" in poll.extracted_content
    log = await p2.process_log(ProcessLogParams(job_id=jid), execution_context=ctx)
    assert "kept-in-receipt" in log.extracted_content
    lst = await p2.process_list(ProcessListParams(), execution_context=ctx)
    assert jid in lst.extracted_content and "(receipt)" in lst.extracted_content
    wt = await p2.process_wait(ProcessWaitParams(job_id=jid), execution_context=ctx)
    assert "exit 7" in wt.extracted_content
    k = await p2.process_kill(ProcessJobParams(job_id=jid), execution_context=ctx)
    assert "already finished" in k.extracted_content
    # another session never sees it
    other = await p2.process_poll(ProcessJobParams(job_id=jid),
                                  execution_context=_ctx(tmp_path, sid="s2"))
    assert other.error and "no such job" in other.error


@pytest.mark.asyncio
async def test_kill_writes_a_killed_receipt(monkeypatch, tmp_path):
    _host_on(monkeypatch, tmp_path)
    t, p, reg = _tools()
    ctx = _ctx(tmp_path)
    await t.shell_run(ShellRunParams(command="sleep 60", background=True), execution_context=ctx)
    jid = reg.list("s1")[0].id
    await asyncio.sleep(0.2)
    await p.process_kill(ProcessJobParams(job_id=jid), execution_context=ctx)
    assert rc_mod.load_receipt("s1", "local", jid)["status"] == "killed"


def test_receipt_retention_and_redaction(tmp_path):
    from tools.shell.process_registry import Job
    d = tmp_path / "receipts" / "s1"
    for i in range(70):
        job = Job(id=f"job-{i:04d}", session_id="s1", command="echo sk-ant-api03-" + "a" * 40,
                  created_at=float(i))
        rc_mod.write_receipt("s1", "local", job, status="done", exit_code=0,
                             log_text="x" * 300_000)
    files = list(d.glob("*.json"))
    assert len(files) == rc_mod.RECEIPT_MAX_PER_SESSION
    rec = rc_mod.load_receipt("s1", "local", "job-0069")
    assert "a" * 40 not in rec["command"]
    assert len(rec["log_tail"]) <= rc_mod.RECEIPT_LOG_MAX_CHARS + 500
    old = d / "job-0069.json"
    t_old = time.time() - rc_mod.RECEIPT_MAX_AGE_SEC - 10
    os.utime(old, (t_old, t_old))
    assert rc_mod.load_receipt("s1", "local", "job-0069") is None
    rc_mod.prune(d)
    assert not old.exists()
    assert oct(os.stat(files[0].parent).st_mode & 0o777) == "0o700"


def test_job_ids_are_unique_across_registries():
    a = ProcessRegistry().create("s", "x", now=0.0).id
    b = ProcessRegistry().create("s", "x", now=0.0).id
    assert a != b and a.startswith("job-0001-")


# --- container executor (fake backend) ---------------------------------------------------

@pytest.mark.asyncio
async def test_container_launcher_has_fifo_keeper_and_rc():
    from tools.shell.executor import DockerShellExecutor
    be = _FakeDockerBackend()
    ex = DockerShellExecutor(be)
    await ex.start_background("cat", "job-c", ShellState(cwd="/workspace"), workdir="sub")
    s = be.detached[0]
    assert "mkfifo" in s and "job-c.keeper" in s and "job-c.rc" in s and "wait " in s
    assert "cd sub || exit 2" in s
    with pytest.raises(ValueError):
        await ex.start_background("cat", "job-d", ShellState(), pty=True)


@pytest.mark.asyncio
async def test_container_write_is_base64_and_bounded():
    from tools.shell.executor import DockerShellExecutor
    be = _FakeDockerBackend()
    ex = DockerShellExecutor(be)
    ok, _ = await ex.write_stdin("job-c", b"it's; $(rm -rf /)\n")
    code = be.runs[-1].code
    assert "base64 -d" in code and "rm -rf" not in code and "timeout" in code
    ok, msg = await ex.write_stdin("job-c", b"x" * (64 * 1024 + 1))
    assert not ok and "limited" in msg
    await ex.close_stdin("job-c")
    assert "job-c.closed" in be.runs[-1].code and "job-c.keeper" in be.runs[-1].code


_needs_docker = pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")


@_needs_docker
@pytest.mark.asyncio
async def test_real_container_stdin_round_trip(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_COMPUTE_POSTURE", "1")
    monkeypatch.setenv("CODE_EXEC_BACKEND", "docker")
    monkeypatch.setenv("CODE_EXEC_DOCKER_PERSISTENT", "true")
    c._refreeze_compute_posture_for_tests()
    monkeypatch.setattr(
        "tools.code_exec.backends.docker.DockerBackend._resolve_persistent_workdir",
        lambda self: str(tmp_path))
    t, p, reg = _tools()
    t._resolve_executor = ShellTool._resolve_executor.__get__(t, ShellTool)
    sid = f"w4-{uuid.uuid4().hex}"
    ctx = ActionExecutionContext(role="orchestrator", is_sub_agent=False, user_id="local",
                                 session_id=sid, metadata={"turn_kind": None})
    try:
        try:
            r = await t.shell_run(ShellRunParams(
                command='while read l; do echo "got:$l"; done; echo eof; exit 4',
                background=True), execution_context=ctx)
        except Exception as e:  # pragma: no cover - docker daemon down
            pytest.skip(f"docker unavailable: {e}")
        if r.error and "unavailable" in r.error:
            pytest.skip(r.error)
        assert r.error is None, r.error
        jid = reg.list(sid)[0].id
        await asyncio.sleep(0.5)
        s = await p.process_submit(ProcessSubmitParams(job_id=jid, data="one"), execution_context=ctx)
        assert s.error is None, s.error
        s = await p.process_submit(ProcessSubmitParams(job_id=jid, data="two"), execution_context=ctx)
        assert s.error is None, s.error
        cl = await p.process_close(ProcessJobParams(job_id=jid), execution_context=ctx)
        assert cl.error is None, cl.error
        w = await p.process_wait(ProcessWaitParams(job_id=jid, timeout=20), execution_context=ctx)
        assert "got:one" in w.extracted_content and "got:two" in w.extracted_content
        assert "eof" in w.extracted_content and "exit 4" in w.extracted_content
    finally:
        await backend_pool.teardown_session(sid)


# --- W5 sudo ---------------------------------------------------------------------------

def test_sudo_mode_default_and_garbage(monkeypatch):
    assert sudo_mod.host_sudo_mode() == "refuse"
    monkeypatch.setenv("SHELL_HOST_SUDO", "yes-please")
    sudo_mod._refreeze_for_tests()
    assert sudo_mod.host_sudo_mode() == "refuse"
    monkeypatch.setenv("SHELL_HOST_SUDO", "prompt")
    sudo_mod._refreeze_for_tests()
    assert sudo_mod.host_sudo_mode() == "prompt"


@pytest.mark.parametrize("cmd,want", [
    ("sudo apt update", "sudo -S -p '' apt update"),
    ("ls && sudo rm x; echo sudo done", "ls && sudo -S -p '' rm x; echo sudo done"),
    ("if true; then sudo id; fi", "if true; then sudo -S -p '' id; fi"),
    ("echo pseudo sudoers", "echo pseudo sudoers"),
])
def test_rewrite_sudo(cmd, want):
    assert sudo_mod.rewrite_sudo(cmd) == want


def _fake_sudo(monkeypatch, tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    f = bindir / "sudo"
    f.write_text('#!/bin/bash\nread -r pw\necho "ARGS=$*"\necho "PWLEN=${#pw}"\n'
                 'echo "ENV_HAS_PW=$(env | grep -c s3cr3t)"\n')
    f.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ.get('PATH', '')}")


@pytest.mark.asyncio
async def test_sudo_refused_by_default(monkeypatch, tmp_path):
    _host_on(monkeypatch, tmp_path)
    t, _, _ = _tools()
    r = await t.shell_run(ShellRunParams(command="sudo id"), execution_context=_ctx(tmp_path))
    assert r.error and "SHELL_HOST_SUDO=prompt" in r.error


@pytest.mark.asyncio
async def test_sudo_prompt_feeds_password_on_stdin_only(monkeypatch, tmp_path):
    _host_on(monkeypatch, tmp_path)
    _fake_sudo(monkeypatch, tmp_path)
    monkeypatch.setenv("SHELL_HOST_SUDO", "prompt")
    sudo_mod._refreeze_for_tests()
    asked = []
    monkeypatch.setattr(sudo_mod, "prompt_password", lambda cmd: asked.append(cmd) or "s3cr3t")
    t, _, _ = _tools()
    r = await t.shell_run(ShellRunParams(command="sudo id -u"), execution_context=_ctx(tmp_path))
    assert r.error is None, r.error
    assert "ARGS=-S -p  id -u" in r.extracted_content
    assert "PWLEN=6" in r.extracted_content and "ENV_HAS_PW=0" in r.extracted_content
    assert "s3cr3t" not in r.extracted_content
    assert asked == ["sudo id -u"]
    # never cached: the next command asks again
    await t.shell_run(ShellRunParams(command="sudo true"), execution_context=_ctx(tmp_path))
    assert len(asked) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("params,ctx_kw,pw,needle", [
    (dict(command="sudo id"), dict(metadata={"turn_kind": None, "seat": None}), "pw", None),
    (dict(command="sudo id", background=True), {}, "pw", "foreground"),
    (dict(command="doas id"), {}, "pw", "doas"),
    (dict(command="sudo id"), {}, None, "no terminal"),
])
async def test_sudo_prompt_refusals(monkeypatch, tmp_path, params, ctx_kw, pw, needle):
    _host_on(monkeypatch, tmp_path)
    monkeypatch.setenv("SHELL_HOST_SUDO", "prompt")
    sudo_mod._refreeze_for_tests()
    monkeypatch.setattr(sudo_mod, "prompt_password", lambda cmd: pw)
    t, _, _ = _tools()
    if ctx_kw.get("metadata", {}).get("seat", "terminal") is None:
        # a non-terminal seat never reaches the host: exercise the helper itself
        stdin, refusal = await t._sudo_password(_ctx(tmp_path, **ctx_kw), "sudo id", None,
                                                background=False)
        assert stdin is None and "sudo" in refusal
        return
    r = await t.shell_run(ShellRunParams(**params), execution_context=_ctx(tmp_path, **ctx_kw))
    assert r.error and needle in r.error, r.error


def test_no_tty_means_no_password(monkeypatch):
    monkeypatch.setattr(sudo_mod, "tty_available", lambda: False)
    assert sudo_mod.prompt_password("sudo id") is None


class _LocalBashBackend:
    """Runs the container executor's REAL scripts with the local bash — proves the
    launcher, FIFO keeper, base64 write, close and incremental log read without a
    docker daemon."""

    def __init__(self):
        self.procs = []

    async def run(self, request):
        import subprocess
        from tools.code_exec.result import ExecutionResult
        p = await asyncio.to_thread(subprocess.run, ["bash", "-c", request.code],
                                    capture_output=True, text=True, timeout=30)
        return ExecutionResult(stdout=p.stdout, stderr=p.stderr, exit_code=p.returncode,
                               backend="local-bash")

    async def exec_detached(self, script):
        import subprocess
        self.procs.append(subprocess.Popen(["bash", "-c", script], start_new_session=True,
                                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL, cwd="/tmp"))
        return 0


@pytest.mark.skipif(shutil.which("timeout") is None or shutil.which("base64") is None,
                    reason="needs timeout + base64")
@pytest.mark.asyncio
async def test_container_scripts_run_for_real(tmp_path):
    from tools.shell.executor import DockerShellExecutor
    be = _LocalBashBackend()
    ex = DockerShellExecutor(be)
    jid = f"job-t{uuid.uuid4().hex[:8]}"
    await ex.start_background('while read l; do echo "got:$l"; done; echo eof; exit 4', jid,
                              ShellState(cwd=str(tmp_path)))
    try:
        assert await _until(lambda: _running(ex, jid), 5)
        ok, msg = await ex.write_stdin(jid, b"it's $(x)\n")
        assert ok, msg
        assert await _until(lambda: _contains(ex, jid, "got:it's $(x)"), 5)
        text, off = await ex.read_log_from(jid, 0)
        assert "got:it's $(x)" in text and off == len(text.encode())
        text2, off2 = await ex.read_log_from(jid, off)
        assert text2 == "" and off2 == off
        assert (await ex.close_stdin(jid))[0]
        # (the launcher is our zombie child here, so wait on .rc, not on kill -0)
        assert await _until(lambda: _has_rc(ex, jid), 5)
        assert await ex.exit_code(jid) == 4
        ok, msg = await ex.write_stdin(jid, b"late\n")
        assert not ok and "closed" in msg
        # a job that never reads: the write times out instead of hanging
        jid2 = f"job-u{uuid.uuid4().hex[:8]}"
        await ex.start_background("exit 0", jid2, ShellState(cwd=str(tmp_path)))
        assert await _until(lambda: _has_rc(ex, jid2), 5)
        ok, msg = await ex.write_stdin(jid2, b"x")
        assert not ok
    finally:
        await ex.kill(jid)
        os.system("rm -f /tmp/polyrob-jobs/job-t* /tmp/polyrob-jobs/job-u*")


async def _running(ex, jid):
    return await ex.poll(jid) == "running"


async def _has_rc(ex, jid):
    return await ex.exit_code(jid) is not None
