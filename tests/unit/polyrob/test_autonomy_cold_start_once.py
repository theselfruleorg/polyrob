"""098 CL-03/CL-04/CO-32: cold-start sweeps and boot one-shots run once per
process, and a once-flag sets only after the work was scheduled."""
import asyncio

import pytest

import polyrob.app.autonomy_runtime as ar
from cli.ui.autonomy_live import LIVE_AUTONOMY_MACHINE, LiveAutonomy, autonomy_switch
from core.state.machine import IllegalTransition


@pytest.fixture(autouse=True)
def _reset():
    ar.reset_cold_start_latches()
    yield
    ar.reset_cold_start_latches()


def test_restart_does_not_rerun_sweeps(tmp_path):
    calls = []
    fns = {"orphan_reap": lambda: calls.append("orphan"),
           "delegation_recovery": lambda: calls.append("deleg"),
           "hf_deploy_reconcile": lambda: calls.append("hf")}
    for _ in range(3):  # boot, /autonomy off+on, again
        for name, fn in fns.items():
            ar._cold_start_once(name, str(tmp_path), fn)
    assert sorted(calls) == ["deleg", "hf", "orphan"]
    # another data home in the same process is its own cold start
    ar._cold_start_once("delegation_recovery", str(tmp_path / "other"), fns["delegation_recovery"])
    assert calls.count("deleg") == 2


def test_sweep_latch_set_only_after_schedule(tmp_path):
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("no running loop")

    with pytest.raises(RuntimeError):
        ar._cold_start_once("delegation_recovery", str(tmp_path), flaky)
    ar._cold_start_once("delegation_recovery", str(tmp_path), flaky)
    ar._cold_start_once("delegation_recovery", str(tmp_path), flaky)
    assert len(attempts) == 2


def test_start_autonomy_twice_schedules_recovery_once(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(ar, "_schedule_delegation_recovery",
                        lambda ta, dd=None: seen.append(dd))
    monkeypatch.setattr(ar, "_schedule_cold_start_orphan_reap", lambda: None)
    monkeypatch.setattr(ar, "_schedule_hf_deploy_reconcile", lambda: None)

    async def go():
        for _ in range(2):
            h = ar.start_autonomy(task_agent=object(), data_dir=str(tmp_path))
            await h.stop()

    asyncio.run(go())
    assert len(seen) == 1


def test_no_container_does_not_latch(monkeypatch):
    monkeypatch.setattr(ar, "_boot_migrations_scheduled", False)
    scheduled = []

    async def go():
        ar._schedule_boot_migrations(object())              # a stub: no container
        assert ar._boot_migrations_scheduled is False
        monkeypatch.setattr("migrations.boot.run_boot_migrations",
                            lambda c, local=True: _done(scheduled))
        ar._schedule_boot_migrations(type("A", (), {"container": object()})())
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(go())
    assert ar._boot_migrations_scheduled is True and scheduled == [1]


async def _done(out):
    out.append(1)
    return {}


def test_create_task_failure_does_not_latch(monkeypatch):
    monkeypatch.setattr(ar, "_self_binding_sweep_scheduled", False)
    with pytest.raises(RuntimeError):            # no running loop: create_task raises
        ar._schedule_self_binding_sweep(object())
    assert ar._self_binding_sweep_scheduled is False


def test_unresolvable_home_retries_next_call(monkeypatch):
    import core.runtime_paths as rp
    import core.sidecar_relocate as sr
    monkeypatch.setattr(sr, "_DONE", False)
    monkeypatch.setattr(rp, "resolve_data_home",
                        lambda: (_ for _ in ()).throw(RuntimeError("not yet")))
    assert sr.relocate_legacy_sidecars() == []
    assert sr._DONE is False


def test_failed_restart_reports_failed_state(monkeypatch):
    import cli.ui.autonomy_live as al

    class _Res:
        ok, message = True, "AUTONOMY_ENABLED=true"

    monkeypatch.setattr("core.config_service.set_value", lambda *a, **k: _Res())

    def boom():
        raise RuntimeError("tickers broke")

    live = LiveAutonomy(boom)
    al.bind(live)
    try:
        text = asyncio.run(autonomy_switch(True))
    finally:
        al.bind(None)
    assert live.state == "failed"
    assert "failed to start (RuntimeError: tickers broke)" in text
    assert "/autonomy on retries" in text


def test_illegal_transition_running_to_starting():
    assert LIVE_AUTONOMY_MACHINE.validate() == []
    with pytest.raises(IllegalTransition):
        LIVE_AUTONOMY_MACHINE.check("running", "starting")
