"""077 — the lifecycle record (core/lifecycle.py)."""
import json
import os
import time

import pytest

from core import lifecycle as lc


@pytest.fixture
def home(tmp_path):
    return tmp_path / "data"


def test_one_open_request_at_a_time(home):
    req, created = lc.request_restart(requested_by="owner", origin={"surface": "telegram"},
                                      data_home=home)
    assert created and req.state == lc.STATE_OPEN and req.kind == lc.KIND_RESTART
    again, created2 = lc.request_restart(requested_by="cli", data_home=home)
    assert not created2 and again.id == req.id, "a second ask must not queue a second restart"


def test_unknown_kind_is_refused(home):
    with pytest.raises(ValueError):
        lc.request_restart(kind="reboot", requested_by="x", data_home=home)


def test_claim_moves_to_draining_once(home):
    lc.request_restart(requested_by="owner", data_home=home)
    first = lc.claim_request(data_home=home)
    assert first is not None and first.state == lc.STATE_DRAINING
    assert first.claimed_by == os.getpid()
    # Another live process may not take it.
    assert lc.claim_request(pid=os.getpid() + 1, data_home=home) is None


def test_a_dead_claimer_releases_the_request(home):
    lc.request_restart(requested_by="owner", data_home=home)
    lc.claim_request(pid=999_999_999, data_home=home)  # a pid that does not exist
    again = lc.claim_request(data_home=home)
    assert again is not None and again.claimed_by == os.getpid()


def test_an_exiting_request_is_left_for_the_next_boot(home):
    lc.request_restart(requested_by="owner", data_home=home)
    lc.claim_request(data_home=home)
    lc.set_request_state(lc.STATE_EXITING, data_home=home)
    assert lc.claim_request(pid=999_999_999, data_home=home) is None


def test_a_secondary_process_takes_only_its_own_surface(home):
    lc.request_restart(requested_by="owner", origin={"surface": "telegram"}, data_home=home)
    assert lc.claim_request(data_home=home, serves={"email"}, primary=False) is None
    assert lc.claim_request(data_home=home, serves={"telegram"}, primary=False) is not None


def test_close_appends_history_and_removes_the_request(home):
    req, _ = lc.request_restart(requested_by="owner", data_home=home)
    lc.close_request(lc.OUTCOME_BACK_ONLINE, "back", data_home=home, version="1.0")
    assert lc.read_request(data_home=home) is None
    last = lc.history(limit=1, data_home=home)[0]
    assert last["outcome"] == lc.OUTCOME_BACK_ONLINE and last["request"]["id"] == req.id


def test_an_unreadable_request_is_said_not_hidden(home):
    d = lc.lifecycle_dir(home)
    d.mkdir(parents=True)
    (d / "request.json").write_text("{not json")
    with pytest.raises(lc.LifecycleUnreadable):
        lc.read_request(data_home=home)


def test_boot_attempts_count_unhealthy_starts(home):
    assert lc.record_boot(data_home=home)["attempts"] == 1
    assert lc.record_boot(data_home=home)["attempts"] == 2
    lc.mark_healthy(data_home=home)
    assert lc.record_boot(data_home=home)["attempts"] == 1
    lc.reset_boot_attempts(data_home=home)
    assert lc.record_boot(data_home=home)["attempts"] == 1


def test_pending_keys_written_after_the_running_boot(home):
    lc.record_pending("OLD_KEY", data_home=home)
    time.sleep(0.01)
    lc.record_boot(data_home=home)  # this (live) process started after OLD_KEY
    time.sleep(0.01)
    lc.record_pending("NEW_KEY", "global", data_home=home)
    assert lc.pending_keys(data_home=home) == ["NEW_KEY"]


def test_pending_keys_all_wait_when_nothing_runs(home):
    lc.record_pending("A", data_home=home)
    d = lc.lifecycle_dir(home)
    (d / "boot.json").write_text(json.dumps({"pid": 999_999_999, "started_at": time.time()}))
    assert lc.pending_keys(data_home=home) == ["A"]


def test_supervisor_detection(monkeypatch):
    for k in ("INVOCATION_ID", "XPC_SERVICE_NAME"):
        monkeypatch.delenv(k, raising=False)
    assert lc.supervisor() is None
    monkeypatch.setenv("XPC_SERVICE_NAME", "application.com.apple.Terminal.123")
    assert lc.supervisor() is None, "a terminal's launchd name is not our job"
    monkeypatch.setenv("XPC_SERVICE_NAME", lc.LAUNCHD_LABEL)
    assert lc.supervisor() == "launchd"
    monkeypatch.setenv("INVOCATION_ID", "abc")
    monkeypatch.setattr(lc, "_in_service_cgroup", lambda *a, **k: True)  # a pytest shell is a .scope
    assert lc.supervisor() == "systemd"


