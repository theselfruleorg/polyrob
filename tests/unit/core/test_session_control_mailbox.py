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
