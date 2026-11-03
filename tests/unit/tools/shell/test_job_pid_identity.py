"""098 TS-10: a background job's pid is stored with its start time — a reused
pid reads as gone, and ``PermissionError`` (a sudo/root job) means running only
when the start time matches or was never recorded."""
import os
import shlex
import subprocess
import sys

import pytest

import core.state.proc as proc
from tools.shell import jobs
from tools.shell.host_executor import HostShellExecutor
from tools.shell.state import ShellState

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX shell")


def _ex(tmp_path):
    ex = HostShellExecutor(workspace_dir=str(tmp_path), jobs_dir=tmp_path / "jobs")
    ex._prepare_jobs_dir()
    return ex


def _orphan(ex, job_id, pid, start=None):
    """A job this executor did not launch (after a restart): only files remain."""
    ex._paths(job_id)[0].write_text(str(pid))
    if start is not None:
        ex._start_file(job_id).write_text(start)


@pytest.mark.asyncio
async def test_launch_records_start_time(tmp_path):
    ex = _ex(tmp_path)
    await ex.start_background("sleep 5", "j1", ShellState(cwd=str(tmp_path)))
    try:
        pid = ex._pid("j1")
        assert ex._recorded_start("j1") == proc.start_time(pid)
    finally:
        await ex.kill("j1")


@pytest.mark.asyncio
async def test_reused_pid_reads_done(tmp_path, monkeypatch):
    ex = _ex(tmp_path)
    _orphan(ex, "j", os.getpid(), start="then")
    monkeypatch.setattr(proc, "start_time", lambda pid: "now")
    assert await ex.poll("j") == "done"


@pytest.mark.asyncio
async def test_same_start_reads_running(tmp_path, monkeypatch):
    ex = _ex(tmp_path)
    _orphan(ex, "j", os.getpid(), start="same")
    monkeypatch.setattr(proc, "start_time", lambda pid: "same")
    assert await ex.poll("j") == "running"


@pytest.mark.asyncio
@pytest.mark.parametrize("recorded, current, want", [
    ("same", "same", "running"),     # a root job, start matches
    (None, "same", "running"),       # a legacy job: start never recorded
    ("then", "now", "done"),         # a root daemon on a reused pid
])
async def test_permission_error_rule(tmp_path, monkeypatch, recorded, current, want):
    ex = _ex(tmp_path)
    _orphan(ex, "j", 4242, start=recorded)

    def _kill(pid, sig):
        raise PermissionError

    monkeypatch.setattr(os, "kill", _kill)
    monkeypatch.setattr(proc, "start_time", lambda pid: current)
    assert await ex.poll("j") == want


@pytest.mark.asyncio
async def test_kill_never_signals_a_reused_pid(tmp_path, monkeypatch):
    ex = _ex(tmp_path)
    _orphan(ex, "j", 4242, start="then")
    sent = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(os, "killpg", lambda pid, sig: sent.append(("pg", pid, sig)))
    monkeypatch.setattr(proc, "start_time", lambda pid: "now")
    assert await ex.kill("j") is False
    assert sent == []


@pytest.mark.parametrize("recorded, current, want", [
    ("111", "111", "RUNNING"), ("222", "111", "DONE"), ("", "111", "RUNNING"),
    ("111", "", "RUNNING"),
])
def test_shell_probe_reused_pid(tmp_path, monkeypatch, recorded, current, want):
    """The container/ssh probe text: alive + different start = DONE."""
    monkeypatch.setattr(jobs, "_PROC_START_SH", f"echo {current}" if current else "true")
    start = tmp_path / "j.start"
    if recorded:
        start.write_text(recorded + "\n")
    frag = jobs.pid_reused_sh(shlex.quote(str(start)))
    code = (f"pid=$$; if kill -0 \"$pid\" 2>/dev/null; then if {frag}; then echo DONE; "
            f"else echo RUNNING; fi; else echo DONE; fi")
    out = subprocess.run(["sh", "-c", code], capture_output=True, text=True).stdout.strip()
    assert out == want


def test_launcher_writes_start_file_where_proc_exists():
    text = jobs.launcher_script(shell="sh", files=jobs.job_files("/w", "a"), exports="")
    assert "/proc/$$/stat" in text and "> /w/a.start" in text
    assert "/w/a.start" in text.split("\n")[1]  # cleared on relaunch
