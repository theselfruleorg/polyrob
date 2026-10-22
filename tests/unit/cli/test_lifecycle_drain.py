"""077 — the drain (cli/commands/_lifecycle.py) and the boot step
(cli/commands/_lifecycle_boot.py), with a fake clock and fake busy signals."""
import asyncio

import pytest

from core import lifecycle as lc


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.t += s
        await asyncio.sleep(0)  # yield, as a real sleep does


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("INVOCATION_ID", "test-unit")
    monkeypatch.setattr(lc, "_in_service_cgroup", lambda *a, **k: True)  # a pytest shell is a .scope
    yield tmp_path / "data"
    lc.end_drain()


@pytest.fixture
def sent(monkeypatch):
    out = []

    async def _notify(container, req, text):
        out.append(text)
        return "sent"

    monkeypatch.setattr("cli.commands._lifecycle.notify", _notify)
    return out


def _busy_for(monkeypatch, polls):
    """The agent is busy for the first `polls` observations, then idle."""
    state = {"n": 0}

    def _idle(_home):
        state["n"] += 1
        return (state["n"] > polls, "a live human turn (owner_chat)")

    monkeypatch.setattr("cli.commands._lifecycle._is_idle", _idle)
    return state


def _watcher(home, clock, intake):
    from cli.commands._lifecycle import LifecycleWatcher
    exits = []
    w = LifecycleWatcher(data_home=home, container=object(),
                         stop_intake=lambda: intake.__setitem__("stopped", True),
                         intake_done=lambda: intake.get("stopped", False),
                         request_exit=lambda: exits.append(True),
                         clock=clock, sleep=clock.sleep)
    return w, exits


def test_drain_waits_for_the_live_turn_then_exits_75(home, monkeypatch, sent):
    clock, intake = _Clock(), {}
    _busy_for(monkeypatch, 3)
    lc.request_restart(requested_by="owner", origin={"surface": "telegram", "user_id": "u"},
                       data_home=home)
    w, exits = _watcher(home, clock, intake)
    asyncio.run(w.run())
    assert w.exit_code == lc.RESTART_EXIT_CODE and exits
    assert intake["stopped"], "intake stops only once the agent is idle"
    assert lc.read_request(data_home=home).state == lc.STATE_EXITING
    assert any("once the current work finishes" in t for t in sent)
    assert lc.draining(), "no new cron/goal work may start while it exits"


def test_intake_stays_on_while_the_turn_runs(home, monkeypatch, sent):
    clock, intake = _Clock(), {}
    seen = []

    def _idle(_home):
        seen.append(intake.get("stopped", False))
        return (len(seen) > 2, "busy")

    monkeypatch.setattr("cli.commands._lifecycle._is_idle", _idle)
    lc.request_restart(requested_by="owner", data_home=home)
    w, _ = _watcher(home, clock, intake)
    asyncio.run(w.run())
    assert seen[:3] == [False, False, False], "the owner is never cut off while it waits"


def test_drain_timeout_restarts_nothing_and_says_so(home, monkeypatch, sent):
    clock, intake = _Clock(), {}
    _busy_for(monkeypatch, 10_000)
    lc.request_restart(requested_by="owner", drain_timeout=30, data_home=home)
    w, exits = _watcher(home, clock, intake)

    async def _go():
        task = asyncio.ensure_future(w.run())
        while lc.read_request(data_home=home) is not None:
            await asyncio.sleep(0)
        task.cancel()

    asyncio.run(_go())
    assert w.exit_code is None and not exits and not intake
    assert lc.history(limit=1, data_home=home)[0]["outcome"] == lc.OUTCOME_DRAIN_TIMEOUT
    assert not lc.draining(), "autonomy resumes after a called-off drain"
    assert any("I did not restart" in t for t in sent)


def test_force_cuts_the_work_and_says_so(home, monkeypatch, sent):
    clock, intake = _Clock(), {}
    _busy_for(monkeypatch, 10_000)
    lc.request_restart(requested_by="owner", force=True, data_home=home)
    w, _ = _watcher(home, clock, intake)
    asyncio.run(w.run())
    assert w.exit_code == lc.RESTART_EXIT_CODE
    assert any("this cuts" in t for t in sent)


