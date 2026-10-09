"""Process-global per-session sandbox-backend pool for the shell + process tools.

WHY THIS EXISTS (learned against real docker): a background job's pid is meaningful
ONLY inside the container it was launched in (pid namespaces are per-container). If the
`shell` tool (launch) and the `process` tool (poll/log/kill) each resolved their OWN
persistent `DockerBackend`, they'd get two DIFFERENT containers for the same session,
and `process poll` would `kill -0` a pid that doesn't exist in its container — reporting
a live server as "done". So both tools MUST share ONE persistent container per session.

This pool is that single source: keyed by session_id, dev-mode persistent backends,
created once (one container) and reused by every shell/process call for that session.
Installs still live on the shared `<workspace>/.pylibs` bind mount, so run_code's
installs remain visible here too.

073 W1: :func:`resolve_shell_executor` is the ONE selector between the container
and the host (posture 3). The host is never a fallback: a turn that may not use
the host gets the sandbox, and when the sandbox is unreachable it gets the honest
refusal — never the host.

073 W6: the selector also returns a REMOTE executor (``ssh``, or a pack backend
registered through ``tools/code_exec/pack_backends.py``) — only for an explicit
``SHELL_BACKEND=<name>``, never as a fallback, and on a server only when the
backend advertises ``capabilities["sandbox"] is True``
(``tools/code_exec/sandbox_guard.py::remote_shell_refusal``). Every executor
must satisfy ``tools/shell/executor_protocol.py::ShellExecutor``.
"""
from __future__ import annotations

import asyncio
import re
from typing import Dict, List, Optional, Tuple

_POOL: Dict[str, object] = {}
_HOST: Dict[str, object] = {}
#: (session id, kind) -> a RemoteShellExecutor (ssh / pack backends), 073 W6.
_REMOTE: Dict[Tuple[str, str], object] = {}
_LOCK = asyncio.Lock()

#: The built-in values. Any other backend NAME is a pack backend
#: (``tools/code_exec/pack_backends.py``); see :func:`shell_backend_choice`.
SHELL_BACKENDS = ("auto", "docker", "host", "ssh")
#: Kinds that never resolve through the remote path.
_LOCAL_KINDS = frozenset({"docker", "host"})
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class ShellBackendUnavailable(RuntimeError):
    """The shell tool could not resolve a persistent SANDBOX backend to run in."""


def _require_persistent_sandbox(backend) -> None:
    """Fail-closed unless ``backend`` is a persistent SANDBOX (CRITICAL guard).

    The shell tool advertises "commands run INSIDE the session's hardened container."
    But the backend is chosen by ``CODE_EXEC_BACKEND`` (default ``local_subprocess``),
    and a non-sandbox backend's ``run()`` executes on the HOST (root, on the prod
    systemd unit) — a silent sandbox escape by misconfig. So refuse anything that is
    not (a) sandbox-capable AND (b) session-persistent (``exec_detached`` for the
    background path + a bound ``_session_id`` so cwd/installs persist).
    """
    caps = {}
    try:
        caps = backend.capabilities or {}
    except Exception:
        caps = {}
    if caps.get("sandbox") is not True:
        raise ShellBackendUnavailable(
            "the shell tool requires a hardened SANDBOX backend, but the resolved "
            f"backend '{getattr(backend, 'name', type(backend).__name__)}' is not a "
            "sandbox (it would run on the host). Set CODE_EXEC_BACKEND=docker."
        )
    if not hasattr(backend, "exec_detached") or getattr(backend, "_session_id", None) is None:
        raise ShellBackendUnavailable(
            "the shell tool requires a PERSISTENT docker sandbox (for cwd/env/install "
            "persistence and background jobs). Set CODE_EXEC_BACKEND=docker and ensure "
            "CODE_EXEC_DOCKER_PERSISTENT is on (default at AGENT_COMPUTE_POSTURE>=1)."
        )


async def get_shell_backend(session_id: str):
    """Return the ONE dev persistent SANDBOX backend for ``session_id`` (create once).

    Fail-closed: raises :class:`ShellBackendUnavailable` if the configured backend is
    not a persistent sandbox (so the shell tool never silently runs on the host).
    """
    sid = session_id or "shell"
    backend = _POOL.get(sid)
    if backend is not None:
        return backend
    async with _LOCK:
        backend = _POOL.get(sid)
        if backend is None:
            from tools.code_exec import resolve_backend
            backend = resolve_backend(session_id=sid, dev_mode=True)
            _require_persistent_sandbox(backend)  # BEFORE setup() — never start a host backend
            await backend.setup()
            _POOL[sid] = backend
            # WS-4: register the container's published host loopback ports into the
            # narrow SSRF allowlist so the agent can HTTP-test its own server, without
            # a blanket BROWSER_ALLOW_PRIVATE_URLS opening. Fail-open (no publish → no
            # allow); never block backend resolution on this.
            try:
                from tools.shell.loopback_allow import allow_loopback_ports
                published = await backend.published_ports()
                if published:
                    allow_loopback_ports(published.values())
            except Exception:
                pass
        return backend


