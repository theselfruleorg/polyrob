"""098 TS-09: the shell backend pool — per-session lock, jobs die with the session."""
import asyncio
from types import SimpleNamespace

import pytest

from tools.shell import backend_pool as bp
from tools.shell import receipts as rc_mod
from tools.shell import process_registry as pr


class _Backend:
    def __init__(self, sid, gate=None, log=None):
        self._session_id = sid
        self.capabilities = {"sandbox": True}
        self.gate, self.log = gate, log if log is not None else []

    async def exec_detached(self, script):
        return 0

    async def setup(self):
        self.log.append(("setup", self._session_id))
        if self.gate is not None:
            await self.gate.wait()

    async def teardown(self):
        self.log.append(("teardown", self._session_id))

    async def published_ports(self):
        return {}


@pytest.fixture
def pool(monkeypatch, tmp_path):
    log, gates = [], {}
    monkeypatch.setattr(bp, "_POOL", {})
    monkeypatch.setattr(bp, "_SESSION_LOCKS", {})
    monkeypatch.setattr(bp, "_CLOSED", set())
    monkeypatch.setattr(bp, "_GEN", {})
    monkeypatch.setattr(pr, "_REGISTRY", pr.ProcessRegistry())
    monkeypatch.setattr(rc_mod, "receipts_dir", lambda sid, uid: tmp_path / "r" / sid)
    import tools.code_exec as ce
    monkeypatch.setattr(ce, "resolve_backend",
                        lambda session_id, dev_mode: _Backend(session_id, gates.get(session_id), log))
    return SimpleNamespace(log=log, gates=gates, tmp=tmp_path)


def test_teardown_marks_running_jobs_killed(pool):
    async def go():
        reg = pr.get_process_registry()
        job = reg.create("s1", "server", now=0)
        await bp.get_shell_backend("s1")
        await bp.teardown_session("s1")
        assert job.status == "killed"
        rec = rc_mod.load_receipt("s1", None, job.id)
        assert rec["status"] == "killed" and rec["note"] == "session ended"
    asyncio.run(go())


def test_poll_after_teardown_does_not_create_container(pool):
    async def go():
        from tools.shell.process_tool import ProcessJobParams, ProcessTool
        reg = pr.get_process_registry()
        job = reg.create("s1", "server", now=0)
        await bp.get_shell_backend("s1")
        await bp.teardown_session("s1")
        before = [e for e in pool.log if e[0] == "setup"]
        tool = ProcessTool.__new__(ProcessTool)
        tool._registry = reg
        tool._allowed = lambda ctx: True
        ctx = SimpleNamespace(session_id="s1", user_id=None)
        res = await tool.process_poll(ProcessJobParams(job_id=job.id), execution_context=ctx)
        assert "killed" in (res.extracted_content or "")
        assert [e for e in pool.log if e[0] == "setup"] == before
    asyncio.run(go())


def test_slow_setup_does_not_block_other_session(pool):
    async def go():
        pool.gates["slow"] = asyncio.Event()
        t = asyncio.create_task(bp.get_shell_backend("slow"))
        await asyncio.sleep(0.01)
        b = await asyncio.wait_for(bp.get_shell_backend("fast"), 1.0)
        assert b._session_id == "fast"
        pool.gates["slow"].set()
        await t
    asyncio.run(go())


def test_teardown_during_setup_leaves_no_pool_entry(pool):
    async def go():
        pool.gates["s1"] = asyncio.Event()
        t = asyncio.create_task(bp.get_shell_backend("s1"))
        await asyncio.sleep(0.01)
        td = asyncio.create_task(bp.teardown_session("s1"))
        await asyncio.sleep(0.01)
        pool.gates["s1"].set()
        await t
        await td
        assert "s1" not in bp._POOL
        assert ("teardown", "s1") in pool.log
    asyncio.run(go())


def test_late_call_after_close_starts_no_sandbox(pool):
    """098 TS-09: a closed session refuses a new sandbox until it resumes."""
    async def go():
        await bp.get_shell_backend("s1")
        await bp.teardown_session("s1")
        assert bp.is_session_closed("s1")
        with pytest.raises(bp.ShellBackendUnavailable, match="closed"):
            await bp.get_shell_backend("s1")
        with pytest.raises(bp.ShellBackendUnavailable, match="closed"):
            await bp.resolve_shell_executor(SimpleNamespace(session_id="s1", user_id=None))
        assert [e for e in pool.log if e[0] == "setup"] == [("setup", "s1")]
    asyncio.run(go())


def test_resume_clears_marker_and_gets_fresh_sandbox(pool):
    async def go():
        first = await bp.get_shell_backend("s1")
        await bp.teardown_session("s1")
        bp.reopen_session("s1")
        assert not bp.is_session_closed("s1")
        second = await bp.get_shell_backend("s1")
        assert second is not first
        assert [e for e in pool.log if e[0] == "setup"] == [("setup", "s1"), ("setup", "s1")]
    asyncio.run(go())


def test_orchestrator_reopen_hook_clears_marker(pool):
    from agents.task.session.cleanup import SessionCleanupMixin

    class _Orch(SessionCleanupMixin):
        session_id = "s1"

    async def go():
        await bp.get_shell_backend("s1")
        await _Orch().release_shell_sandbox()
        assert bp.is_session_closed("s1")
        _Orch().reopen_shell_sandbox()
        assert not bp.is_session_closed("s1")
    asyncio.run(go())


def test_a_finished_runs_release_never_closes_the_next_run(pool):
    """098 review: run A ends, run B takes the session and reopens it, and only
    then does A's release run — it must not tear down B's sandbox or leave B
    refused with "session closed" for the rest of its run."""
    from agents.task.session.cleanup import SessionCleanupMixin

    class _Orch(SessionCleanupMixin):
        session_id = "s1"

    async def go():
        orch = _Orch()
        orch.reopen_shell_sandbox()                     # run A starts
        gen_a = orch.shell_sandbox_generation()
        await bp.get_shell_backend("s1")
        orch.reopen_shell_sandbox()                     # run B starts
        b_backend = await bp.get_shell_backend("s1")
        await orch.release_shell_sandbox(generation=gen_a)  # A's late release
        assert not bp.is_session_closed("s1")
        assert bp.peek_backend("s1") is b_backend
        await orch.release_shell_sandbox(generation=orch.shell_sandbox_generation())
        assert bp.is_session_closed("s1")
    asyncio.run(go())