def test_no_supervisor_refuses(home, monkeypatch, sent):
    for k in ("INVOCATION_ID", "XPC_SERVICE_NAME"):
        monkeypatch.delenv(k, raising=False)
    clock, intake = _Clock(), {}
    lc.request_restart(requested_by="owner", data_home=home)
    w, exits = _watcher(home, clock, intake)

    async def _go():
        task = asyncio.ensure_future(w.run())
        while lc.read_request(data_home=home) is not None:
            await asyncio.sleep(0)
        task.cancel()

    asyncio.run(_go())
    assert not exits and lc.history(limit=1, data_home=home)[0]["outcome"] == lc.OUTCOME_CANCELLED
    assert any("nothing would start me again" in t for t in sent)


def test_healthy_boot_answers_the_request_once(home, sent):
    from cli.commands._lifecycle import announce_boot
    lc.request_restart(requested_by="owner", data_home=home)
    lc.claim_request(data_home=home)
    lc.set_request_state(lc.STATE_EXITING, data_home=home)
    lc.record_boot(data_home=home)
    text = asyncio.run(announce_boot(object(), home))
    assert text.startswith("✅ Back online on")
    assert asyncio.run(announce_boot(object(), home)) is None, "one message, not two"
    assert lc.history(limit=1, data_home=home)[0]["outcome"] == lc.OUTCOME_BACK_ONLINE
    assert lc.read_boot(data_home=home)["healthy_at"]


def test_boot_message_for_an_update(home, sent):
    from cli.commands._lifecycle import announce_boot
    lc.request_restart(kind=lc.KIND_UPDATE, requested_by="owner", data_home=home)
    lc.set_request_state(lc.STATE_EXITING, data_home=home,
                         update_result={"ok": True, "from_version": "1.3.0",
                                        "to_version": "1.4.0"})
    lc.record_boot(data_home=home)
    assert asyncio.run(announce_boot(object(), home)).startswith("✅ Updated and back online")
    assert lc.history(limit=1, data_home=home)[0]["outcome"] == lc.OUTCOME_UPDATED


def test_stop_drain_waits_then_reports(home, monkeypatch):
    from cli.commands._lifecycle import stop_drain
    clock, intake = _Clock(), {}
    _busy_for(monkeypatch, 4)
    done = asyncio.run(stop_drain(data_home=home,
                                  stop_intake=lambda: intake.__setitem__("stopped", True),
                                  intake_done=lambda: intake.get("stopped", False),
                                  clock=clock, sleep=clock.sleep))
    assert done and intake["stopped"]
    _busy_for(monkeypatch, 10_000)
    assert not asyncio.run(stop_drain(data_home=home, stop_intake=lambda: None,
                                      intake_done=lambda: True, timeout=5,
                                      clock=clock, sleep=clock.sleep))


# ---- the boot step ---------------------------------------------------------

def _exiting_update(home, **result):
    lc.request_restart(kind=lc.KIND_UPDATE, requested_by="owner", channel="stable",
                       data_home=home)
    lc.set_request_state(lc.STATE_EXITING, data_home=home,
                         **({"update_result": result} if result else {}))


def test_boot_applies_the_pending_update_and_execs(home):
    from cli.commands._lifecycle_boot import boot_step
    _exiting_update(home)
    calls = []
    did = boot_step("gateway", data_home=home,
                    apply=lambda ch: {"ok": True, "from_version": "1", "to_version": "2"},
                    reexec=lambda: calls.append("exec"), echo=lambda *_: None)
    assert did == "applied" and calls == ["exec"]
    assert lc.read_request(data_home=home).update_result["ok"]


def test_boot_starts_the_old_code_when_the_update_fails(home):
    from cli.commands._lifecycle_boot import boot_step
    _exiting_update(home)
    calls = []
    did = boot_step("gateway", data_home=home,
                    apply=lambda ch: {"ok": False, "failed_step": "verify", "rolled_back": True},
                    reexec=lambda: calls.append("exec"), echo=lambda *_: None)
    assert did == "not_applied" and not calls


def test_boot_passes_the_confirmed_target_version_to_the_apply(home):
    from cli.commands._lifecycle_boot import boot_step
    lc.request_restart(kind=lc.KIND_UPDATE, requested_by="owner", channel="stable",
                       target_version="v1.6.0", data_home=home)
    lc.set_request_state(lc.STATE_EXITING, data_home=home)
    seen = []
    boot_step("gateway", data_home=home,
              apply=lambda ch, **kw: seen.append((ch, kw)) or {"ok": False},
              reexec=lambda: None, echo=lambda *_: None)
    assert seen == [("stable", {"target_version": "v1.6.0"})]


