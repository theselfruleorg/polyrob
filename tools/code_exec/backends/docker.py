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
import stat
import tempfile
import time
import threading
import uuid
from typing import Awaitable, Callable, List, Optional, Tuple

from tools.code_exec.backend import ExecutionBackend, ExecutionBackendError
from tools.code_exec.backends._proc import run_group
from tools.code_exec.env_policy import SECRET_PAT, build_child_env
from tools.code_exec.limits import exec_timeout_cap, max_output_bytes, max_timeout_sec
from tools.code_exec.result import ExecutionRequest, ExecutionResult

logger = logging.getLogger(__name__)

_PY = ("python", "python3", "py")
_SH = ("bash", "sh", "shell")


def widen_mode_for_container(mode: int) -> int:
    """The permission bits a host-created file needs so the forced-unprivileged
    container uid can edit it — derived from the file's OWN mode, never a flat
    0o666.

    A file the host wrote 0o644 becomes 0o666; one written 0o755 becomes 0o777, so
    a script the agent generated stays runnable. A file with no owner-exec bit
    never GAINS one (widening permissions must not turn data into something the
    sandbox can execute). Only the low nine bits are touched — setuid/setgid/sticky
    are never propagated.
    """
    mode = stat.S_IMODE(mode)
    widened = mode | 0o066                     # group + other: read & write
    if mode & stat.S_IXUSR:
        widened |= 0o011                       # keep an executable file executable
    return widened


def _chmod_nofollow(path: str, mode: int, *, want_dir: bool) -> None:
    """chmod *path* WITHOUT ever following a symlink (H10).

    Opens the entry with ``O_NOFOLLOW`` (a symlink swapped in after the caller's
    ``lstat`` makes the open fail with ELOOP), re-checks the type on the fd, and
    ``fchmod``s the fd — so the mode can only land on the inode that was opened.
    Linux's ``os.chmod(..., follow_symlinks=False)`` raises NotImplementedError,
    hence the fd route rather than that flag. Raises ``OSError`` on refusal.
    """
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    flags = os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0)
    if want_dir:
        flags |= getattr(os, "O_DIRECTORY", 0)
    if not nofollow:  # platform without O_NOFOLLOW: fall back to an lstat gate
        if stat.S_ISLNK(os.lstat(path).st_mode):
            raise OSError(f"refusing to chmod through a symlink: {path}")
        os.chmod(path, mode)
        return
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        ok = stat.S_ISDIR(st.st_mode) if want_dir else stat.S_ISREG(st.st_mode)
        if not ok:
            raise OSError(f"refusing to chmod unexpected file type: {path}")
        os.fchmod(fd, mode)
    finally:
        os.close(fd)


def _install_root() -> str:
    """Host root for the dev-mode ``/install`` dirs — OUTSIDE every bind-mounted
    workspace (H02). ``<data_home>/sandbox_installs``; a tempdir fallback (per
    uid) only when the data home cannot be resolved."""
    try:
        from core.runtime_paths import effective_data_home
        return os.path.join(str(effective_data_home()), "sandbox_installs")
    except Exception:
        uid = os.geteuid() if hasattr(os, "geteuid") else "u"
        return os.path.join(tempfile.gettempdir(), f"polyrob_sandbox_installs_{uid}")


def _rmtree_install_dir(path: str) -> None:
    """Remove a throwaway install dir; never follow it if it became a link."""
    try:
        if os.path.islink(path):
            os.unlink(path)
        else:
            shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


#: Label applied to every persistent container this backend creates — the marker
#: ``reap_orphans`` filters ``docker ps`` on.
_SANDBOX_LABEL = "polyrob.sandbox=1"
#: 073 W8: CODE_EXEC_NETWORK=proxy — the allowlist egress sidecar.
_PROXY_POLICY = "proxy"
_EGRESS_LABEL = "polyrob.egress=1"
_EGRESS_HOST = "polyrob-egress"
_EGRESS_PORT = 3128

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


def docker_binary() -> str:
    """The container CLI every docker-backend call shells out to (073 W6):
    ``CODE_EXEC_DOCKER_BINARY`` (default ``docker``; e.g. ``podman``). Read per
    call. A value that begins with ``-`` is refused (it would read as an option)."""
    raw = (os.getenv("CODE_EXEC_DOCKER_BINARY") or "").strip()
    if not raw:
        return "docker"
    if raw.startswith("-"):
        raise ExecutionBackendError(
            f"CODE_EXEC_DOCKER_BINARY value {raw!r} begins with '-'; set a binary name or path.")
    return raw


def docker_binary_is_daemonless() -> bool:
    """True for Podman: no daemon socket to probe (``sandbox_guard``)."""
    try:
        return os.path.basename(docker_binary()).startswith("podman")
    except ExecutionBackendError:
        return False


def docker_reuse_across_restart_enabled() -> bool:
    """``CODE_EXEC_DOCKER_REUSE_ACROSS_RESTART`` (default OFF, 073 W6): a persistent
    session container is labelled with its config hash and RE-ATTACHED by a later
    process for the same session instead of re-created. Still one container per
    session — never shared across sessions (073 §7)."""
    from core.env import bool_env
    return bool_env("CODE_EXEC_DOCKER_REUSE_ACROSS_RESTART", False)


def _tool_rpc_on() -> bool:
    """073 W9's per-setup RPC bind makes a container non-re-attachable."""
    try:
        from tools.code_exec.tool_rpc import tool_rpc_enabled
        return bool(tool_rpc_enabled())
    except Exception:
        return False


