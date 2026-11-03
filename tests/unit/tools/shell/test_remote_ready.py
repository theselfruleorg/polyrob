"""098 TS-12: the remote executor's prepared far side is a lifecycle."""
import asyncio

from tools.shell.remote_executor import RemoteShellExecutor


class _Transport:
    name = "fake"
    capabilities = {"sandbox": True}

    def __init__(self, kind_rc=0, fail_after=None):
        self.setups = 0
        self.rc = kind_rc
        self.raise_next = False

    async def setup(self):
        self.setups += 1

    async def run_script(self, script, *, timeout):
        if script.startswith("mkdir -p"):
            return 0, "/far/jobs\n", "", False
        if self.raise_next:
            self.raise_next = False
            raise RuntimeError("sandbox was recycled")
        return self.rc, "RUNNING\n" if self.rc == 0 else "", "", False

    async def close(self):
        pass


def _ex(t, kind="pack"):
    return RemoteShellExecutor(t, kind=kind, default_cwd="/far", jobs_dir="jobs")


def test_backend_transport_resetup_after_failure():
    async def go():
        t = _Transport()
        ex = _ex(t)
        assert await ex.poll("job-1") == "running"
        assert t.setups == 1
        t.raise_next = True
        try:
            await ex.poll("job-1")
        except RuntimeError:
            pass
        assert not ex._ready
        assert await ex.poll("job-1") == "running"
        assert t.setups == 2  # setup() ran again for the recycled sandbox
    asyncio.run(go())


def test_ssh_255_marks_degraded_not_done():
    async def go():
        t = _Transport(kind_rc=255)
        ex = _ex(t, kind="ssh")
        assert await ex.poll("job-1") == "unknown"  # never "done"
        assert not ex._ready
        t.rc = 0
        assert await ex.poll("job-1") == "running"
        assert t.setups == 2
    asyncio.run(go())
