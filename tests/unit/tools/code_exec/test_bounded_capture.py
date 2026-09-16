import asyncio
import os
import sys

import pytest

from tools.code_exec.backends._proc import run_group


@pytest.mark.asyncio
async def test_shared_runner_caps_both_output_streams(monkeypatch):
    monkeypatch.setenv("CODE_EXEC_MAX_OUTPUT_BYTES", "4096")
    code, out, err, timeout = await run_group(
        [sys.executable, "-I", "-c", "import os\nwhile True:\n os.write(1,b'x'*1024)\n os.write(2,b'y'*1024)"],
        stdin_bytes=None, timeout=10, label="test",
    )
    assert code != 0
    assert not timeout
    assert len(out.encode()) + len(err.encode()) < 4300
    assert "output limit exceeded" in err


@pytest.mark.asyncio
async def test_shared_runner_handles_input_larger_than_pipe(monkeypatch):
    monkeypatch.setenv("CODE_EXEC_MAX_OUTPUT_BYTES", "200000")
    code, out, err, timeout = await run_group(
        [sys.executable, "-I", "-c", "import sys; print(len(sys.stdin.buffer.read()))"],
        stdin_bytes=b"x" * 200000, timeout=5, label="test",
    )
    assert code == 0
    assert out.strip() == "200000"
    assert not timeout


@pytest.mark.asyncio
async def test_shared_runner_cancellation_reaps_child(tmp_path):
    pid_file = tmp_path / "pid"
    task = asyncio.create_task(run_group(
        [sys.executable, "-I", "-c", "import os,sys,time; open(sys.argv[1],'w').write(str(os.getpid())); time.sleep(30)", str(pid_file)],
        stdin_bytes=None, timeout=30, label="test",
    ))
    try:
        for _ in range(100):
            if pid_file.exists():
                break
            await asyncio.sleep(0.02)
        assert pid_file.exists()
        pid = int(pid_file.read_text())
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
