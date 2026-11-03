"""098 TS-10: background job states — unknown is a probe, never 'done'."""
import asyncio

import pytest

from core.state import machine
from tools.shell import receipts as rc_mod
from tools.shell.job_state import JOB, UNKNOWN_LIMIT
from tools.shell.process_registry import ProcessRegistry


@pytest.fixture
def receipts(monkeypatch, tmp_path):
    monkeypatch.setattr(rc_mod, "receipts_dir", lambda sid, uid: tmp_path / "receipts" / sid)
    return tmp_path / "receipts"


class _Exec:
    def __init__(self, probes):
        self.probes = list(probes)

    async def poll(self, job_id):
        return self.probes.pop(0) if self.probes else "running"

    async def exit_code(self, job_id):
        return None

    async def read_log(self, job_id, max_bytes=0):
        return ""


def test_table_is_well_formed():
    assert JOB.validate() == []


def test_unknown_probe_does_not_finalize(receipts):
    reg = ProcessRegistry()
    job = reg.create("s", "srv", now=0)
    assert reg.observe("s", job.id, "unknown", now=1) == "unknown"
    assert job.status == "running" and not receipts.exists()
    assert reg.observe("s", job.id, "running", now=2) == "running"
    assert job.unobserved == 0


def test_repeated_unknown_becomes_lost(receipts):
    async def go():
        from tools.shell.watch import JobWatch
        reg = ProcessRegistry()
        job = reg.create("s", "srv", now=0)
        w = JobWatch(executor=_Exec(["unknown"] * UNKNOWN_LIMIT), job=job, session_id="s",
                     user_id=None, registry=reg, notify=None, interval=0)
        await asyncio.wait_for(w.run(), 2)
        assert job.status == "lost"
        rec = rc_mod.load_receipt("s", None, job.id)
        assert rec["status"] == "lost" and "could not observe" in rec["note"]
        assert "could not observe" in rc_mod.receipt_status_line(rec)
    asyncio.run(go())


def test_done_to_running_is_illegal():
    reg = ProcessRegistry()
    job = reg.create("s", "x", now=0)
    reg.mark("s", job.id, "done", now=1)
    machine.set_strict(False)
    try:
        reg.mark("s", job.id, "running", now=2)
    finally:
        machine.set_strict(True)
    assert job.status == "done"
    reg.mark("s", job.id, "killed", now=3)  # a kill that raced the exit
    assert job.status == "killed"


def test_close_session_marks_running_jobs_killed():
    reg = ProcessRegistry()
    a = reg.create("s", "a", now=0)
    b = reg.create("s", "b", now=0)
    reg.mark("s", b.id, "done", now=1)
    closed = reg.close_session("s", now=2)
    assert [j.id for j in closed] == [a.id]
    assert a.status == "killed" and b.status == "done"


def test_ssh_transport_error_keeps_running(receipts):
    """A remote probe that could not observe (ssh 255 -> unknown) keeps the job."""
    reg = ProcessRegistry()
    job = reg.create("s", "srv", now=0, backend="remote")
    for i in range(UNKNOWN_LIMIT - 1):
        assert reg.observe("s", job.id, "unknown", now=i) == "unknown"
    assert job.status == "running"