def test_boot_rolls_back_new_code_that_never_starts(home):
    from cli.commands._lifecycle_boot import boot_step
    _exiting_update(home, ok=True, old_sha="abc", snapshot="s1")
    calls = []
    kw = dict(data_home=home, rollback=lambda res: (calls.append("rb"), {"ok": True})[1],
              reexec=lambda: calls.append("exec"), echo=lambda *_: None)
    for _ in range(lc.BOOT_FAILURE_ATTEMPTS - 1):
        assert boot_step("gateway", **kw) is None  # a crash, then the supervisor starts it again
    assert boot_step("gateway", **kw) == "rolled_back"
    assert calls == ["rb", "exec"]
    res = lc.read_request(data_home=home).update_result
    assert res["boot_failed"] and not res["ok"]


def test_boot_without_a_request_only_stamps(home):
    from cli.commands._lifecycle_boot import boot_step
    assert boot_step("telegram", data_home=home, echo=lambda *_: None) is None
    assert lc.read_boot(data_home=home)["command"] == "telegram"


def test_boot_survives_an_unreadable_record(home):
    from cli.commands._lifecycle_boot import boot_step
    d = lc.lifecycle_dir(home)
    d.mkdir(parents=True)
    (d / "request.json").write_text("{broken")
    assert boot_step("gateway", data_home=home, echo=lambda *_: None) is None


# ---- end to end through the real gateway ------------------------------------

def test_gateway_round_trip_exit_75_then_back_online(monkeypatch, tmp_path):
    """A restart request → the running gateway drains, stops intake and exits
    75; the next start answers the request with one "back online" line."""
    import os as _os
    from click.testing import CliRunner
    from surfaces._launch import Launched
    from tests.unit.cli.test_gateway_command import _patch_gateway_bootstrap

    _patch_gateway_bootstrap(monkeypatch, data_dir=str(tmp_path / "data"))
    data = tmp_path / "data"
    _os.environ["POLYROB_DATA_DIR"] = str(data)
    _os.environ["INVOCATION_ID"] = "test-unit"
    monkeypatch.setattr(lc, "_in_service_cgroup", lambda *a, **k: True)  # a pytest shell is a .scope
    _os.environ["TELEGRAM_SURFACE_ENABLED"] = "true"
    _os.environ["TELEGRAM_BOT_TOKEN"] = "tok"
    monkeypatch.setattr("cli.commands._lifecycle.POLL_SEC", 0.05)
    sent = []

    async def _notify(container, req, text):
        sent.append(text)
        return "sent"

    monkeypatch.setattr("cli.commands._lifecycle.notify", _notify)
    intake = {"on": True, "batches": 0}

    async def _launch(ctx):
        async def _run():
            while intake["on"]:
                intake["batches"] += 1
                await asyncio.sleep(0.01)
        return Launched(run=_run, on_signal=lambda: intake.__setitem__("on", False))

    monkeypatch.setattr("surfaces.telegram.launch.launch", _launch)
    lc.request_restart(requested_by="owner", origin={"surface": "telegram", "user_id": "u1",
                                                     "chat_id": "42"}, data_home=data)
    from cli.commands import gateway as gw
    # The request stands in for one sent while this gateway runs (made here,
    # before the start, it would be settled by the boot step as already answered).
    real_settle = lc.settle_stale_request
    monkeypatch.setattr(lc, "settle_stale_request", lambda *a, **k: None)
    res = CliRunner().invoke(gw.gateway, ["--no-autonomy"])
    monkeypatch.setattr(lc, "settle_stale_request", real_settle)
    assert res.exit_code == lc.RESTART_EXIT_CODE, res.output
    assert not intake["on"] and intake["batches"] > 0
    assert lc.read_request(data_home=data).state == lc.STATE_EXITING
    lc.end_drain()

    intake["on"] = True

    async def _launch_and_stop(ctx):
        intake["on"] = False  # the second process just starts and stops
        return await _launch(ctx)

    monkeypatch.setattr("surfaces.telegram.launch.launch", _launch_and_stop)
    res2 = CliRunner().invoke(gw.gateway, ["--no-autonomy"])
    assert res2.exit_code == 0, res2.output
    assert lc.read_request(data_home=data) is None
    assert lc.history(limit=1, data_home=data)[0]["outcome"] == lc.OUTCOME_BACK_ONLINE
    assert [t for t in sent if t.startswith("✅ Back online on")] != []


# ---- 077 review fixes --------------------------------------------------------

