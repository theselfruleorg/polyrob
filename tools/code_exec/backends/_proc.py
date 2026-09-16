"""Shared subprocess-group runner for the CLI-shelling backend runners (dedup).

Docker and SSH CLI runners share incremental bounded pipe capture in a thread
executor (avoiding the non-main-thread asyncio child-watcher hazard). Each caller
keeps its argv construction and its own raise-on-timeout exception/message.

``label`` preserves the per-caller launch-error message shape
(``"docker launch error: ..."`` vs ``"ssh launch error: ..."``).
"""
from __future__ import annotations

import asyncio
import os
import signal
import threading
from typing import List, Optional, Tuple

from tools.code_exec.env_policy import build_child_env


async def run_group(
    cmd: List[str],
    *,
    stdin_bytes: Optional[bytes],
    timeout: Optional[float],
    label: str,
) -> Tuple[int, str, str, bool]:
    """Run ``cmd`` in its own process group off the event-loop thread.

    Returns ``(returncode, stdout_text, stderr_text, timed_out)``. A launch
    failure never raises — it returns ``(1, "", "<label> launch error: ...",
    False)``. Timeouts, output overflow and cancellation kill the process group
    (falling back to the direct child where group signalling is unavailable).
    Output is bounded during capture; cancellation waits for child cleanup.
    """

    stop = threading.Event()
    limit = int(os.getenv("CODE_EXEC_MAX_OUTPUT_BYTES", "100000"))

    def kill(proc):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, AttributeError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    def _run_sync():
        import subprocess
        try:
            proc = subprocess.Popen(
                cmd, env=build_child_env({}),
                stdin=subprocess.PIPE if stdin_bytes is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True,  # own process group -> killpg on timeout
            )
        except Exception as e:
            return 1, b"", f"{label} launch error: {type(e).__name__}: {e}".encode(), False
        from tools.code_exec.backends.bounded_capture import capture
        out, err, timed_out, truncated = capture(
            proc, data=stdin_bytes, timeout=timeout, limit=limit, stop=stop, kill=kill,
        )
        if truncated:
            err += b"\n[execution stopped: output limit exceeded]"
        return proc.returncode, out, err, timed_out

    loop = asyncio.get_event_loop()
    future = loop.run_in_executor(None, _run_sync)
    try:
        code, out, err, timed_out = await asyncio.shield(future)
    except asyncio.CancelledError:
        stop.set()
        await asyncio.shield(future)
        raise
    out_text = (out or b"").decode("utf-8", errors="replace")
    err_text = (err or b"").decode("utf-8", errors="replace")
    return code, out_text, err_text, timed_out
