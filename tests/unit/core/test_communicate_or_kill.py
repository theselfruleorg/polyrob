"""``wait_for(proc.communicate())`` cancels the read, not the OS process: a timed-out child
kept running. One helper kills and reaps it, on a timeout and on a cancel."""
import asyncio
import base64
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.async_bridge import communicate_or_kill

_SLEEPS = [sys.executable, "-c", "import time; time.sleep(60)"]


async def _spawn(*args, **kw):
    return await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **kw)


@pytest.mark.asyncio
async def test_a_timeout_kills_and_reaps_the_child():
    proc = await _spawn(*_SLEEPS)
    with pytest.raises(asyncio.TimeoutError):
        await communicate_or_kill(proc, 0.1)
    assert proc.returncode == -9


@pytest.mark.asyncio
async def test_a_cancel_kills_and_reaps_the_child():
    proc = await _spawn(*_SLEEPS)
    task = asyncio.create_task(communicate_or_kill(proc, 60))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert proc.returncode == -9


@pytest.mark.asyncio
async def test_a_child_that_ends_returns_its_output():
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import sys; print(sys.stdin.read().upper())",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    out, err = await communicate_or_kill(proc, 20, input=b"hello")
    assert out.strip() == b"HELLO" and err == b"" and proc.returncode == 0


class _StuckProc:
    returncode = None

    def __init__(self):
        self.killed = False

    async def communicate(self, input=None):
        await asyncio.sleep(60)

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


def _spawns(monkeypatch, proc):
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))


@pytest.mark.asyncio
async def test_the_update_check_kills_a_stuck_child(monkeypatch):
    from surfaces.telegram import lifecycle_ops
    proc = _StuckProc()
    _spawns(monkeypatch, proc)
    monkeypatch.setattr(lifecycle_ops, "CHECK_TIMEOUT_SEC", 0.01)
    payload, why = await lifecycle_ops._update_check("stable")
    assert payload is None and "did not answer" in why
    assert proc.killed
@pytest.mark.asyncio
async def test_a_cancelled_anysite_run_kills_the_child(monkeypatch):
    client = pytest.importorskip("polyrob_discovery.anysite.client")
    proc = _StuckProc()
    _spawns(monkeypatch, proc)
    monkeypatch.setattr(client, "_host_refusal", lambda: None)
    task = asyncio.create_task(client.run_anysite(["anysite"], timeout=60))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert proc.killed


@pytest.mark.asyncio
async def test_a_timed_out_anysite_run_says_so(monkeypatch):
    client = pytest.importorskip("polyrob_discovery.anysite.client")
    proc = _StuckProc()
    _spawns(monkeypatch, proc)
    monkeypatch.setattr(client, "_host_refusal", lambda: None)
    res = await client.run_anysite(["anysite"], timeout=0.01)
    assert res.timed_out is True and proc.killed


def _png_item():
    state = SimpleNamespace(screenshot=base64.b64encode(b"not-a-real-png").decode())
    return SimpleNamespace(state=state, model_output=None)


@pytest.mark.parametrize("gif_ok", [True, False])
def test_the_gif_temp_frames_are_removed(monkeypatch, tmp_path, gif_ok):
    from agents.task import gif_utils
    seen = []

    def _create(paths, output_path, captions, user_id=None):
        seen.extend(paths)
        assert all(os.path.exists(p) for p in paths)
        if not gif_ok:
            raise RuntimeError("gif failed")
        return True
    monkeypatch.setattr(gif_utils, "create_gif_with_retry", _create)
    out = gif_utils.create_history_gif([_png_item(), _png_item()], "s1",
                                       output_path=str(tmp_path / "o.gif"))
    assert (out is not None) is gif_ok
    assert len(seen) == 2 and not any(os.path.exists(p) for p in seen)


def test_a_screenshot_file_of_the_session_is_kept(monkeypatch, tmp_path):
    from agents.task import gif_utils
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"x")
    monkeypatch.setattr(gif_utils, "create_gif_with_retry", lambda *a, **k: True)
    item = SimpleNamespace(state=SimpleNamespace(screenshot_path=str(shot)), model_output=None)
    gif_utils.create_history_gif([item], "s1", output_path=str(tmp_path / "o.gif"))
    assert Path(shot).exists()


def test_a_started_xvfb_outlives_the_browser_that_started_it(monkeypatch):
    """Another browser finds the display by pgrep: a close must not end it."""
    from unittest.mock import mock_open
    from tools.browser.browser import Browser
    xvfb = SimpleNamespace(pid=4321, poll=lambda: None)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: xvfb)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/Xvfb")
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr("builtins.open", mock_open())  # the pid file stays out of the tree
    browser = Browser.__new__(Browser)
    browser.browser_config = SimpleNamespace(auto_configure_for_server=True, headless=False)
    browser._xvfb_process = None
    env = browser._setup_display_for_server({"PATH": "/usr/bin"})
    assert env["DISPLAY"] == ":99"
    assert browser._xvfb_process is None
    browser._started_xvfb = False  # __del__ of this bare object must not touch the fake
