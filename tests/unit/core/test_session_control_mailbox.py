"""Control requests cross processes and pause execution, not only metadata."""
import asyncio
import multiprocessing
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.session_control import SessionControl


def _worker(directory):
    from pathlib import Path
    from agents.task.agent.core.session_control import control_checkpoint
    async def drive():
        store = SessionControl(directory)
        generation = store.start()
        agent = SimpleNamespace(_session_control=(store, generation), cancel=lambda: None)
        try:
            while await control_checkpoint(agent):
                with (Path(directory) / "steps").open("a") as file:
                    file.write("step\n")
                await asyncio.sleep(0.02)
        finally:
            store.finish(generation)
    asyncio.run(drive())


def _await(predicate):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("control acknowledgement timed out")


def test_pause_resume_cancel_across_processes(tmp_path):
    process = multiprocessing.get_context("spawn").Process(target=_worker, args=(str(tmp_path),))
    process.start()
    store = SessionControl(tmp_path)
    try:
        _await(lambda: store.alive(store.read()) and (tmp_path / "steps").exists())
        generation = store.request("pause")
        _await(lambda: store.read()["acknowledged"] == "paused")
        steps = (tmp_path / "steps").read_text()
        time.sleep(0.08)
        assert (tmp_path / "steps").read_text() == steps
        assert store.request("resume") == generation
        _await(lambda: len((tmp_path / "steps").read_text()) > len(steps))
        store.request("cancel")
        process.join(10)
        assert process.exitcode == 0
        assert store.read()["acknowledged"] == "cancelled"
        assert not store.alive(store.read())
    finally:
        if process.is_alive():
            process.terminate()
            process.join()


def test_no_live_owner_is_not_reported_as_cancelled(tmp_path):
    store = SessionControl(tmp_path)
    assert store.request("cancel") is None
    assert store.read() is None


@pytest.mark.asyncio
async def test_cancel_signals_registered_session_agents(tmp_path):
    from agents.task.agent.core.session_control import control_checkpoint
    store = SessionControl(tmp_path)
    generation = store.start()
    agent = SimpleNamespace(_session_control=(store, generation), cancel=Mock(),
                            orchestrator=SimpleNamespace(cancel=Mock()))
    try:
        store.request("cancel")
        assert await control_checkpoint(agent) is False
        agent.cancel.assert_called_once_with()
        agent.orchestrator.cancel.assert_called_once_with()
        assert store.read()["acknowledged"] == "cancelled"
    finally:
        store.finish(generation)


# -- 098 CO-05: a cancel is never erased ------------------------------------------

def _live(tmp_path):
    store = SessionControl(tmp_path)
    return store, store.start()


def test_resume_after_unacked_cancel_is_refused(tmp_path):
    from core.session_control import CANCELLED_ALREADY
    store, gen = _live(tmp_path)
    try:
        assert store.request("cancel") == gen
        assert store.request("resume") == CANCELLED_ALREADY
        assert store.request("pause") == CANCELLED_ALREADY
        assert store.read()["request"] == "cancel"
    finally:
        store.finish(gen)


def test_pause_then_cancel_wins(tmp_path):
    store, gen = _live(tmp_path)
    try:
        assert store.request("pause") == gen
        assert store.request("cancel") == gen
        assert store.read()["request"] == "cancel"
    finally:
        store.finish(gen)


def test_finish_after_cancel_reports_cancelled(tmp_path):
    store, gen = _live(tmp_path)
    store.request("cancel")
    store.request("resume")
    store.finish(gen)
    assert store.read()["acknowledged"] == "cancelled"


@pytest.mark.asyncio
async def test_request_and_wait_says_cancelled_already(tmp_path):
    from core.session_control import CANCELLED_ALREADY
    store, gen = _live(tmp_path)
    try:
        store.request("cancel")
        assert await store.request_and_wait("resume", timeout=0.1) == CANCELLED_ALREADY
    finally:
        store.finish(gen)


def test_session_control_machine_well_formed():
    from core.session_control import SESSION_CONTROL
    assert SESSION_CONTROL.validate() == []


def _send(directory, command):
    SessionControl(directory).request(command)


def test_two_processes_cancel_and_resume(tmp_path):
    """Both orders across processes: the run always ends on cancel."""
    ctx = multiprocessing.get_context("spawn")
    for order in (("cancel", "resume"), ("resume", "cancel")):
        d = tmp_path / "-".join(order)
        d.mkdir()
        store, gen = _live(d)
        store.request("pause")
        try:
            for cmd in order:
                p = ctx.Process(target=_send, args=(str(d), cmd))
                p.start()
                p.join(10)
            assert store.read()["request"] == "cancel"
        finally:
            store.finish(gen)


def test_a_pid_reused_after_a_reboot_is_not_a_live_run(tmp_path):
    """The mailbox stored a bare pid: after a reboot a reused live pid read as
    "already has a live run" for ever. The generation now carries the run's process
    identity (no schema change); an old row keeps the bare-pid check."""
    import os
    import sqlite3
    from core.state.proc import ProcIdentity, process_identity
    store = SessionControl(tmp_path)
    generation = store.start()
    assert process_identity().encode() in generation
    assert SessionControl.alive(store.read())
    # The same live pid, recorded by a run of an earlier boot.
    ghost = ProcIdentity(os.getppid(), "earlier", "boot-earlier").encode()
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE control SET generation=?, pid=? WHERE id=1",
                     (f"deadbeef|{ghost}", os.getppid()))
    assert not SessionControl.alive(store.read())
    assert store.start()                      # not refused
    # An old-format row (a bare uuid): the bare-pid check still holds it live.
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE control SET generation='deadbeef', pid=? WHERE id=1",
                     (os.getppid(),))
    assert SessionControl.alive(store.read())
