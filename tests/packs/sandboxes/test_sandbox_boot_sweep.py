"""098 CL-15: the boot sweep deletes a dead owner's ephemeral cloud sandbox,
stops (never deletes) a persistent one, and leaves live or unowned records."""
import json
import sys
import types

import pytest

from core.state.proc import ProcIdentity, boot_id, process_identity
from tools.code_exec.backends import sandbox_sweep

from tests.packs.sandboxes.test_sandbox_packs import (  # noqa: F401 (fixture)
    _data_home, _fake_daytona, _fake_modal)

DEAD = ProcIdentity(2 ** 22 + 12345, "never", boot_id()).encode()


def _put(tmp_path, provider, slug, record):
    d = tmp_path / "data" / "sandbox_state" / provider
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{slug}.json").write_text(json.dumps(record))
    return d / f"{slug}.json"


def _daytona(monkeypatch, log):
    monkeypatch.setitem(sys.modules, "daytona", _fake_daytona(log))
    monkeypatch.setenv("DAYTONA_API_KEY", "k")
    from polyrob_daytona.backend import DaytonaBackend
    return lambda provider: DaytonaBackend() if provider == "daytona" else None


async def _sweep(tmp_path, backend_for):
    return await sandbox_sweep.sweep_orphan_sandboxes(
        data_home=str(tmp_path / "data"), backend_for=backend_for)


@pytest.mark.asyncio
async def test_dead_owner_ephemeral_is_deleted(monkeypatch, tmp_path):
    log = []
    path = _put(tmp_path, "daytona", "a", {"phase": "created", "sandbox_id": "sb-1",
                                          "mode": "ephemeral", "owner": DEAD})
    report = await _sweep(tmp_path, _daytona(monkeypatch, log))
    assert report["deleted"] == ["daytona:sb-1"]
    assert ("delete",) in log and not path.exists()


@pytest.mark.asyncio
async def test_dead_owner_persistent_is_stopped_not_deleted(monkeypatch, tmp_path):
    log = []
    path = _put(tmp_path, "daytona", "a", {"phase": "created", "sandbox_id": "sb-1",
                                          "mode": "persist", "owner": DEAD})
    report = await _sweep(tmp_path, _daytona(monkeypatch, log))
    assert report["stopped"] == ["daytona:sb-1"]
    assert ("stop",) in log and ("delete",) not in log
    assert json.loads(path.read_text())["phase"] == "stopped"
    # a second boot has nothing left to do
    log.clear()
    assert not any((await _sweep(tmp_path, _daytona(monkeypatch, log))).values())
    assert log == []


@pytest.mark.asyncio
async def test_live_or_unowned_records_are_left_alone(monkeypatch, tmp_path):
    log = []
    _put(tmp_path, "daytona", "live", {"phase": "created", "sandbox_id": "sb-1",
                                       "mode": "ephemeral",
                                       "owner": process_identity().encode()})
    _put(tmp_path, "daytona", "legacy", {"sandbox_id": "sb-2"})
    report = await _sweep(tmp_path, _daytona(monkeypatch, log))
    assert not any(report.values()) and log == []


@pytest.mark.asyncio
async def test_provider_not_found_drops_the_record(monkeypatch, tmp_path):
    log = []
    backend_for = _daytona(monkeypatch, log)
    sdk = sys.modules["daytona"]

    def gone(self, sid):
        raise RuntimeError("Sandbox not found")
    monkeypatch.setattr(sdk.Daytona, "get", gone)
    path = _put(tmp_path, "daytona", "a", {"sandbox_id": "sb-1", "mode": "ephemeral",
                                          "owner": DEAD})
    assert (await _sweep(tmp_path, backend_for))["gone"] == ["daytona:sb-1"]
    assert not path.exists()


@pytest.mark.asyncio
async def test_errors_and_unknown_providers_are_reported_not_raised(monkeypatch, tmp_path):
    log = []
    backend_for = _daytona(monkeypatch, log)
    sdk = sys.modules["daytona"]

    def boom(self, sid):
        raise TimeoutError("read timed out")
    monkeypatch.setattr(sdk.Daytona, "get", boom)
    kept = _put(tmp_path, "daytona", "a", {"sandbox_id": "sb-1", "mode": "ephemeral",
                                          "owner": DEAD})
    _put(tmp_path, "nopack", "b", {"sandbox_id": "x-1", "mode": "ephemeral", "owner": DEAD})
    report = await _sweep(tmp_path, backend_for)
    assert report["errors"] == ["daytona:sb-1"] and report["skipped"] == ["nopack:x-1"]
    assert kept.exists()  # a failed reap keeps the record for the next boot


