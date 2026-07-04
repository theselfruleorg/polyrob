"""Local subprocess execution backend (Item 3 — WS-C2).

Runs code in a subprocess with a hard wall-clock timeout (process-group kill),
an output cap, and an env allowlist that NEVER inherits secrets (``*_API_KEY`` etc.).

⚠️ This is a CONVENIENCE backend, NOT a security sandbox. Keep ``CODE_EXEC_ENABLED``
OFF for multi-tenant prod until a hard-sandbox backend (Docker/Modal) exists — this
one is for single-user/local use only.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import signal
import sys
import tempfile
import time

from tools.code_exec.backend import ExecutionBackend
from tools.code_exec.env_policy import SAFE_ALLOWLIST, SECRET_PAT, build_child_env
from tools.code_exec.result import ExecutionRequest, ExecutionResult


class LocalSubprocessBackend(ExecutionBackend):
    name = "local_subprocess"

    # Back-compat re-exports; the single source is now tools.code_exec.env_policy so
    # every backend shares one secret-scrub (P0-B).
    SAFE_ALLOWLIST = SAFE_ALLOWLIST
    SECRET_PAT = SECRET_PAT

    def __init__(self) -> None:
        self.max_timeout = float(os.getenv("CODE_EXEC_MAX_TIMEOUT_SEC", "30"))
        self.max_output = int(os.getenv("CODE_EXEC_MAX_OUTPUT_BYTES", "100000"))

    async def setup(self) -> None:  # no-op
        return None

    async def teardown(self) -> None:  # no-op
        return None

    @property
    def capabilities(self):
        return {"network": True, "isolation": "process", "sandbox": False}

    # -- helpers --------------------------------------------------------------

    def _clamp_timeout(self, t) -> float:
        if t is None:
            return self.max_timeout
        return max(1.0, min(float(t), self.max_timeout))

    def _build_env(self, extra) -> dict:
        # Delegate to the shared policy (single source of truth for the scrub).
        return build_child_env(extra)

    def _cap(self, data: bytes):
        text = (data or b"").decode("utf-8", errors="replace")
        if len(text) > self.max_output:
            return text[: self.max_output] + f"\n...[truncated {len(text) - self.max_output} chars]", True
        return text, False

    @staticmethod
    def _kill_group(proc) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # -- run ------------------------------------------------------------------

    async def run(self, request: ExecutionRequest) -> ExecutionResult:
        lang = (request.language or "").lower()
        timeout = self._clamp_timeout(request.timeout)
        env = self._build_env(request.env)
        # NOTE: request.network is IGNORED by this backend — a local subprocess always has
        # host network. Sandbox backends (DockerBackend, Task 3) honor request.network.

        if lang in ("python", "python3", "py"):
            cmd = [sys.executable or "python3", "-I", "-c", request.code]
        elif lang in ("bash", "sh", "shell"):
            cmd = ["bash", "-c", request.code]
        else:
            return ExecutionResult(
                stderr=f"unsupported language '{request.language}' (use python|bash)",
                exit_code=2,
                backend=self.name,
            )

        workdir = request.workdir or tempfile.mkdtemp(prefix="rob_codeexec_")
        created_tmp = request.workdir is None
        if not created_tmp:
            # A caller-supplied workspace dir may not exist yet; subprocess cwd requires it.
            os.makedirs(workdir, exist_ok=True)
        start = time.monotonic()
        timed_out = False
        exit_code = 1
        stdout = stderr = b""
        stdin_bytes = (request.stdin or "").encode() if request.stdin else None

        # NB: use a SYNCHRONOUS subprocess in a thread executor rather than
        # asyncio.create_subprocess_exec. The latter raises an (empty)
        # NotImplementedError when the agent loop runs off the main thread / without
        # a child watcher (the CLI path) — so code_exec silently "didn't work in the
        # CLI". subprocess.Popen in a worker thread is loop-agnostic and preserves the
        # pgroup-kill timeout.
        def _run_sync():
            import subprocess
            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=workdir,
                    env=env,
                    stdin=subprocess.PIPE if stdin_bytes is not None else subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,  # own process group -> killpg on timeout
                )
            except Exception as e:
                detail = f"{type(e).__name__}: {e}".rstrip(": ").strip()
                return b"", f"execution error: {detail}".encode(), 1, False
            try:
                out, err = proc.communicate(input=stdin_bytes, timeout=timeout)
                return out, err, proc.returncode, False
            except subprocess.TimeoutExpired:
                self._kill_group(proc)
                try:
                    out, err = proc.communicate(timeout=5)
                except Exception:
                    out, err = b"", b""
                return out, err, proc.returncode, True

        try:
            loop = asyncio.get_event_loop()
            stdout, stderr, exit_code, timed_out = await loop.run_in_executor(None, _run_sync)
        finally:
            if created_tmp:
                shutil.rmtree(workdir, ignore_errors=True)

        out, t1 = self._cap(stdout)
        err, t2 = self._cap(stderr)
        return ExecutionResult(
            stdout=out,
            stderr=err,
            exit_code=exit_code,
            timed_out=timed_out,
            truncated=t1 or t2,
            duration_sec=time.monotonic() - start,
            backend=self.name,
        )
