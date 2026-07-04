"""Hardened Docker execution backend (P0-A) + opt-in persistent per-session mode (P1-B).

Runs code inside a locked-down container so agent-authored code never touches the
trusted host process. The container is a security boundary (namespaces), not perfect
isolation — for genuinely untrusted multi-tenant code a microVM (P1 E2B) is the real
answer — but with all-caps-dropped, no-new-privileges, read-only rootfs, a
workspace-only bind mount, PID/memory/CPU caps and network-deny-by-default it is a
sane server default and the reference sandbox for the sandbox-invariant guard (P0-4).

Two modes, selected by the constructor's ``session_id``:

* **Ephemeral (default — ``session_id=None``, e.g. plain ``DockerBackend()``):** the
  P0 behavior, byte-for-byte unchanged. Every ``run()`` call is its own ``docker run
  --rm`` — a fresh container, gone the instant the call returns. No state survives
  between calls. This is what every existing caller gets today.
* **Persistent (opt-in — ``session_id=<sid>``):** ``setup()`` starts ONE long-lived
  container (``docker run -d ... sleep infinity``) labeled ``polyrob.sandbox=1`` +
  ``polyrob.session=<sid>``; every ``run()`` call ``docker exec``s into that SAME
  container, so ``pip install`` / ``cd`` / created files persist across calls within
  the session. ``teardown()`` force-removes it; a process that crashed without
  calling ``teardown()`` leaves an orphaned labeled container behind for
  ``reap_orphans()`` to sweep up on the next process start. Gated end-to-end by
  ``CODE_EXEC_DOCKER_PERSISTENT`` (default OFF — see ``tools/code_exec/__init__.py``
  for the flag helper and the documented wiring gap to a real ``session_id``).

Both modes share ONE hardening-flag helper (``_hardening_flags``) so the persistent
container's ``docker run -d`` can never drift from the ephemeral path's ``docker run
--rm`` flags. All persistent-mode docker invocations go through the injectable
``self._docker`` runner (``DockerRunner`` — no daemon needed to unit-test them); the
ephemeral path is untouched and still shells out directly, exactly as P0 shipped it.

The argv builder ``_build_run_argv`` is PURE (env-read only, no subprocess) so the
hardening flags are unit-testable without Docker installed.

Holds no ``@BaseTool.action`` closures — ``from __future__ import annotations`` is safe.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import tempfile
import time
import uuid
from typing import Awaitable, Callable, List, Optional, Tuple

from tools.code_exec.backend import ExecutionBackend, ExecutionBackendError
from tools.code_exec.env_policy import SECRET_PAT, build_child_env
from tools.code_exec.result import ExecutionRequest, ExecutionResult

logger = logging.getLogger(__name__)

_PY = ("python", "python3", "py")
_SH = ("bash", "sh", "shell")

#: Label applied to every persistent container this backend creates — the marker
#: ``reap_orphans`` filters ``docker ps`` on.
_SANDBOX_LABEL = "polyrob.sandbox=1"

#: Exit codes that mean "the in-container `timeout --signal=KILL <n>` wrapper
#: fired" (see ``_run_persistent``). GNU coreutils `timeout` exits 124 when it
#: kills the child with the DEFAULT signal (TERM) — but we deliberately pass
#: ``--signal=KILL`` (untrusted code must not be able to catch/ignore the kill),
#: and coreutils' own documented behavior is that when the child is actually
#: terminated BY a KILL signal, `timeout` forwards the child's own
#: signal-death exit status (128+9=137) instead of the synthetic 124 --
#: empirically confirmed against the `python:3.12-slim` image's coreutils.
#: 124 is kept too, defensively (e.g. if the signal policy ever changes).
#: NOTE: 137 is inherently a little ambiguous — a process killed by SIGKILL
#: for an unrelated reason (e.g. the container's own `--memory` limit OOM-
#: killing it) also exits 137 and would be reported as `timed_out=True` here.
#: That's an acceptable, documented trade-off: both cases are genuine
#: abnormal-termination failures, and `exit_code` (137) stays available on
#: the result for a caller that needs to distinguish them.
_TIMEOUT_EXIT_CODES = frozenset({124, 137})

#: (argv-without-leading-"docker", *, input=<stdin text|None>, timeout=<seconds|None>)
#: -> (returncode, stdout_text, stderr_text). Injectable so persistent-mode tests need
#: no Docker daemon (see ``test_docker_persistent.py``'s fake runners).
DockerRunner = Callable[..., Awaitable[Tuple[int, str, str]]]


class _DockerExecTimeout(Exception):
    """Raised by ``_default_docker_runner`` when a docker CLI call exceeds its
    timeout. Only the DEFAULT runner ever raises this — an injected fake runner
    never does (tests don't simulate real subprocess timeouts) — so this is purely
    an internal signal between the default runner and its persistent-mode callers.
    """


async def _default_docker_runner(
    args: List[str], *, input: Optional[str] = None, timeout: Optional[float] = None
) -> Tuple[int, str, str]:
    """Default ``DockerRunner``: invokes the real ``docker`` CLI off the event-loop
    thread (never ``asyncio.create_subprocess_exec`` — the same non-main-thread
    child-watcher hazard the ephemeral path and ``local_subprocess`` already dodge
    via ``run_in_executor``, see ``test_thread_loop_subprocess.py``). ``args``
    excludes the leading ``"docker"`` token (the fake runners in tests mirror this).
    """
    argv = ["docker"] + list(args)
    stdin_bytes = input.encode() if input is not None else None

    def _run_sync():
        import subprocess
        try:
            proc = subprocess.Popen(
                argv, env=build_child_env({}),
                stdin=subprocess.PIPE if stdin_bytes is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except Exception as e:
            return 1, b"", f"docker launch error: {type(e).__name__}: {e}".encode(), False
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
    if timed_out:
        raise _DockerExecTimeout(
            err_text or f"docker {' '.join(args[:2])} timed out after {timeout}s"
        )
    return code, out_text, err_text


def _age_from_docker_timestamp(ts: str, now: float) -> Optional[float]:
    """Best-effort seconds-since-start from a docker ``.State.StartedAt``-style
    RFC3339 timestamp (typically nanosecond precision, e.g.
    ``2026-07-02T10:30:00.123456789Z``). Returns ``None`` (never guesses) when the
    timestamp can't be parsed — the caller treats that as "leave it alone": a parse
    miss must never cause an otherwise-live container to be removed.
    """
    ts = (ts or "").strip()
    if not ts:
        return None
    try:
        s = ts.replace("Z", "+00:00")
        if "." in s:
            head, _, rest = s.partition(".")
            if "+" in rest:
                frac, _, tz = rest.partition("+")
                s = f"{head}.{frac[:6]}+{tz}"
            elif "-" in rest:
                frac, _, tz = rest.partition("-")
                s = f"{head}.{frac[:6]}-{tz}"
            else:
                s = f"{head}.{rest[:6]}"
        from datetime import datetime
        started = datetime.fromisoformat(s).timestamp()
    except Exception:
        return None
    return max(0.0, now - started)


class DockerBackend(ExecutionBackend):
    name = "docker"

    def __init__(
        self,
        *,
        docker_runner: Optional[DockerRunner] = None,
        session_id: Optional[str] = None,
    ) -> None:
        self.image = os.getenv("CODE_EXEC_DOCKER_IMAGE", "python:3.12-slim")
        self.memory_mb = int(os.getenv("CODE_EXEC_CONTAINER_MEMORY_MB", "1024"))
        self.cpus = os.getenv("CODE_EXEC_CONTAINER_CPUS", "1.0")
        self.pids_limit = int(os.getenv("CODE_EXEC_PIDS_LIMIT", "256"))
        self.max_timeout = float(os.getenv("CODE_EXEC_MAX_TIMEOUT_SEC", "30"))
        self.max_output = int(os.getenv("CODE_EXEC_MAX_OUTPUT_BYTES", "100000"))
        # Container user precedence: explicit operator override (verbatim, even if root)
        # > non-root host uid:gid (keeps the mounted workspace writable) > forced-unprivileged
        # when the HOST process itself is root (prod systemd runs User=root — never let that
        # silently become uid 0 *inside* the sandbox container) > Windows fallback.
        env_user = os.getenv("CODE_EXEC_DOCKER_USER", "")
        if env_user:
            self.user = env_user
        elif hasattr(os, "getuid"):
            if os.getuid() == 0:
                self.user = "65534:65534"  # nobody:nogroup
                logging.getLogger(__name__).warning(
                    "code_exec docker backend: host process is running as root (uid 0) and "
                    "CODE_EXEC_DOCKER_USER is not set; forcing the sandbox container user to "
                    "65534:65534 (nobody:nogroup) so agent-authored code never runs as root "
                    "inside the container. Set CODE_EXEC_DOCKER_USER to override."
                )
            else:
                # Run as the invoking (non-root) user so the mounted workspace stays writable.
                self.user = f"{os.getuid()}:{os.getgid()}"
        else:
            self.user = "1000:1000"

        # -- P1-B: opt-in persistent per-session mode --------------------------------
        # session_id is None (the default, e.g. plain `DockerBackend()`) => this instance
        # is a plain EPHEMERAL backend: every method below is byte-for-byte the P0
        # behavior (`docker run --rm` per `run()` call), never touching `self._docker`.
        # session_id set => `setup()` starts ONE long-lived container for the session and
        # `run()` execs into it instead, so `pip install`/cwd/created files persist across
        # `run()` calls. See setup()/teardown()/_run_persistent() below.
        self._session_id = session_id
        self._docker: DockerRunner = docker_runner or _default_docker_runner
        self._container: Optional[str] = None  # persistent container name, once created
        self._workdir: Optional[str] = None  # persistent container's host bind-mount dir
        self._setup_lock = asyncio.Lock()  # guards persistent-mode create-once-under-races

    # -- lifecycle ------------------------------------------------------------

    async def setup(self) -> None:
        if self._session_id is None:
            # EPHEMERAL (P0, unchanged): fail fast with a clear error if the CLI is missing.
            if shutil.which("docker") is None:
                raise ExecutionBackendError(
                    "docker backend selected but the 'docker' CLI was not found on PATH. "
                    "Install Docker or set CODE_EXEC_BACKEND to another backend."
                )
            return
        # PERSISTENT (P1-B, opt-in): start ONE long-lived container for this session.
        if self._container is not None:
            return  # idempotent — already set up
        async with self._setup_lock:
            if self._container is not None:  # lost a setup() race to another waiter
                return
            workdir = self._resolve_persistent_workdir()
            network = self._resolve_network(ExecutionRequest(language="bash", code="true"))
            container_name = f"polyrob-sbx-{uuid.uuid4().hex}"
            argv = [
                "run", "-d",
                "--label", _SANDBOX_LABEL,
                "--label", f"polyrob.session={self._session_id}",
                "--name", container_name,
            ] + self._hardening_flags(network=network, workdir_host=workdir) + [
                self.image, "sleep", "infinity",
            ]
            try:
                code, out, err = await self._docker(argv, timeout=self.max_timeout)
            except _DockerExecTimeout as e:
                raise ExecutionBackendError(
                    f"docker run -d (persistent sandbox) timed out: {e}"
                ) from e
            if code != 0:
                raise ExecutionBackendError(
                    f"docker run -d (persistent sandbox) failed (exit {code}): {err or out}"
                )
            self._workdir = workdir
            self._container = container_name

    async def teardown(self) -> None:
        if self._session_id is None:
            return  # EPHEMERAL (P0, unchanged): no-op — nothing persists to clean up.
        if self._container is None:
            return  # idempotent — never set up, or already torn down
        cname = self._container
        self._container = None  # mark torn down even if the rm call below errors
        try:
            code, _out, err = await self._docker(["rm", "-f", cname], timeout=self.max_timeout)
            if code != 0 and "no such container" not in (err or "").lower():
                logger.warning("docker backend: teardown 'rm -f %s' exited %s: %s", cname, code, err)
        except _DockerExecTimeout:
            logger.warning("docker backend: teardown 'rm -f %s' timed out", cname)
        except Exception:
            logger.warning("docker backend: teardown 'rm -f %s' raised", cname, exc_info=True)

    @property
    def capabilities(self):
        default_net = (os.getenv("CODE_EXEC_NETWORK", "none") or "none").lower()
        return {
            "network": default_net not in ("none", ""),
            "isolation": "container",
            "sandbox": True,
        }

    # -- helpers --------------------------------------------------------------

    def _clamp_timeout(self, t) -> float:
        if t is None:
            return self.max_timeout
        return max(1.0, min(float(t), self.max_timeout))

    def _cap(self, data: bytes):
        text = (data or b"").decode("utf-8", errors="replace")
        if len(text) > self.max_output:
            return text[: self.max_output] + f"\n...[truncated {len(text) - self.max_output} chars]", True
        return text, False

    def _cap_text(self, text: Optional[str]):
        """Same truncation rule as ``_cap``, for callers (persistent mode) whose
        runner already hands back decoded text instead of raw bytes."""
        text = text or ""
        if len(text) > self.max_output:
            return text[: self.max_output] + f"\n...[truncated {len(text) - self.max_output} chars]", True
        return text, False

    def _resolve_network(self, request: ExecutionRequest) -> str:
        """Map policy -> docker --network value. Never silently fall back to host."""
        policy = (request.network or os.getenv("CODE_EXEC_NETWORK", "none") or "none").lower()
        if policy in ("none", ""):
            return "none"
        if policy == "host":
            return "host"
        if policy == "egress":
            return "bridge"  # outbound allowed, no host namespace; operator adds egress proxy
        return "none"

    def _hardening_flags(self, *, network: str, workdir_host: str) -> List[str]:
        """PURE: the ONE hardening-flag list shared by the ephemeral ``docker run
        --rm`` and the persistent container's ``docker run -d`` — kept in exactly one
        place so the two paths can never drift apart. Order matches the pre-P1-B
        ephemeral argv exactly (existing tests locate flags via ``.index()``, not
        position, but keep this stable regardless).
        """
        return [
            "--network", network,
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--read-only",
            "--tmpfs", "/tmp",
            "--pids-limit", str(self.pids_limit),
            "--memory", f"{self.memory_mb}m",
            "--memory-swap", f"{self.memory_mb}m",
            "--cpus", str(self.cpus),
            "--user", self.user,
            "-v", f"{workdir_host}:/workspace",
            "-w", "/workspace",
        ]

    def _container_env_flags(self, request: ExecutionRequest) -> List[str]:
        """PURE: ``-e KEY=VALUE`` flags for the caller-supplied, secret-scrubbed env.
        Shared by the ephemeral ``docker run`` and persistent ``docker exec`` argv
        builders so the scrub logic can't drift between the two paths."""
        flags: List[str] = []
        for k, v in (request.env or {}).items():
            if SECRET_PAT.search(k):
                continue  # never let a caller smuggle a secret-named var in
            flags += ["-e", f"{k}={v}"]
        return flags

    def _resolve_persistent_workdir(self) -> str:
        """Ensure + return the host dir bind-mounted into the persistent container.

        Prefers the real session workspace — the SAME directory the ephemeral backend
        would bind-mount for an identical session_id (``CodeExecutionTool.
        _resolve_workdir`` uses the same ``pm().get_workspace_dir`` call) — so the
        persistent sandbox's confinement boundary is the session workspace, not some
        other host path. Falls back to a dedicated tempdir if the session/path
        machinery is unavailable (e.g. a bare construction with a synthetic
        session_id that was never a real session).
        """
        if self._session_id:
            try:
                from agents.task.path import pm
                return str(pm().get_workspace_dir(self._session_id))
            except Exception:
                logger.warning(
                    "docker backend: could not resolve session workspace for %r; "
                    "falling back to a dedicated tempdir",
                    self._session_id, exc_info=True,
                )
        fallback = os.path.join(
            tempfile.gettempdir(), f"rob_docker_persistent_{self._session_id or 'anon'}"
        )
        os.makedirs(fallback, exist_ok=True)
        return fallback

    def _build_run_argv(self, request: ExecutionRequest, workdir: str) -> List[str]:
        """PURE: the full ``docker run`` argv incl. the in-container command.

        EPHEMERAL mode only — this is the P0 argv shape, unchanged by P1-B (this
        method is never called from the persistent path; see ``_run_persistent``,
        which builds its own ``docker exec`` argv sharing only ``_hardening_flags``
        and ``_container_env_flags``).
        """
        lang = (request.language or "").lower()
        argv = ["docker", "run", "--rm"] + self._hardening_flags(
            network=self._resolve_network(request), workdir_host=workdir
        )
        if request.stdin is not None:
            argv.append("-i")  # keep stdin open
        argv += self._container_env_flags(request)
        argv.append(self.image)
        if lang in _PY:
            argv += ["python", "-I", "-c", request.code]
        elif lang in _SH:
            argv += ["bash", "-c", request.code]
        else:
            raise ValueError(f"unsupported language '{request.language}' (use python|bash)")
        return argv

    # -- run ------------------------------------------------------------------

    async def run(self, request: ExecutionRequest) -> ExecutionResult:
        if self._session_id is not None:
            return await self._run_persistent(request)
        return await self._run_ephemeral(request)

    async def _run_ephemeral(self, request: ExecutionRequest) -> ExecutionResult:
        """EPHEMERAL mode (P0, byte-for-byte unchanged): a fresh ``docker run --rm``
        per call, direct subprocess management — never touches ``self._docker``."""
        lang = (request.language or "").lower()
        if lang not in _PY and lang not in _SH:
            return ExecutionResult(
                stderr=f"unsupported language '{request.language}' (use python|bash)",
                exit_code=2, backend=self.name,
            )
        timeout = self._clamp_timeout(request.timeout)
        workdir = request.workdir or tempfile.mkdtemp(prefix="rob_docker_")
        created_tmp = request.workdir is None
        os.makedirs(workdir, exist_ok=True)
        argv = self._build_run_argv(request, workdir)
        env = build_child_env({})  # env for the docker CLI process itself (PATH/HOME only)
        stdin_bytes = (request.stdin or "").encode() if request.stdin else None
        start = time.monotonic()

        def _run_sync():
            import subprocess
            try:
                proc = subprocess.Popen(
                    argv, env=env,
                    stdin=subprocess.PIPE if stdin_bytes is not None else subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    start_new_session=True,
                )
            except Exception as e:
                return b"", f"docker launch error: {type(e).__name__}: {e}".encode(), 1, False
            try:
                out, err = proc.communicate(input=stdin_bytes, timeout=timeout)
                return out, err, proc.returncode, False
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
            stdout=out, stderr=err, exit_code=exit_code, timed_out=timed_out,
            truncated=t1 or t2, duration_sec=time.monotonic() - start, backend=self.name,
        )

    async def _run_persistent(self, request: ExecutionRequest) -> ExecutionResult:
        """PERSISTENT mode (P1-B, opt-in): ``docker exec`` into the ONE long-lived
        container for this session — created lazily on first use if ``setup()``
        wasn't already called. ``docker exec`` inherits the container's caps/
        network/user/read-only rootfs, so the containment established at ``setup()``
        time holds for every exec too.

        Timeout handling (P1-B review, Important #1 — the foot-gun fix): a
        HOST-side-only timeout is unsafe here. ``docker exec``'s in-container
        process lifetime is independent of the host CLI client — SIGKILLing the
        client (the old behavior) leaves the process running INSIDE the (reused!)
        session container, eating its pid/memory budget and poisoning every later
        ``run()`` on that same container. The in-container command is therefore
        wrapped with coreutils ``timeout --signal=KILL <clamped_sec>`` (present in
        the default ``python:3.12-slim`` image) so the CONTAINER itself bounds and
        kills the process; ``timeout``'s exit code when it actually fires (124, or
        137 when — as here — the kill signal is KILL; see ``_TIMEOUT_EXIT_CODES``)
        is mapped to ``ExecutionResult.timed_out``. The host-side ``self._docker(...)``
        call keeps its own ``timeout=``, but set slightly HIGHER than the
        in-container bound (+5s) — a backstop for a hung ``docker exec`` client
        only, never the primary kill path; the in-container timeout is expected to
        fire first and its (already-captured) output is what gets returned.
        """
        lang = (request.language or "").lower()
        if lang not in _PY and lang not in _SH:
            return ExecutionResult(
                stderr=f"unsupported language '{request.language}' (use python|bash)",
                exit_code=2, backend=self.name,
            )
        if self._container is None:
            await self.setup()
        clamped_sec = self._clamp_timeout(request.timeout)

        argv = ["exec"]
        if request.stdin is not None:
            argv.append("-i")
        # The container's default workdir is already /workspace (set at `docker run
        # -d` time via _hardening_flags' `-w`), but pin it explicitly per-exec too —
        # defense in depth against any docker-version difference in inherited workdir.
        argv += ["-w", "/workspace"]
        argv += self._container_env_flags(request)
        argv.append(self._container)
        # In-container bound (the actual security boundary — see docstring above,
        # NOT the host-side backstop below). --signal=KILL: the sandboxed code is
        # untrusted and must not be able to catch/ignore the default SIGTERM. No
        # `--foreground`, so `timeout` puts the command in its own process group
        # and kills that whole group, catching any children it spawns too.
        argv += ["timeout", "--signal=KILL", str(clamped_sec)]
        if lang in _PY:
            argv += ["python", "-I", "-c", request.code]
        else:
            argv += ["bash", "-c", request.code]

        # Host-side backstop ONLY (see docstring) — deliberately looser than
        # clamped_sec so the in-container `timeout` above is what actually fires.
        host_backstop_sec = clamped_sec + 5

        start = time.monotonic()
        try:
            code, out, err = await self._docker(argv, input=request.stdin, timeout=host_backstop_sec)
            timed_out = code in _TIMEOUT_EXIT_CODES
        except _DockerExecTimeout as e:
            code, out, err, timed_out = 1, "", str(e), True

        out_text, t1 = self._cap_text(out)
        err_text, t2 = self._cap_text(err)
        return ExecutionResult(
            stdout=out_text, stderr=err_text, exit_code=code, timed_out=timed_out,
            truncated=t1 or t2, duration_sec=time.monotonic() - start, backend=self.name,
        )

    # -- crash-safety sweep -----------------------------------------------------

    @staticmethod
    async def reap_orphans(
        docker_runner: Optional[DockerRunner] = None, *, max_age_sec: int = 3600
    ) -> int:
        """Force-remove ``polyrob.sandbox=1``-labeled containers started at least
        ``max_age_sec`` ago.

        A process that dies without calling ``teardown()`` leaves its persistent
        sandbox container running — this is the crash-safety backstop, meant to run
        once at process start (documented call site: ``core/autonomy_runtime.py::
        start_autonomy`` — not wired there by this change; see the P1-B report).
        Best-effort and fail-open: never raises, and a start-time parse failure for
        any one container means that container is left alone (not removed) rather
        than risking removal of something still legitimately in use. Returns the
        number of containers actually removed.
        """
        runner = docker_runner or _default_docker_runner
        try:
            code, out, err = await runner(["ps", "-aq", "--filter", f"label={_SANDBOX_LABEL}"])
        except Exception:
            logger.warning("reap_orphans: 'docker ps' raised", exc_info=True)
            return 0
        if code != 0:
            logger.warning("reap_orphans: 'docker ps' exited %s: %s", code, err)
            return 0
        cids = [c for c in (out or "").split() if c.strip()]
        if not cids:
            return 0

        try:
            icode, iout, ierr = await runner(["inspect", "-f", "{{.State.StartedAt}}", *cids])
        except Exception:
            logger.warning("reap_orphans: 'docker inspect' raised", exc_info=True)
            return 0
        if icode != 0:
            logger.warning("reap_orphans: 'docker inspect' exited %s: %s", icode, ierr)
            return 0

        started_lines = (iout or "").splitlines()
        now = time.time()
        removed = 0
        for i, cid in enumerate(cids):
            raw_ts = started_lines[i] if i < len(started_lines) else ""
            age = _age_from_docker_timestamp(raw_ts, now)
            if age is None:
                logger.warning(
                    "reap_orphans: could not parse start time for %s (%r); leaving it", cid, raw_ts
                )
                continue
            if age < max_age_sec:
                continue
            try:
                rcode, _rout, rerr = await runner(["rm", "-f", cid])
            except Exception:
                logger.warning("reap_orphans: 'rm -f %s' raised", cid, exc_info=True)
                continue
            if rcode == 0:
                removed += 1
            else:
                logger.warning("reap_orphans: 'rm -f %s' exited %s: %s", cid, rcode, rerr)
        return removed