def peek_backend(session_id: str):
    """Return the pooled backend for ``session_id`` without creating one (or None)."""
    return _POOL.get(session_id or "shell")


async def teardown_session(session_id: str) -> None:
    """Tear down + drop the pooled backend for one session (best-effort).

    Also revokes that container's published host loopback ports from the SSRF
    allowlist so a stale ephemeral port a later unrelated bind reuses can't be
    silently reached (the allowlist is process-global; without this it grows
    unbounded and retains dead ports)."""
    # 073: the session's persistent python kernel (run_code persist=True) goes
    # with the rest of its execution state (the agents tier calls only this).
    try:
        from tools.code_exec.kernel import shutdown_kernel
        await shutdown_kernel(session_id)
    except Exception:
        pass
    sid = session_id or "shell"
    try:  # 073 W4: the session's job watchers end with it
        from tools.shell.watch import stop_watches
        stop_watches(sid)
    except Exception:
        pass
    for key in [k for k in _REMOTE if k[0] == sid]:
        remote = _REMOTE.pop(key, None)
        close = getattr(remote, "close", None)
        if callable(close):
            # A remote job dies with its session (ssh: the master exits too; a
            # pack sandbox: its own teardown — stop/snapshot is the pack's concern).
            try:
                await close()
            except Exception:
                pass
    host = _HOST.pop(sid, None)
    if host is not None:
        # A host job dies with its session: the owner's machine keeps no orphan
        # server the agent can no longer see or kill.
        try:
            await host.kill_all()
        except Exception:
            pass
    backend = _POOL.pop(sid, None)
    if backend is not None:
        try:
            published = await backend.published_ports()
            if published:
                from tools.shell.loopback_allow import revoke_loopback_ports
                revoke_loopback_ports(published.values())
        except Exception:
            pass
        try:
            await backend.teardown()
        except Exception:
            pass


def shell_backend_choice() -> str:
    """``SHELL_BACKEND``: ``auto`` (default — the host when this turn may use it,
    else the sandbox) | ``docker`` (never the host) | ``host`` (the host or a
    refusal; never a silent sandbox) | ``ssh`` | ``<pack backend name>`` (073 W6:
    that remote backend or a refusal). Unset or not name-shaped -> ``auto``. A
    name-shaped value that names no backend is returned as is and REFUSED at
    resolution — an explicit choice never degrades to ``auto`` (which may be the
    host). Read per call: every value is at most as wide as ``auto``, and ``auto``
    itself is bounded by the frozen posture and :func:`host_shell_refusal`."""
    import os
    raw = (os.getenv("SHELL_BACKEND") or "auto").strip().lower()
    if raw in SHELL_BACKENDS or _NAME_RE.match(raw):
        return raw
    return "auto"


def _remote_kind(choice: str) -> bool:
    return choice not in ("auto",) and choice not in _LOCAL_KINDS


def _host_executor(execution_context):
    sid = getattr(execution_context, "session_id", None) or "shell"
    ex = _HOST.get(sid)
    if ex is not None:
        return ex
    from pathlib import Path
    from agents.task.path import pm
    from tools.shell.host_executor import HostShellExecutor
    uid = getattr(execution_context, "user_id", None)
    workspace = getattr(execution_context, "workspace_dir", None)
    if not workspace:
        workspace = str(pm().get_workspace_dir(sid, uid))
    jobs = Path(pm().get_logs_dir(sid, uid)) / "jobs"
    ex = HostShellExecutor(workspace_dir=str(workspace), jobs_dir=jobs)
    _HOST[sid] = ex
    return ex


def host_selected(execution_context) -> bool:
    """Whether THIS turn's shell runs on the host. Raises
    :class:`ShellBackendUnavailable` for ``SHELL_BACKEND=host`` when the host gate
    refuses (an explicit host request never degrades to the sandbox)."""
    choice = shell_backend_choice()
    if choice == "docker" or _remote_kind(choice):
        return False  # an explicit sandbox / remote backend never runs on the host
    from core.security.host_execution import host_shell_refusal
    refusal = host_shell_refusal(execution_context)
    if refusal is None:
        return True
    if choice == "host":
        raise ShellBackendUnavailable(f"SHELL_BACKEND=host, but {refusal}.")
    return False