def test_a_stop_during_the_drain_yields_and_leaves_it_for_the_next_start(home, monkeypatch, sent):
    clock, intake = _Clock(), {}
    _busy_for(monkeypatch, 10_000)
    lc.request_restart(requested_by="owner", data_home=home)
    w, exits = _watcher(home, clock, intake)

    async def _go():
        task = asyncio.ensure_future(w.run())
        while lc.read_request(data_home=home).state != lc.STATE_DRAINING:
            await asyncio.sleep(0)
        w.abort()  # SIGTERM arrives
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(_go())
    assert w.exit_code is None and not exits
    assert lc.read_request(data_home=home).state == lc.STATE_EXITING


def test_a_failed_drain_closes_the_request(home, monkeypatch, sent):
    clock, intake = _Clock(), {}

    def _boom(_home):
        raise RuntimeError("sqlite exploded")

    monkeypatch.setattr("cli.commands._lifecycle._is_idle", _boom)
    lc.request_restart(requested_by="owner", data_home=home)
    w, _ = _watcher(home, clock, intake)

    async def _go():
        task = asyncio.ensure_future(w.run())
        while lc.read_request(data_home=home) is not None:
            await asyncio.sleep(0)
        task.cancel()

    asyncio.run(_go())
    assert lc.history(limit=1, data_home=home)[0]["outcome"] == lc.OUTCOME_CANCELLED
    assert not lc.draining() and any("drain failed" in t for t in sent)


def test_boot_settles_a_request_its_dead_drainer_left(home):
    from cli.commands._lifecycle_boot import boot_step
    import time as _t
    lc.request_restart(requested_by="owner", data_home=home)
    lc.claim_request(pid=999_999_999, data_home=home)  # the drainer died
    _t.sleep(0.01)
    boot_step("gateway", data_home=home, echo=lambda *_: None)
    assert lc.read_request(data_home=home).state == lc.STATE_EXITING, \
        "this start answers it; no second restart"


def test_a_secondary_process_never_applies_or_settles(home):
    from cli.commands._lifecycle_boot import boot_step
    _exiting_update(home)
    called = []
    did = boot_step("email", data_home=home, role="email",
                    apply=lambda ch: called.append(ch) or {"ok": True},
                    reexec=lambda: called.append("exec"), echo=lambda *_: None)
    assert did is None and not called
    assert lc.read_boot(data_home=home) is None, "the primary record is untouched"
    assert lc.read_boot(data_home=home, role="email")["command"] == "email"


def test_an_interrupted_apply_is_rolled_back_at_the_next_start(home):
    from cli.commands._lifecycle_boot import boot_step
    _exiting_update(home, in_progress=True, old_sha="abc", old_branch="HEAD")
    calls = []
    did = boot_step("gateway", data_home=home,
                    rollback=lambda res: (calls.append(res["old_sha"]), {"ok": True})[1],
                    reexec=lambda: calls.append("exec"), echo=lambda *_: None)
    assert did == "rolled_back" and calls == ["abc", "exec"]
    assert lc.read_request(data_home=home).update_result["boot_failed"]


def test_apply_records_the_commit_before_anything_moves(home):
    from cli.commands._lifecycle_boot import boot_step
    _exiting_update(home)
    seen = {}

    def _apply(ch):
        seen.update(lc.read_request(data_home=home).update_result)
        return {"ok": False, "failed_step": "install", "rolled_back": True}

    boot_step("gateway", data_home=home, apply=_apply, echo=lambda *_: None)
    assert seen.get("in_progress") is True and "old_sha" in seen


def test_a_clean_stop_never_reads_as_a_live_service(home):
    lc.record_boot(data_home=home)
    assert lc.running_service(home) is not None
    lc.mark_stopped(data_home=home)
    assert lc.running_service(home) is None


def test_the_unclaimed_withdrawal_loses_to_a_claim(home):
    req, _ = lc.request_restart(requested_by="cli", data_home=home)
    lc.claim_request(data_home=home)
    assert not lc.withdraw_if_open(req.id, "x", data_home=home)
    assert lc.read_request(data_home=home).state == lc.STATE_DRAINING


# ---- prod's three-unit shape: one primary, two secondaries -------------------

def test_a_secondary_never_claims_an_update(home):
    lc.request_restart(kind=lc.KIND_UPDATE, requested_by="owner",
                       origin={"surface": "email"}, data_home=home)
    assert lc.claim_request(data_home=home, serves={"email"}, primary=False) is None
    assert lc.claim_request(data_home=home, serves={"telegram"}, primary=True) is not None


