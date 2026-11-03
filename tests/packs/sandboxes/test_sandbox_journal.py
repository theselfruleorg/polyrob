"""098 CL-15: a provisioned cloud sandbox is always named by a record."""
import sys

import pytest

from tools.code_exec.backend import ExecutionBackendError

from tests.packs.sandboxes.test_sandbox_packs import (  # noqa: F401 (fixture)
    _data_home, _fake_daytona, _fake_modal)


def _daytona(monkeypatch, log, **kw):
    sdk = _fake_daytona(log, **kw)
    monkeypatch.setitem(sys.modules, "daytona", sdk)
    monkeypatch.setenv("DAYTONA_API_KEY", "k")
    return sdk


@pytest.mark.asyncio
async def test_mkdir_failure_deletes_ephemeral_sandbox(monkeypatch):
    log = []
    sdk = _daytona(monkeypatch, log)
    real_create = sdk.Daytona.create

    def create(self, params):
        sb = real_create(self, params)
        sb.process.exec = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("mkdir boom"))
        return sb
    monkeypatch.setattr(sdk.Daytona, "create", create)
    from polyrob_daytona.backend import DaytonaBackend
    b = DaytonaBackend(session_id="s9", dev_mode=True)
    with pytest.raises(Exception):
        await b.setup()
    assert ("delete",) in log and b.load_state() == {}


@pytest.mark.asyncio
async def test_transient_get_error_does_not_create_second_sandbox(monkeypatch):
    log = []
    sdk = _daytona(monkeypatch, log)
    monkeypatch.setenv("DAYTONA_SANDBOX_PERSIST", "1")
    from polyrob_daytona.backend import DaytonaBackend
    b = DaytonaBackend(session_id="s8", dev_mode=True)
    b.save_state({"sandbox_id": "sb-old"})

    def get(self, sid):
        raise TimeoutError("read timed out")
    monkeypatch.setattr(sdk.Daytona, "get", get)
    with pytest.raises(ExecutionBackendError):
        await b.setup()
    assert not [e for e in log if e[0] == "create"]
    assert b.load_state()["sandbox_id"] == "sb-old"

    def gone(self, sid):
        raise RuntimeError("Sandbox not found")
    monkeypatch.setattr(sdk.Daytona, "get", gone)
    await b.setup()  # a typed not-found: create a new one
    assert [e for e in log if e[0] == "create"]


@pytest.mark.asyncio
async def test_dead_owner_ephemeral_reaped_on_next_setup(monkeypatch):
    log = []
    _daytona(monkeypatch, log)
    from polyrob_daytona.backend import DaytonaBackend
    b = DaytonaBackend(session_id="s7", dev_mode=True)
    b.save_state({"phase": "created", "sandbox_id": "sb-orphan", "mode": "ephemeral",
                  "owner": "999999|never|boot-x"})
    await b.setup()
    assert ("get", "sb-orphan") in log and log.index(("delete",)) < [
        i for i, e in enumerate(log) if e[0] == "create"][0]
    await b.teardown()


@pytest.mark.asyncio
async def test_old_state_file_reads_as_persist(monkeypatch):
    log = []
    _daytona(monkeypatch, log)
    monkeypatch.setenv("DAYTONA_SANDBOX_PERSIST", "1")
    from polyrob_daytona.backend import DaytonaBackend
    b = DaytonaBackend(session_id="s6", dev_mode=True)
    b.save_state({"sandbox_id": "sb-1"})
    await b.setup()
    assert ("get", "sb-1") in log and not [e for e in log if e[0] == "create"]
    assert b.load_state()["mode"] == "persist"


@pytest.mark.asyncio
async def test_exec_failure_terminates_modal_sandbox(monkeypatch):
    log = []
    modal = _fake_modal(log)

    def bad_exec(self, *a, **k):
        raise RuntimeError("exec boom")
    monkeypatch.setattr(modal.Sandbox, "exec", bad_exec)
    monkeypatch.setitem(sys.modules, "modal", modal)
    from polyrob_modal.backend import ModalBackend
    with pytest.raises(Exception):
        await ModalBackend(session_id="m1", dev_mode=True).setup()
    assert ("terminate",) in log