def _session_dir(execution_context, sid: str) -> str:
    from agents.task.path import pm
    return str(pm().get_logs_dir(sid, getattr(execution_context, "user_id", None)))


def _workspace_dir(execution_context, sid: str) -> str:
    workspace = getattr(execution_context, "workspace_dir", None)
    if workspace:
        return str(workspace)
    from agents.task.path import pm
    return str(pm().get_workspace_dir(sid, getattr(execution_context, "user_id", None)))


def _build_remote_executor(kind: str, execution_context, sid: str):
    """A NEW remote executor for ``kind`` (``ssh`` or a pack backend), or raise
    :class:`ShellBackendUnavailable` naming why there is none."""
    from pathlib import Path
    if kind == "ssh":
        from tools.shell.remote_executor import ssh_shell_executor
        return ssh_shell_executor(session_id=sid,
                                  session_dir=Path(_session_dir(execution_context, sid)) / "ssh")
    from tools.code_exec.pack_backends import shell_backend_names, shell_executor_factory
    factory = shell_executor_factory(kind)
    if factory is None:
        known = ", ".join(["ssh", *shell_backend_names()])
        raise ShellBackendUnavailable(
            f"SHELL_BACKEND={kind} names no loaded shell backend (known remote backends: "
            f"{known}). Enable the pack that provides it (polyrob pack list) or set "
            "SHELL_BACKEND=auto|docker.")
    return factory(session_id=sid,
                   workspace_dir=_workspace_dir(execution_context, sid),
                   logs_dir=_session_dir(execution_context, sid))


async def _remote_executor(kind: str, execution_context):
    """The ONE pooled remote executor for (session, kind) — created once, so
    `shell` and `process` share the far-side job files and the ssh master."""
    from tools.code_exec.sandbox_guard import remote_shell_refusal
    from tools.shell.executor_protocol import NotAShellExecutor, require_shell_executor
    sid = getattr(execution_context, "session_id", None) or "shell"
    key = (sid, kind)
    ex = _REMOTE.get(key)
    if ex is None:
        async with _LOCK:
            ex = _REMOTE.get(key)
            if ex is None:
                try:
                    ex = require_shell_executor(_build_remote_executor(kind, execution_context, sid),
                                                source=f"SHELL_BACKEND={kind}")
                except NotAShellExecutor as e:
                    raise ShellBackendUnavailable(str(e)) from e
                except ShellBackendUnavailable:
                    raise
                except Exception as e:  # a pack factory fault is this backend's refusal
                    raise ShellBackendUnavailable(
                        f"SHELL_BACKEND={kind}: the backend could not be built "
                        f"({type(e).__name__}: {e}).") from e
                _REMOTE[key] = ex
    # The sandbox rule is re-checked on EVERY call (custody can arrive mid-process),
    # BEFORE any command reaches the far side.
    try:
        caps = dict(getattr(ex, "capabilities", None) or {})
    except Exception:
        caps = {}
    refusal = remote_shell_refusal(kind, caps)
    if refusal:
        raise ShellBackendUnavailable(refusal)
    return ex


async def resolve_shell_executor(execution_context, *, kind: Optional[str] = None):
    """The ONE executor for this turn: host (posture 3, gated), the session
    sandbox, or an explicitly chosen remote backend (073 W6).

    ``kind`` pins a job's executor for the `process` tool: a host job is only ever
    managed from a turn that may use the host; a remote job only through the
    same (session, kind) executor.
    """
    sid = getattr(execution_context, "session_id", None) or "shell"
    if kind is None:
        choice = shell_backend_choice()
        if _remote_kind(choice):
            kind = choice
        else:
            kind = "host" if host_selected(execution_context) else "docker"
    if kind == "host":
        from core.security.host_execution import host_shell_refusal
        refusal = host_shell_refusal(execution_context)
        if refusal is not None:
            raise ShellBackendUnavailable(refusal)
        return _host_executor(execution_context)
    if kind != "docker":
        return await _remote_executor(kind, execution_context)
    from tools.shell.executor import DockerShellExecutor
    try:
        return DockerShellExecutor(await get_shell_backend(sid))
    except ShellBackendUnavailable as e:
        # At posture 3 say why the HOST was not used either — otherwise the owner
        # who asked for the host shell only hears about docker.
        try:
            from core.config_policy import compute_posture
            from core.security.host_execution import host_shell_refusal
            if compute_posture() >= 3 and shell_backend_choice() == "auto":
                why = host_shell_refusal(execution_context)
                if why:
                    raise ShellBackendUnavailable(f"{e} (The host shell was not used: {why}.)") from e
        except ShellBackendUnavailable:
            raise
        except Exception:
            pass
        raise