def test_draining_flag_round_trip():
    assert not lc.draining()
    lc.begin_drain()
    try:
        assert lc.draining()
    finally:
        lc.end_drain()
    assert not lc.draining()


def test_cron_tick_starts_nothing_while_draining(tmp_path):
    import asyncio

    from cron import scheduler as sched_mod

    class _Store:
        def due(self, now):  # pragma: no cover — must not be reached
            raise AssertionError("a draining tick must not look for due jobs")

    sch = sched_mod.CronScheduler.__new__(sched_mod.CronScheduler)
    sch.lock_path = str(tmp_path / "tick.lock")
    sch.store = _Store()
    lc.begin_drain()
    try:
        res = asyncio.run(sch.tick())
    finally:
        lc.end_drain()
    assert res.skipped_busy and not res.ran


def test_a_terminal_shell_with_a_leaked_invocation_id_is_not_supervised(tmp_path):
    cg = tmp_path / "cgroup"
    cg.write_text("0::/user.slice/user-1000.slice/user@1000.service/app.slice/"
                  "vte-spawn-1234.scope\n")
    assert not lc._in_service_cgroup(str(cg))
    cg.write_text("0::/system.slice/polyrob.service\n")
    assert lc._in_service_cgroup(str(cg))
    assert lc._in_service_cgroup(str(tmp_path / "absent"))


def test_a_withdrawal_closes_under_its_own_lock(home):
    req, _ = lc.request_restart(requested_by="cli", data_home=home)
    assert lc.withdraw_if_open(req.id, "x", data_home=home)
    assert lc.read_request(data_home=home) is None


# ---- 098 CO-02 ---------------------------------------------------------------

def test_set_request_state_refuses_other_request_id(tmp_path):
    home = tmp_path / "data"
    req, _ = lc.request_restart(requested_by="owner", data_home=home)
    lc.claim_request(data_home=home)
    assert lc.set_request_state(lc.STATE_EXITING, data_home=home, request_id="other") is None
    assert lc.set_request_state(lc.STATE_EXITING, data_home=home, request_id=req.id,
                                expect=(lc.STATE_OPEN,)) is None
    assert lc.set_request_state(lc.STATE_EXITING, data_home=home, request_id=req.id,
                                expect=(lc.STATE_DRAINING,)).state == lc.STATE_EXITING


def test_set_request_state_refuses_illegal_transition(tmp_path):
    import pytest
    from core.state.machine import IllegalTransition
    home = tmp_path / "data"
    lc.request_restart(requested_by="owner", data_home=home)
    lc.claim_request(data_home=home)
    lc.set_request_state(lc.STATE_EXITING, data_home=home)
    with pytest.raises(IllegalTransition):
        lc.set_request_state(lc.STATE_OPEN, data_home=home)


# ---- 098 CO-03: the drain is a set of holders -----------------------------------

def test_end_drain_of_restart_does_not_clear_stop_drain():
    lc.end_drain()
    restart = lc.begin_drain("restart")
    lc.begin_drain("stop")
    lc.end_drain(restart)
    try:
        assert lc.draining()
    finally:
        lc.end_drain()
    assert not lc.draining()


def test_end_drain_unknown_token_is_noop():
    lc.end_drain()
    t = lc.begin_drain("restart")
    lc.end_drain("nope")
    assert lc.draining()
    lc.end_drain(t)
    assert not lc.draining()


# ---- 098 CO-04: the boot record names a process, not a pid ----------------------

def test_running_service_ignores_reused_pid(tmp_path, monkeypatch):
    import json
    home = tmp_path / "data"
    monkeypatch.setattr(lc, "supervisor", lambda: "systemd")
    lc.record_boot(data_home=home)
    assert lc.running_service(data_home=home) is not None
    p = lc.lifecycle_dir(home) / "boot.json"
    rec = json.loads(p.read_text())
    pid, _start, boot = rec["ident"].split("|", 2)
    rec["ident"] = f"{pid}|an-older-start|{boot}"   # the pid now belongs to someone else
    p.write_text(json.dumps(rec))
    assert lc.running_service(data_home=home) is None


def test_reset_boot_attempts_from_other_process_is_noop(tmp_path):
    import json
    home = tmp_path / "data"
    lc.record_boot(data_home=home)
    p = lc.lifecycle_dir(home) / "boot.json"
    rec = json.loads(p.read_text())
    rec["attempts"] = 2
    rec["ident"] = rec["ident"].split("|", 1)[0] + "|other|boot"
    p.write_text(json.dumps(rec))
    lc.reset_boot_attempts(data_home=home)
    assert json.loads(p.read_text())["attempts"] == 2