def test_a_secondary_answers_only_its_own_restart(home, sent):
    from cli.commands._lifecycle import announce_boot
    lc.request_restart(requested_by="owner", origin={"surface": "telegram"}, data_home=home)
    lc.claim_request(data_home=home)
    lc.set_request_state(lc.STATE_EXITING, data_home=home)
    lc.record_boot(data_home=home, role="email")
    assert asyncio.run(announce_boot(object(), home, role="email",
                                     serves=frozenset({"email"}))) is None
    assert lc.read_request(data_home=home).state == lc.STATE_EXITING, "left for the primary"
    lc.record_boot(data_home=home)
    assert asyncio.run(announce_boot(object(), home)).startswith("✅ Back online on")


def test_a_secondary_answers_a_restart_from_a_surface_it_serves(home, sent):
    from cli.commands._lifecycle import announce_boot
    lc.request_restart(requested_by="owner", origin={"surface": "email"}, data_home=home)
    lc.claim_request(data_home=home, serves={"email"}, primary=False)
    lc.set_request_state(lc.STATE_EXITING, data_home=home)
    lc.record_boot(data_home=home, role="email")
    assert asyncio.run(announce_boot(object(), home, role="email",
                                     serves=frozenset({"email"}))).startswith("✅ Back online")


def test_a_gateway_without_autonomy_is_secondary(monkeypatch, tmp_path):
    """Prod runs `polyrob gateway --no-autonomy` beside the agent unit: it must
    never apply an update or take the primary boot record."""
    import os as _os
    from click.testing import CliRunner
    from tests.unit.cli.test_gateway_command import _patch_gateway_bootstrap
    _patch_gateway_bootstrap(monkeypatch, data_dir=str(tmp_path / "data"))
    _os.environ["POLYROB_DATA_DIR"] = str(tmp_path / "data")
    seen = []
    monkeypatch.setattr("cli.commands._lifecycle_boot.boot_step",
                        lambda cmd, **kw: seen.append(kw.get("role")) or None)
    from cli.commands import gateway as gw
    CliRunner().invoke(gw.gateway, ["--no-autonomy"])
    assert seen == ["gateway"]


def test_an_idle_gateway_counts_as_a_healthy_start(monkeypatch, tmp_path):
    """Prod's gateway idles with no surface: the idle IS its healthy state, so
    its boot record must not count every start as one that never came up."""
    import os as _os
    from click.testing import CliRunner
    from tests.unit.cli.test_gateway_command import _patch_gateway_bootstrap
    data = tmp_path / "data"
    _patch_gateway_bootstrap(monkeypatch, data_dir=str(data))
    _os.environ["POLYROB_DATA_DIR"] = str(data)

    async def _wait(self):
        return None

    monkeypatch.setattr("asyncio.Event.wait", _wait)
    from cli.commands import gateway as gw
    for _ in range(2):
        CliRunner().invoke(gw.gateway, ["--no-autonomy", "--idle-when-empty",
                                        "--skip", "telegram,email"])
    boot = lc.read_boot(data_home=data, role="gateway")
    assert boot["healthy_at"] and boot["attempts"] == 0


def test_a_stop_during_the_apply_keeps_its_result(home):
    import os
    import signal
    from cli.commands._lifecycle_boot import boot_step
    _exiting_update(home)

    def _apply(ch):
        os.kill(os.getpid(), signal.SIGTERM)  # deferred until the apply returns
        return {"ok": True, "from_version": "1", "to_version": "2"}

    with pytest.raises(SystemExit):
        boot_step("gateway", data_home=home, apply=_apply, reexec=lambda: None,
                  echo=lambda *_: None)
    res = lc.read_request(data_home=home).update_result
    assert res["ok"] and not res.get("in_progress")


def test_an_interrupted_apply_never_restores_an_older_updates_snapshot(tmp_path, monkeypatch):
    import time
    from types import SimpleNamespace
    from cli.commands import update as upd
    snap = tmp_path / "old-snap"
    snap.mkdir()
    restored = []
    monkeypatch.setattr(upd, "detect_install", lambda: SimpleNamespace())
    monkeypatch.setattr(upd, "build_runners", lambda *a, **k: SimpleNamespace(
        rollback_code=lambda: None))
    monkeypatch.setattr(upd, "resolve_update_context", lambda: SimpleNamespace(
        snapshots_root=tmp_path, data_home=tmp_path, config_paths=[], dir_paths=[],
        db_paths=[]))
    monkeypatch.setattr(upd, "latest_complete", lambda _r: SimpleNamespace(
        path=snap, name="old-snap", complete=True))
    monkeypatch.setattr(upd, "restore_snapshot", lambda *a, **k: restored.append(a))
    out = upd.rollback_for_boot({"in_progress": True, "old_sha": "abc",
                                 "started_at": time.time() + 60})
    assert out["ok"] and not restored