#: Labels of a re-attachable persistent container (only while the flag is on).
_REUSE_LABEL = "polyrob.reuse=1"
_CONFIG_LABEL_KEY = "polyrob.config"


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
    argv = [docker_binary()] + list(args)
    stdin_bytes = input.encode() if input is not None else None

    code, out_text, err_text, timed_out = await run_group(
        argv, stdin_bytes=stdin_bytes, timeout=timeout, label="docker"
    )
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
        dev_mode: bool = False,
    ) -> None:
        # 014 B1: image resolution. Explicit CODE_EXEC_DOCKER_IMAGE always wins.
        # Dev mode (posture>=1 persistent dev container — the shell/coding/run_code-dev
        # path) defaults to a python+node image so JS/TS toolchains (npm/npx) work;
        # the ephemeral confined sandbox keeps the slim python-only default.
        _explicit_image = os.getenv("CODE_EXEC_DOCKER_IMAGE")
        if _explicit_image:
            self.image = _explicit_image
        elif dev_mode:
            self.image = os.getenv(
                "CODE_EXEC_DEV_IMAGE", "nikolaik/python-nodejs:python3.11-nodejs20")
        else:
            self.image = "python:3.12-slim"
        self.memory_mb = int(os.getenv("CODE_EXEC_CONTAINER_MEMORY_MB", "1024"))
        self.cpus = os.getenv("CODE_EXEC_CONTAINER_CPUS", "1.0")
        self.pids_limit = int(os.getenv("CODE_EXEC_PIDS_LIMIT", "256"))
        # Docker's default /dev/shm is 64MB regardless of --memory — the classic
        # cause of a headless-Chromium SIGTRAP/crashpad crash under Puppeteer/
        # Playwright/Remotion (they mmap frame buffers there; a plain page fetch
        # or bare `chromium --headless` run doesn't hit it, only a real render
        # does — exactly the shape of the 2026-08-27 release-video failure: four
        # render attempts crashed identically while bare headless chromium
        # worked). Not a security boundary (no cap/read-only/pids/memory relaxed).
        self.shm_size_mb = int(os.getenv("CODE_EXEC_SHM_SIZE_MB", "1024"))
        # 014 B3: explicit CODE_EXEC_MAX_TIMEOUT_SEC always wins. Unset: dev mode
        # follows the ONE foreground ceiling shell_run uses
        # (tools/code_exec/limits.py::dev_exec_max_timeout_sec — SHELL_MAX_TIMEOUT_SEC,
        # default 600, so an install fits) — pre-014 the backend re-clamp silently cut
        # shell foreground commands to 30s; the confined default stays 30.
        self.max_timeout = max_timeout_sec(dev_mode)
        self.max_output = max_output_bytes()
        # Container user precedence: explicit operator override (verbatim, even if root)
        # > non-root host uid:gid (keeps the mounted workspace writable) > forced-unprivileged
        # when the HOST process itself is root (prod systemd runs User=root — never let that
        # silently become uid 0 *inside* the sandbox container) > Windows fallback.
        env_user = os.getenv("CODE_EXEC_DOCKER_USER", "")
        # Set whenever the forced-unprivileged branch below fires: the container user
        # then can't be the host workspace dir's owner (it's owned by the root process
        # that created it), so the bind-mounted workspace needs an explicit best-effort
        # writable chmod (see `_ensure_workspace_writable`) or every in-container write
        # to /workspace hard-fails EACCES regardless of dev mode.
        self._workspace_needs_chmod = False
        if env_user:
            self.user = env_user
        elif hasattr(os, "getuid"):
            if os.getuid() == 0:
                self.user = "65534:65534"  # nobody:nogroup
                self._workspace_needs_chmod = True
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
        # WS-1 (compute posture): sandbox-dev entitlement for THIS backend instance.
        # Only affects the PERSISTENT container's mounts (a writable /install bind is
        # fixed at `docker run -d` time); the per-request python/-I-vs-/-s + env choice
        # rides on ExecutionRequest.dev_mode. Callers construct dev backends only for
        # sessions that passed compute_posture_allows(ctx, 1).
        self._dev_mode = bool(dev_mode)
        # 073 W9 (code calls tools): the host dir bound at /polyrob_rpc in the
        # PERSISTENT container (set at setup() only when CODE_EXEC_TOOL_CALLS is on).
        self._tool_rpc_host_dir: Optional[str] = None
        # 073 W8 (CODE_EXEC_NETWORK=proxy): the internal network + proxy sidecar
        # this PERSISTENT container sits behind (created at setup(), removed at
        # teardown()).
        self._egress_network: Optional[str] = None
        self._egress_sidecar: Optional[str] = None

    # -- lifecycle ------------------------------------------------------------

    async def setup(self) -> None:
        if self._session_id is None:
            # EPHEMERAL (P0, unchanged): fail fast with a clear error if the CLI is missing.
            binary = docker_binary()
            if shutil.which(binary) is None:
                raise ExecutionBackendError(
                    f"docker backend selected but the '{binary}' CLI was not found on PATH. "
                    "Install Docker (or Podman with CODE_EXEC_DOCKER_BINARY=podman) or set "
                    "CODE_EXEC_BACKEND to another backend."
                )
            return
        # PERSISTENT (P1-B, opt-in): start ONE long-lived container for this session.
        if self._container is not None:
            return  # idempotent — already set up
        async with self._setup_lock:
            if self._container is not None:  # lost a setup() race to another waiter
                return
            workdir = self._resolve_persistent_workdir()
            os.makedirs(workdir, exist_ok=True)  # as ephemeral does; lstat-checked below
            if self._workspace_needs_chmod:
                self._ensure_workspace_writable(workdir)
            network = self._resolve_setup_network()
            egress_flags: List[str] = []
            if network == _PROXY_POLICY:
                network, egress_flags = await self._egress_setup()
            container_name = f"polyrob-sbx-{uuid.uuid4().hex}"
            install_host = self._ensure_install_dir(workdir) if self._dev_mode else None
            # H02: verify every bind source right before the argv (raises).
            self._check_bind_source(workdir, "workspace")
            if install_host is not None:
                self._check_bind_source(install_host, "install dir")
            reuse_labels: List[str] = []
            if docker_reuse_across_restart_enabled() and not _tool_rpc_on() and not egress_flags:
                # 073 W6: re-attach to this session's running container from a
                # previous process when it was built with the SAME config (image,
                # mounts, network, caps, user) — never a container of another session.
                config = self._config_hash(self._hardening_flags(
                    network=network, workdir_host=workdir, install_host=install_host
                ) + self._publish_flags() + [self.image, "sleep", "infinity"])
                found = await self._find_reusable(config)
                if found:
                    self._workdir = workdir
                    self._container = found
                    logger.info("docker backend: re-attached session %s to container %s",
                                self._session_id, found[:12])
                    return
                reuse_labels = ["--label", _REUSE_LABEL,
                                "--label", f"{_CONFIG_LABEL_KEY}={config}"]
            argv = [
                "run", "-d",
                "--label", _SANDBOX_LABEL,
                "--label", f"polyrob.session={self._session_id}",
            ] + reuse_labels + [
                "--name", container_name,
            ] + self._hardening_flags(
                network=network, workdir_host=workdir, install_host=install_host
            ) + (egress_flags or self._publish_flags()) + self._tool_rpc_setup_flags() + [
                self.image, "sleep", "infinity",
            ]
            try:
                code, out, err = await self._docker(argv, timeout=self.max_timeout)
            except _DockerExecTimeout as e:
                raise ExecutionBackendError(
                    f"docker run -d (persistent sandbox) timed out: {e}"
                ) from e
            if code != 0:
                await self._egress_teardown()
                raise ExecutionBackendError(
                    f"docker run -d (persistent sandbox) failed (exit {code}): {err or out}"
                )
            self._workdir = workdir
            self._container = container_name

    def _config_hash(self, body: List[str]) -> str:
        """PURE: a short hash of everything a re-attached container must share
        with a fresh one (the ``docker run -d`` body: hardening flags, mounts,
        network, publish flags, image, command) plus the user and the binary."""
        import hashlib
        import json
        blob = json.dumps([docker_binary(), self.user, self._dev_mode, body])
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    async def _find_reusable(self, config: str) -> Optional[str]:
        """The id of a RUNNING container of this session with this config, or
        None. Any error -> None (create a new one)."""
        try:
            code, out, _err = await self._docker([
                "ps", "-q",
                "--filter", f"label={_SANDBOX_LABEL}",
                "--filter", f"label=polyrob.session={self._session_id}",
                "--filter", f"label={_REUSE_LABEL}",
                "--filter", f"label={_CONFIG_LABEL_KEY}={config}",
                "--filter", "status=running",
            ], timeout=self.max_timeout)
        except Exception:
            logger.debug("docker backend: reuse probe failed", exc_info=True)
            return None
        if code != 0:
            return None
        ids = [c for c in (out or "").split() if c.strip()]
        return ids[0] if ids else None

    async def teardown(self) -> None:
        if self._session_id is None:
            return  # EPHEMERAL (P0, unchanged): no-op — nothing persists to clean up.
        if self._container is None:
            return  # idempotent — never set up, or already torn down
        cname = self._container
        self._container = None  # mark torn down even if the rm call below errors
        if self._tool_rpc_host_dir:  # 073 W9: the persistent tool-RPC socket dir
            shutil.rmtree(self._tool_rpc_host_dir, ignore_errors=True)
            self._tool_rpc_host_dir = None
        try:
            code, _out, err = await self._docker(["rm", "-f", cname], timeout=self.max_timeout)
            if code != 0 and "no such container" not in (err or "").lower():
                logger.warning("docker backend: teardown 'rm -f %s' exited %s: %s", cname, code, err)
        except _DockerExecTimeout:
            logger.warning("docker backend: teardown 'rm -f %s' timed out", cname)
        except Exception:
            logger.warning("docker backend: teardown 'rm -f %s' raised", cname, exc_info=True)
        await self._egress_teardown()

    # -- 073 W8: the egress allowlist proxy ------------------------------------

    async def _egress_setup(self):
        """Create an ``--internal`` network (no route out) and ONE proxy sidecar on
        it + the default bridge; return ``(network, extra docker-run flags)`` for the
        session container. The sidecar runs ``tools/code_exec/egress_proxy.py`` with
        the image's own python3, read-only, all caps dropped, as nobody. Any failure
        tears down what was made and raises — never a silent open network."""
        from tools.code_exec.egress_proxy import parse_allowlist, parse_ports
        tag = uuid.uuid4().hex[:12]
        net = f"polyrob-egress-{tag}"
        side = f"polyrob-egp-{tag}"
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "egress_proxy.py")
        allow = ",".join(parse_allowlist(os.getenv("CODE_EXEC_EGRESS_ALLOW")))
        ports = ",".join(str(p) for p in sorted(parse_ports(os.getenv("CODE_EXEC_EGRESS_PORTS"))))
        session_label = f"polyrob.session={self._session_id}"
        steps = [
            ["network", "create", "--internal", "--label", _SANDBOX_LABEL,
             "--label", _EGRESS_LABEL, "--label", session_label, net],
            ["run", "-d", "--name", side, "--label", _SANDBOX_LABEL, "--label", session_label,
             "--network", "bridge", "--read-only", "--cap-drop", "ALL",
             "--security-opt", "no-new-privileges", "--user", "65534:65534",
             "--pids-limit", "64", "--memory", "128m",
             "--mount", f"type=bind,src={script},dst=/polyrob_egress_proxy.py,readonly",
             self.image, "python3", "-I", "/polyrob_egress_proxy.py",
             str(_EGRESS_PORT), allow, ports],
            ["network", "connect", "--alias", _EGRESS_HOST, net, side],
        ]
        self._egress_network = net
        for argv in steps:
            if argv[0] == "run":
                self._egress_sidecar = side
            try:
                code, out, err = await self._docker(argv, timeout=self.max_timeout)
            except Exception as e:
                code, out, err = 1, "", str(e)
            if code != 0:
                await self._egress_teardown()
                raise ExecutionBackendError(
                    f"CODE_EXEC_NETWORK=proxy: could not set up the egress proxy "
                    f"({' '.join(argv[:2])} exited {code}: {err or out}). The sandbox was "
                    "NOT started with an open network instead.")
        url = f"http://{_EGRESS_HOST}:{_EGRESS_PORT}"
        flags: List[str] = []
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            flags += ["-e", f"{k}={url}"]
        flags += ["-e", "NO_PROXY=localhost,127.0.0.1", "-e", "no_proxy=localhost,127.0.0.1"]
        return net, flags

    async def _egress_teardown(self) -> None:
        side, net = self._egress_sidecar, self._egress_network
        self._egress_sidecar = self._egress_network = None
        for argv in ([["rm", "-f", side]] if side else []) + ([["network", "rm", net]] if net else []):
            try:
                await self._docker(argv, timeout=self.max_timeout)
            except Exception:
                logger.warning("docker backend: egress teardown %s failed", argv[:2], exc_info=True)

    @property
    def capabilities(self):
        default_net = (os.getenv("CODE_EXEC_NETWORK", "none") or "none").lower()
        if default_net == _PROXY_POLICY and self._session_id is None:
            default_net = "none"
        return {
            "network": default_net not in ("none", ""),
            "isolation": "container",
            "sandbox": True,
        }

    # -- helpers --------------------------------------------------------------

    def _clamp_timeout(self, t, ceiling=None) -> float:
        cap = exec_timeout_cap(self.max_timeout, ceiling)
        if t is None:
            return cap
        return max(1.0, min(float(t), cap))

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

    def effective_setup_network(self) -> str:
        """The network a run/exec on THIS backend actually experiences (014 B2).

        Persistent mode: the container's own network — ``docker exec`` inherits
        it, including the dev-mode auto-bridge below. Ephemeral mode: the
        per-request policy (env-driven; no auto-bridge — every ``docker run``
        resolves its own ``--network``). The run_code ``packages=`` gate probes
        this instead of the raw env so a dev container that auto-bridged is not
        wrongly refused pip installs.
        """
        if self._session_id is not None:
            return self._resolve_setup_network()
        return self._resolve_network(ExecutionRequest(language="bash", code="true"))

    def _resolve_setup_network(self) -> str:
        """Network for the persistent CONTAINER at ``docker run -d`` time.

        A dev sandbox (posture>=1) is a NETWORKED dev environment by the posture-1
        contract: importable pip installs AND port-publish (WS-4) both require egress,
        and docker SILENTLY IGNORES ``-p`` under ``--network none``. So a dev container
        defaults to ``bridge`` when ``CODE_EXEC_NETWORK`` is unset; an explicit value
        (incl. ``none``) still wins. A non-dev persistent container is unchanged (uses
        the plain policy, default ``none``).
        """
        if self._dev_mode and os.getenv("CODE_EXEC_NETWORK") is None:
            return "bridge"
        if (os.getenv("CODE_EXEC_NETWORK") or "").strip().lower() == _PROXY_POLICY:
            return _PROXY_POLICY  # setup() builds the internal network + sidecar
        return self._resolve_network(ExecutionRequest(language="bash", code="true"))

    def _resolve_network(self, request: ExecutionRequest) -> str:
        """Map policy -> docker --network value. Never silently fall back to host."""
        policy = (request.network or os.getenv("CODE_EXEC_NETWORK", "none") or "none").lower()
        if policy in ("none", ""):
            return "none"
        if policy == "host":
            return "host"
        if policy == _PROXY_POLICY:
            # 073 W8: the proxy is a PERSISTENT-sandbox feature (one sidecar per
            # session). A one-shot run gets no network rather than an open one.
            logger.info("CODE_EXEC_NETWORK=proxy: ephemeral run has no network "
                        "(the allowlist proxy serves persistent session sandboxes)")
            return "none"
        if policy in ("egress", "bridge"):
            # 'bridge' (the docker network name) aliases 'egress' — an operator who
            # sets it means outbound-allowed, and silently degrading to no-network
            # strands agent pip installs with confusing DNS errors (prod 2026-07-06).
            return "bridge"  # outbound allowed, no host namespace; operator adds egress proxy
        logger.warning("CODE_EXEC_NETWORK policy %r unknown — denying network (use none|egress|host)",
                       policy)
        return "none"

    def _hardening_flags(
        self, *, network: str, workdir_host: str, install_host: Optional[str] = None
    ) -> List[str]:
        """PURE: the ONE hardening-flag list shared by the ephemeral ``docker run
        --rm`` and the persistent container's ``docker run -d``. Since 032 the list
        itself lives in ``core/container_hardening.py::hardening_flags`` (the
        app-service supervisor runs the same flags from its own process); this
        method binds the backend's limits to it. Byte-identical to the pre-lift argv.

        ``install_host`` (WS-1, sandbox-dev ONLY — callers pass it only for a
        posture-entitled dev run): bind an additional writable ``/install`` dir for
        importable pip installs. This is the ONLY mount relaxation dev mode gets —
        the rootfs stays ``--read-only`` and every cap/pid/memory/user flag is
        unchanged. ``None`` (the default) is byte-identical to the pre-WS-1 argv.
        """
        from core.container_hardening import hardening_flags
        return hardening_flags(
            network=network, workdir_host=workdir_host, install_host=install_host,
            pids_limit=self.pids_limit, memory_mb=self.memory_mb,
            shm_size_mb=self.shm_size_mb, cpus=self.cpus, user=self.user,
        )

    #: Env a sandbox-dev run gets by default: HOME points at /install (NOT /workspace —
    #: /workspace is the host bind-mount, owned by whatever host user ran docker, and
    #: the container runs as an unprivileged fixed uid that is neither its owner nor
    #: group, so it has no write bit there; every npm/pip/etc. tool that stages its
    #: cache/config under $HOME then hard-fails EACCES. /install is the ONE dir this
    #: backend guarantees 0o777 for exactly this reason (`_ensure_install_dir`)).
    #: PYTHONPATH so /install packages import under `python -s`, and PIP_TARGET so a
    #: bare `pip install X` lands in /install without the agent spelling --target.
    _DEV_ENV_DEFAULTS = {
        "HOME": "/install",
        "PYTHONPATH": "/install",
        "PIP_TARGET": "/install",
    }

    def _container_env_flags(self, request: ExecutionRequest) -> List[str]:
        """PURE: ``-e KEY=VALUE`` flags for the caller-supplied, secret-scrubbed env.
        Shared by the ephemeral ``docker run`` and persistent ``docker exec`` argv
        builders so the scrub logic can't drift between the two paths.

        WS-1: a ``dev_mode`` request additionally gets :data:`_DEV_ENV_DEFAULTS`
        (caller env wins on key collision). The secret-name scrub applies to the
        MERGED map, and a non-dev request is byte-identical to before.
        """
        merged: dict = {}
        if request.dev_mode:
            merged.update(self._DEV_ENV_DEFAULTS)
        merged.update(request.env or {})
        # The agent-child marker goes in last: a polyrob CLI inside the sandbox
        # image refuses its owner-only verbs (core.security.agent_child).
        from core.security.agent_child import agent_child_env_flags
        merged.update(agent_child_env_flags())
        flags: List[str] = []
        for k, v in merged.items():
            if SECRET_PAT.search(k):
                continue  # never let a caller smuggle a secret-named var in
            flags += ["-e", f"{k}={v}"]
        return flags

    def _publish_ports(self) -> List[int]:
        """Container ports to publish to host loopback in dev persistent mode (WS-4).

        Configured via ``CODE_EXEC_PUBLISH_PORTS`` (comma list; default the common dev
        server ports). Only consulted for a dev persistent container — an ephemeral
        ``--rm`` run is not a server and never publishes.
        """
        raw = os.getenv("CODE_EXEC_PUBLISH_PORTS", "8000,5000,8080,3000")
        ports: List[int] = []
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                p = int(part)
            except ValueError:
                continue
            if 1 <= p <= 65535 and p not in ports:
                ports.append(p)
        return ports

    def _publish_flags(self) -> List[str]:
        """``-p 127.0.0.1::<cport>`` for each dev-published port (host port
        docker-assigned/ephemeral, bound to LOOPBACK only — never 0.0.0.0). Empty for
        a non-dev backend, so posture-0 / non-dev setup is byte-identical."""
        if not self._dev_mode:
            return []
        flags: List[str] = []
        for cport in self._publish_ports():
            flags += ["-p", f"127.0.0.1::{cport}"]
        return flags

    async def published_ports(self) -> dict:
        """Map ``{container_port: host_port}`` for this dev container's published
        loopback ports (via ``docker port``). Empty for a non-dev / not-yet-setup
        container. Never raises — a lookup failure degrades to ``{}``."""
        if not self._dev_mode or self._container is None:
            return {}
        try:
            code, out, _err = await self._docker(["port", self._container], timeout=self.max_timeout)
        except Exception:
            return {}
        if code != 0:
            return {}
        mapping: dict = {}
        for line in (out or "").splitlines():
            # "8000/tcp -> 127.0.0.1:49153"
            line = line.strip()
            if "->" not in line or "/" not in line:
                continue
            left, _, right = line.partition("->")
            cport_s = left.strip().split("/")[0].strip()
            hostpart = right.strip().rsplit(":", 1)
            if len(hostpart) != 2:
                continue
            try:
                mapping[int(cport_s)] = int(hostpart[1].strip())
            except ValueError:
                continue
        return mapping

    @staticmethod
    def _ensure_workspace_writable(workdir_host: str) -> None:
        """Best-effort chmod so the FORCED-unprivileged container uid (65534, see
        `_workspace_needs_chmod`) can write the host-created tree — DIRECTORIES so
        it can traverse and create, and the FILES inside them so it can edit what a
        host-side tool wrote.

        The file half was missing until 2026-09-08 and it was the expensive half:
        prod runs the service as root, so every file the coding/filesystem tool
        wrote landed root-owned 0644 and an in-container `shell_run` editing it
        hard-failed EACCES (9,246 such files under the live project tree; the
        journal shows ship-software runs burning steps on `PermissionError:
        '/workspace/rob-status/trackrecord.html'`). Widening is derived from the
        file's OWN mode (`widen_mode_for_container`) rather than a flat 0o666, so a
        script stays executable and a non-executable file never gains an exec bit.

        Only root-owned entries are touched, and only within the directories this
        walk already visits — a directory owned by the container's own uid is
        pruned (it and its subtree are already writable by that uid), which is what
        keeps this cheap once a large `node_modules` exists.

        SECURITY (H10, 2026-09-23): this runs as ROOT over a tree the sandbox can
        write, so it NEVER follows a symlink — every decision is an ``lstat``, a
        symlinked dir/file is skipped (and never descended), and the chmod itself
        goes through ``_chmod_nofollow`` (``O_NOFOLLOW`` open + ``fchmod``), so a
        link swapped in between the ``lstat`` and the chmod cannot redirect the
        0o777 onto a host path outside the workspace.
        """
        try:
            st = os.lstat(workdir_host)
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                logger.warning("refusing to chmod workspace root that is a symlink or "
                               "not a directory: %s", workdir_host)
                return
            _chmod_nofollow(workdir_host, 0o777, want_dir=True)
        except Exception:
            logger.warning("could not chmod workspace dir writable: %s", workdir_host, exc_info=True)
            return
        for root, dirs, files in os.walk(workdir_host, topdown=True, followlinks=False):
            keep = []
            for d in dirs:
                path = os.path.join(root, d)
                try:
                    st = os.lstat(path)          # lstat: never follow a symlink out
                    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                        continue                 # never chmod through, never descend
                    if st.st_uid == 0:
                        _chmod_nofollow(path, 0o777, want_dir=True)
                        keep.append(d)
                    # else: already owned by a non-root (container) uid — that uid
                    # can already write its own subtree, so don't descend further.
                except Exception:
                    logger.warning("could not chmod workspace subdir writable: %s", path, exc_info=True)
                    try:
                        if not stat.S_ISLNK(os.lstat(path).st_mode):
                            keep.append(d)  # best-effort — still try descendants
                    except Exception:
                        pass
            dirs[:] = keep
            for f in files:
                path = os.path.join(root, f)
                try:
                    st = os.lstat(path)          # lstat: never follow a symlink out
                    if not stat.S_ISREG(st.st_mode) or st.st_uid != 0:
                        continue
                    mode = stat.S_IMODE(st.st_mode)
                    widened = widen_mode_for_container(mode)
                    if widened != mode:
                        _chmod_nofollow(path, widened, want_dir=False)
                except Exception:
                    logger.warning("could not chmod workspace file writable: %s", path, exc_info=True)

    @staticmethod
    def _install_dir_path(workdir_host: str) -> str:
        """PURE: the host dir bind-mounted at ``/install`` for a dev run in *workdir_host*.

        SECURITY (H02, 2026-09-23): this used to be ``<workspace>/.pylibs`` — INSIDE
        the tree the sandbox can write. Sandbox code could replace it with a symlink
        to any host path, and the next ``docker run`` (as root) bind-mounted the
        HOST target read-write: a sandbox escape. It now lives OUTSIDE every
        bind-mounted tree, under ``<data_home>/sandbox_installs/<key>``, keyed by the
        workspace path so run_code and shell_run on one workspace still share their
        installs, and so installs survive container reaps with the session.
        """
        import hashlib
        key = hashlib.sha256(os.path.abspath(workdir_host).encode("utf-8", "surrogateescape")).hexdigest()[:32]
        return os.path.join(_install_root(), key)

    @staticmethod
    def _ensure_install_dir(workdir_host: str) -> str:
        """Ensure + return the host dir bind-mounted at ``/install`` for dev mode
        (see ``_install_dir_path`` for WHERE and why it is outside the workspace).

        Mode 0o777 because the container user (e.g. forced 65534:65534 when the
        host process is root) is generally NOT the host owner — without it, pip
        inside the container can't write and dev mode dies with EACCES. The parent
        root is 0o700 and host-owned; the dir is created with ``mkdir`` (never
        through an existing link) and chmodded through an ``O_NOFOLLOW`` fd. The
        caller still runs ``_check_bind_source`` right before building the argv.
        """
        path = DockerBackend._install_dir_path(workdir_host)
        parent = os.path.dirname(path)
        try:
            try:
                os.makedirs(parent, mode=0o700, exist_ok=True)
            except FileExistsError:
                pass
            pst = os.lstat(parent)
            if stat.S_ISLNK(pst.st_mode) or not stat.S_ISDIR(pst.st_mode):
                logger.warning("dev-mode: install root %s is a symlink or not a directory", parent)
                return path
            try:
                os.mkdir(path, 0o777)
            except FileExistsError:
                pass
            _chmod_nofollow(path, 0o777, want_dir=True)
        except Exception:
            logger.warning("dev-mode: could not prepare install dir %s", path, exc_info=True)
        return path

    # -- 073 W9: code calls tools (tools/code_exec/tool_rpc.py) ------------------
    # The script reaches the agent's per-run Unix socket through ONE extra
    # read-only bind at /polyrob_rpc (a socket on a read-only mount still accepts
    # connect()). Ephemeral: the per-run dir from ExecutionRequest.tool_rpc_dir.
    # Persistent: mounts are fixed at `docker run -d`, so ONE dir is bound at
    # setup when CODE_EXEC_TOOL_CALLS is on, and each run puts its socket in it.
    # Nothing else in the hardening list changes.

    @staticmethod
    def _tool_rpc_bind_flags(host_dir: Optional[str]) -> List[str]:
        if not host_dir:
            return []
        from tools.code_exec.tool_rpc import CONTAINER_RPC_DIR
        if "," in host_dir:
            raise ValueError(f"tool RPC dir must not contain a comma: {host_dir!r}")
        return ["--mount", f"type=bind,src={host_dir},dst={CONTAINER_RPC_DIR},readonly"]

    def _tool_rpc_setup_flags(self) -> List[str]:
        from tools.code_exec.tool_rpc import make_socket_dir, tool_rpc_enabled
        if not tool_rpc_enabled():
            return []
        host_dir = make_socket_dir(owner=self.user)
        self._check_bind_source(host_dir, "tool RPC dir")
        self._tool_rpc_host_dir = host_dir
        return self._tool_rpc_bind_flags(host_dir)

    @property
    def tool_rpc_host_dir(self) -> Optional[str]:
        return self._tool_rpc_host_dir

    @staticmethod
    def _check_bind_source(path: str, what: str) -> None:
        """Refuse a bind-mount source that the sandbox could have swapped (H02).

        Run right before a ``docker run`` argv is built: the daemon (root) resolves
        the source path itself, so a symlink here would mount its HOST target
        read-write into the container. Refuses a symlink, a non-directory, and a
        directory not owned by this process's euid (or root). Raises
        ``ExecutionBackendError`` — never mount something we did not verify.
        """
        try:
            st = os.lstat(path)
        except OSError as e:
            raise ExecutionBackendError(
                f"refusing to bind-mount {what} {path!r}: cannot lstat it ({e})"
            ) from e
        if stat.S_ISLNK(st.st_mode):
            raise ExecutionBackendError(
                f"refusing to bind-mount {what} {path!r}: it is a symlink"
            )
        if not stat.S_ISDIR(st.st_mode):
            raise ExecutionBackendError(
                f"refusing to bind-mount {what} {path!r}: it is not a directory"
            )
        euid = os.geteuid() if hasattr(os, "geteuid") else None
        if euid is not None and st.st_uid not in (euid, 0):
            raise ExecutionBackendError(
                f"refusing to bind-mount {what} {path!r}: owned by uid {st.st_uid}, "
                f"expected {euid}"
            )

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

    def _build_run_argv(
        self,
        request: ExecutionRequest,
        workdir: str,
        *,
        container_name: Optional[str] = None,
        timeout_sec: Optional[float] = None,
    ) -> List[str]:
        """PURE: the full ``docker run`` argv incl. the in-container command.

        EPHEMERAL mode only (see ``_run_persistent`` for the persistent ``docker
        exec`` argv, which shares only ``_hardening_flags``/``_container_env_flags``).

        SECURITY (P0 finalization): ``container_name`` + ``timeout_sec`` close the
        orphaned-container leak. Killing the local ``docker run`` CLI on a host-side
        timeout does NOT stop the container on the daemon, and the old ephemeral argv
        carried no ``--name``/``--label`` so the leak was neither reap-able nor
        removable. Passing a name+label makes it ``docker rm -f``-able and
        ``reap_orphans()``-sweepable; wrapping the command in coreutils
        ``timeout --signal=KILL <n>`` (as the persistent path does) makes the
        CONTAINER self-terminate — with ``--rm`` it is then auto-removed. When both
        are ``None`` (e.g. the pure argv unit tests) the argv is byte-identical to
        the pre-fix P0 shape.
        """
        lang = (request.language or "").lower()
        argv = [docker_binary(), "run", "--rm"]
        if container_name:
            argv += ["--name", container_name, "--label", _SANDBOX_LABEL,
                     "--label", "polyrob.ephemeral=1"]
        argv += self._hardening_flags(
            network=self._resolve_network(request), workdir_host=workdir,
            # dev run: bind the per-workspace install dir (OUTSIDE the workspace —
            # H02) as the writable /install (path built here purely; _run_ephemeral
            # pre-creates + verifies it before invoking).
            install_host=self._install_dir_path(workdir) if request.dev_mode else None,
        )
        argv += self._tool_rpc_bind_flags(getattr(request, "tool_rpc_dir", None))
        if request.stdin is not None:
            argv.append("-i")  # keep stdin open
        argv += self._container_env_flags(request)
        argv.append(self.image)
        if timeout_sec is not None:
            # In-container bound (the real kill path). --signal=KILL: untrusted code
            # must not catch/ignore it. timeout puts the command in its own process
            # group and kills the whole group.
            argv += ["timeout", "--signal=KILL", str(timeout_sec)]
        if lang in _PY:
            # dev mode: `-s` (no user-site) instead of `-I` — `-I` implies -E and
            # would ignore the PYTHONPATH=/install that makes installs importable.
            # Posture-0 keeps `-I` byte-identical (load-bearing isolation).
            argv += ["python", "-s" if request.dev_mode else "-I", "-c", request.code]
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
        timeout = self._clamp_timeout(request.timeout, getattr(request, "ceiling", None))
        workdir = request.workdir or tempfile.mkdtemp(prefix="rob_docker_")
        created_tmp = request.workdir is None
        os.makedirs(workdir, exist_ok=True)
        if self._workspace_needs_chmod:
            self._ensure_workspace_writable(workdir)
        install_dir = self._ensure_install_dir(workdir) if request.dev_mode else None
        try:
            # H02: verify every bind source right before the argv — never mount a
            # symlink (the daemon would mount its HOST target read-write).
            self._check_bind_source(workdir, "workspace")
            if install_dir is not None:
                self._check_bind_source(install_dir, "install dir")
            if getattr(request, "tool_rpc_dir", None):  # 073 W9
                self._check_bind_source(request.tool_rpc_dir, "tool RPC dir")
        except ExecutionBackendError as e:
            if created_tmp:
                shutil.rmtree(workdir, ignore_errors=True)
                if install_dir is not None:
                    _rmtree_install_dir(install_dir)
            return ExecutionResult(stderr=str(e), exit_code=2, backend=self.name)
        # P0 finalization: a named+labeled container with an IN-CONTAINER timeout so a
        # host-side kill can no longer orphan a still-running container on the daemon.
        container_name = f"polyrob-sbx-{uuid.uuid4().hex}"
        argv = self._build_run_argv(
            request, workdir, container_name=container_name, timeout_sec=timeout,
        )
        env = build_child_env({})  # env for the docker CLI process itself (PATH/HOME only)
        binary = docker_binary()
        stdin_bytes = (request.stdin or "").encode() if request.stdin else None
        # Host-side wait is a BACKSTOP only (a hung docker CLI client) — deliberately
        # looser than the in-container `timeout` so the container self-terminates first.
        host_backstop = timeout + 5
        start = time.monotonic()

        stop = threading.Event()

        def _run_sync():
            import subprocess
            from tools.code_exec.backends.bounded_capture import capture

            def kill(proc):
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError, AttributeError):
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass

            try:
                proc = subprocess.Popen(
                    argv, env=env,
                    stdin=subprocess.PIPE if stdin_bytes is not None else subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    start_new_session=True,
                )
            except Exception as e:
                return b"", f"docker launch error: {type(e).__name__}: {e}".encode(), 1, False, False
            remove = True
            try:
                out, err, timed_out, truncated = capture(
                    proc, data=stdin_bytes, timeout=host_backstop, limit=self.max_output,
                    stop=stop, kill=kill,
                )
                remove = timed_out or truncated or stop.is_set()
                return out, err, proc.returncode, timed_out, truncated
            finally:
                if remove:
                    # Stopping the CLI does not stop the container on the daemon.
                    try:
                        subprocess.run(
                            [binary, "rm", "-f", container_name],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            timeout=10, env=env,
                        )
                    except Exception:
                        logger.warning("sandbox cleanup failed; orphan reaper must retry")

        try:
            loop = asyncio.get_event_loop()
            future = loop.run_in_executor(None, _run_sync)
            try:
                stdout, stderr, exit_code, host_timed_out, truncated = await asyncio.shield(future)
            except asyncio.CancelledError:
                stop.set()
                await asyncio.shield(future)
                raise
        finally:
            if created_tmp:
                shutil.rmtree(workdir, ignore_errors=True)
                if install_dir is not None:
                    # A throwaway tempdir workspace's installs die with it (they
                    # used to live inside it, so the rmtree above took them).
                    _rmtree_install_dir(install_dir)
        # In-container `timeout --signal=KILL` exits 124/137 when it fires (the
        # container then self-removes via --rm); map that to timed_out too, since the
        # host wait returns normally in that case.
        timed_out = host_timed_out or (exit_code in _TIMEOUT_EXIT_CODES)

        out, t1 = self._cap(stdout)
        err, t2 = self._cap(stderr)
        return ExecutionResult(
            stdout=out, stderr=err, exit_code=exit_code, timed_out=timed_out,
            truncated=truncated or t1 or t2, duration_sec=time.monotonic() - start, backend=self.name,
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
        elif self._workspace_needs_chmod and self._workdir:
            # setup() already fixed the tree once; re-check per exec too — a
            # HOST-side tool (filesystem/coding) can scaffold new subdirectories
            # under this same workspace between calls on a long-lived session.
            self._ensure_workspace_writable(self._workdir)
        clamped_sec = self._clamp_timeout(request.timeout, getattr(request, "ceiling", None))

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
            # Same dev-vs-isolated choice as the ephemeral builder (see
            # _build_run_argv): `-s` + PYTHONPATH=/install only for a dev request.
            argv += ["python", "-s" if request.dev_mode else "-I", "-c", request.code]
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

    async def exec_detached(self, script: str) -> int:
        """PERSISTENT-only: launch ``script`` DETACHED in the session container
        (``docker exec -d``) so a background job (e.g. a server) survives the call.

        Used by the `shell` tool's background path (WS-2). Unlike ``run()`` there is
        NO in-container ``timeout`` wrapper — a background job is meant to outlive the
        call; its lifetime is bounded instead by the `process` tool's kill and by the
        container's own reaping. Raises if this backend isn't persistent (a detached
        job in an ephemeral ``--rm`` container would vanish immediately).
        """
        if self._session_id is None:
            raise ExecutionBackendError(
                "exec_detached requires a persistent (session-scoped) docker backend"
            )
        if self._container is None:
            await self.setup()
        argv = ["exec", "-d", "-w", "/workspace", self._container, "bash", "-c", script]
        try:
            code, _out, err = await self._docker(argv, timeout=self.max_timeout)
        except _DockerExecTimeout as e:
            raise ExecutionBackendError(f"docker exec -d (background) timed out: {e}") from e
        if code != 0:
            raise ExecutionBackendError(
                f"docker exec -d (background) failed (exit {code}): {err}"
            )
        return code

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

        # 073 W6: while re-attach is on, a re-attachable container outlives its
        # process ON PURPOSE; the cold-start age sweep leaves it to the
        # ownership-keyed reap_unowned.
        reuse = docker_reuse_across_restart_enabled()
        fmt = ("{{.State.StartedAt}}\t{{index .Config.Labels \"polyrob.reuse\"}}" if reuse
               else "{{.State.StartedAt}}")
        try:
            icode, iout, ierr = await runner(["inspect", "-f", fmt, *cids])
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
            raw_ts, _, reuse_label = raw_ts.partition("\t")
            if reuse and reuse_label.strip() == "1":
                continue
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
        # 073 W8: an egress network whose containers are gone (prune only removes
        # networks with no attached container, so a live session is never touched).
        try:
            await runner(["network", "prune", "-f", "--filter", f"label={_EGRESS_LABEL}"])
        except Exception:
            logger.debug("reap_orphans: egress network prune failed", exc_info=True)
        return removed

    @staticmethod
    async def reap_unowned(
        live_session_ids,
        docker_runner: Optional[DockerRunner] = None,
        *,
        min_age_sec: int = 900,
    ) -> int:
        """Force-remove sandbox containers whose owning session is GONE.

        This is the periodic companion to :meth:`reap_orphans`, and it exists
        because an age-based sweep cannot be run periodically: a chat session
        that is merely idle between turns legitimately outlives any sane
        ``max_age_sec``, so a recurring age sweep would kill live sessions. This
        one keys on OWNERSHIP instead — it removes a container only when the
        ``polyrob.session`` label names a session that is no longer resident in
        the SessionRegistry. An idle-but-live session is still resident, so it is
        never touched; an evicted or crashed session's container is.

        Why it is needed: only two paths release a container today — the full
        ``SessionCleanupMixin.cleanup`` and the one-shot autonomous run in
        ``run_as_session``. A resident chat session gets the PARTIAL cleanup
        (which deliberately keeps the container for continuous chat), so when its
        orchestrator is later evicted the container is simply abandoned. Live on
        prod 2026-08-24: 19 containers, the oldest 2 days old, on a disk at 80%.

        ``min_age_sec`` is a creation-race guard, not an expiry: a container
        younger than it is always left alone, so a session that is mid-creation
        (labeled but not yet registered) can never be swept out from under itself.

        Best-effort and fail-open: never raises. A container with no session
        label, or an unparseable start time, is LEFT ALONE — the failure mode of
        this sweep must be "leaked container", never "killed a live session".
        Returns the number of containers actually removed.
        """
        live = {str(s) for s in (live_session_ids or ()) if s}
        runner = docker_runner or _default_docker_runner
        try:
            code, out, err = await runner(["ps", "-aq", "--filter", f"label={_SANDBOX_LABEL}"])
        except Exception:
            logger.warning("reap_unowned: 'docker ps' raised", exc_info=True)
            return 0
        if code != 0:
            logger.warning("reap_unowned: 'docker ps' exited %s: %s", code, err)
            return 0
        cids = [c for c in (out or "").split() if c.strip()]
        if not cids:
            return 0

        fmt = '{{index .Config.Labels "polyrob.session"}}\t{{.State.StartedAt}}'
        try:
            icode, iout, ierr = await runner(["inspect", "-f", fmt, *cids])
        except Exception:
            logger.warning("reap_unowned: 'docker inspect' raised", exc_info=True)
            return 0
        if icode != 0:
            logger.warning("reap_unowned: 'docker inspect' exited %s: %s", icode, ierr)
            return 0

        lines = (iout or "").splitlines()
        now = time.time()
        removed = 0
        for i, cid in enumerate(cids):
            raw = lines[i] if i < len(lines) else ""
            sid, _, raw_ts = raw.partition("\t")
            sid = sid.strip()
            # No label (pre-label container, or a foreign one) -> leave it to the
            # cold-start age sweep. Never guess.
            if not sid or sid == "<no value>":
                continue
            if sid in live:
                continue
            age = _age_from_docker_timestamp(raw_ts.strip(), now)
            if age is None:
                logger.warning(
                    "reap_unowned: could not parse start time for %s (%r); leaving it",
                    cid, raw_ts,
                )
                continue
            if age < min_age_sec:
                continue  # creation-race guard
            try:
                rcode, _rout, rerr = await runner(["rm", "-f", cid])
            except Exception:
                logger.warning("reap_unowned: 'rm -f %s' raised", cid, exc_info=True)
                continue
            if rcode == 0:
                removed += 1
                logger.info(
                    "reap_unowned: removed sandbox container %s (session %s is gone)",
                    cid[:12], sid,
                )
            else:
                logger.warning("reap_unowned: 'rm -f %s' exited %s: %s", cid, rcode, rerr)
        return removed