def _modal(monkeypatch, log):
    modal = _fake_modal(log)

    class _Sb(modal.Sandbox):
        object_id = "sb-m1"

    def from_id(sid, **kw):
        log.append(("from_id", sid))
        return _Sb()
    modal.Sandbox.from_id = staticmethod(from_id)
    monkeypatch.setitem(sys.modules, "modal", modal)
    from polyrob_modal.backend import ModalBackend
    return lambda provider: ModalBackend() if provider == "modal" else None


@pytest.mark.asyncio
async def test_modal_ephemeral_terminated_persistent_snapshotted(monkeypatch, tmp_path):
    log = []
    backend_for = _modal(monkeypatch, log)
    eph = _put(tmp_path, "modal", "a", {"sandbox_id": "sb-m1", "mode": "ephemeral",
                                       "owner": DEAD})
    per = _put(tmp_path, "modal", "b", {"sandbox_id": "sb-m2", "mode": "persist",
                                       "owner": DEAD, "image_id": "im-old"})
    report = await _sweep(tmp_path, backend_for)
    assert report["deleted"] == ["modal:sb-m1"] and report["stopped"] == ["modal:sb-m2"]
    assert not eph.exists()
    assert json.loads(per.read_text()) == {"image_id": "im-123"}
    assert log.count(("terminate",)) == 2 and ("snapshot",) in log


@pytest.mark.asyncio
async def test_modal_records_the_sandbox_and_forgets_it_on_teardown(monkeypatch, tmp_path):
    log = []
    _modal(monkeypatch, log)
    modal = sys.modules["modal"]
    monkeypatch.setattr(modal.Sandbox, "object_id", "sb-new", raising=False)
    from polyrob_modal.backend import ModalBackend
    b = ModalBackend(session_id="s1", dev_mode=True)
    await b.setup()
    rec = b.load_state()
    assert rec["sandbox_id"] == "sb-new" and rec["mode"] == "ephemeral"
    assert ProcIdentity.decode(rec["owner"]) == process_identity()
    await b.teardown()
    assert b.load_state() == {}


@pytest.mark.asyncio
async def test_no_state_dir_schedules_nothing(tmp_path):
    sandbox_sweep.schedule_boot_sweep(str(tmp_path / "nothing"))
    assert not sandbox_sweep._TASKS


def test_modal_snapshot_mode_needs_a_persistent_session(monkeypatch):
    """098 review: the record's mode follows the SAME rule as the release — an
    ephemeral sandbox is never recorded `persist` (the sweep would snapshot it)."""
    log = []
    _modal(monkeypatch, log)
    from polyrob_modal.backend import ModalBackend
    monkeypatch.setenv("MODAL_SANDBOX_SNAPSHOT", "true")
    assert ModalBackend()._snapshot_mode() is False
    assert ModalBackend(session_id="s1", dev_mode=True)._snapshot_mode() is True


@pytest.mark.asyncio
async def test_a_record_resumed_during_the_reap_is_not_overwritten(tmp_path):
    """098 review O-05: a live session resumed the sandbox while the sweep's
    reap ran; the sweep keeps the new record instead of writing back its old
    read with the dead owner."""
    path = _put(tmp_path, "fake", "a", {"phase": "created", "sandbox_id": "sb-1",
                                       "mode": "persist", "owner": DEAD})
    live = {"phase": "created", "sandbox_id": "sb-1", "mode": "persist",
            "owner": process_identity().encode()}

    class _Backend:
        def reap_orphan(self, record):
            path.write_text(json.dumps(live))       # the resume lands mid-reap
            return "stopped", dict(record, phase="stopped")

    report = await sandbox_sweep.sweep_orphan_sandboxes(
        data_home=str(tmp_path / "data"), backend_for=lambda provider: _Backend())
    assert report["skipped"] == ["fake:sb-1"]
    assert json.loads(path.read_text()) == live
