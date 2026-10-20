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
