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
"""
from __future__ import annotations

import asyncio
from typing import Dict, List, Optional

_POOL: Dict[str, object] = {}
_LOCK = asyncio.Lock()


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
    sid = session_id or "shell"
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


def _all_session_ids() -> List[str]:
    return list(_POOL.keys())
