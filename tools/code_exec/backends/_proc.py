"""Shared subprocess-group runner for the CLI-shelling backend runners (dedup).

``docker.py::_default_docker_runner`` and ``ssh.py::_default_ssh_runner`` ran a
byte-identical launch/communicate/TimeoutExpired/killpg/second-communicate
sequence in a thread executor (never ``asyncio.create_subprocess_exec`` — the
non-main-thread child-watcher hazard, see ``test_thread_loop_subprocess.py``),
then decoded the byte output. ``run_group`` is that shared core; each caller
keeps its own argv construction and its own raise-on-timeout exception/message.

``label`` preserves the per-caller launch-error message shape
(``"docker launch error: ..."`` vs ``"ssh launch error: ..."``).
"""
from __future__ import annotations

import asyncio
import os
import signal
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
    False)``. On ``TimeoutExpired`` the WHOLE group is SIGKILLed (``killpg``,
    fallback ``proc.kill()``), residual output drained with a 5s bound, and
    ``timed_out=True`` returned — the caller decides whether that raises.
    """

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
        try:
            out, err = proc.communicate(input=stdin_bytes, timeout=timeout)
            return proc.returncode, out, err, False
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                out, err = proc.communicate(timeout=5)
            except Exception:
                out, err = b"", b""
            return (proc.returncode if proc.returncode is not None else 1), out, err, True

    loop = asyncio.get_event_loop()
    code, out, err, timed_out = await loop.run_in_executor(None, _run_sync)
    out_text = (out or b"").decode("utf-8", errors="replace")
    err_text = (err or b"").decode("utf-8", errors="replace")
    return code, out_text, err_text, timed_out
