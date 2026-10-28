"""093 A3: can the configured code-execution backend start for THIS process?

Prod 2026-10-28: ``CODE_EXEC_BACKEND=docker`` and the agent UID is (by design)
outside the ``docker`` group, so every ``shell_run`` failed "permission denied
... /var/run/docker.sock" — 43 times in 7 days — while the catalog listed
``shell`` as loadable. The condition is a property of the process, not of the
call, so it is knowable before any call.

ONE cached probe, read by the tools that run on the exec backend (their
``availability_gap()``, which ``tools.tool_disclosure.resolve_tool_status``
reads) and by ``polyrob doctor``:

- :func:`backend_unavailable_reason` — ``None`` when the backend can start here,
  else one short reason. Cached per (backend, ``DOCKER_HOST``) for
  :data:`PROBE_TTL_SEC`, so a catalog render never runs a slow probe.
- :func:`exec_backend_gap` — the ``(reason, remedy)`` pair of the
  ``availability_gap`` seam (remedy key ``backend-unavailable`` in
  ``core.tool_gates.REMEDIES``).
- :func:`doctor_backend_line` — which backends work, for ``polyrob doctor``.

Fail-open everywhere: a probe that raises is "no gap" — the backend's own error
still reaches the agent at call time.
"""
from __future__ import annotations

import logging
import os
import socket
import time
from typing import Dict, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

#: Seconds a probe result is reused. A fixed deploy property; a fix by the
#: operator (group membership, a new backend) shows within this window.
PROBE_TTL_SEC = 60.0
#: The socket connect budget. A local unix socket answers at once.
_CONNECT_TIMEOUT_SEC = 0.5

_CACHE: Dict[Tuple[str, str], Tuple[float, Optional[str]]] = {}


def reset_probe_cache() -> None:
    """Forget every cached probe result (tests; an operator fix in-process)."""
    _CACHE.clear()


def _first_sentence(text: str) -> str:
    text = (text or "").strip()
    head = text.split(". ", 1)[0].rstrip(".")
    return head


def _docker_unavailable_reason() -> Optional[str]:
    from tools.code_exec.sandbox_guard import docker_socket_unreachable_reason
    reason = docker_socket_unreachable_reason("docker")
    if reason:
        return _first_sentence(reason)
    host = (os.getenv("DOCKER_HOST") or "").strip()
    if host and not host.startswith("unix://"):
        return None  # a TCP daemon: not probed here (its own error surfaces)
    from tools.code_exec.backends.docker import docker_binary_is_daemonless
    if not host and docker_binary_is_daemonless():
        return None
    path = host[len("unix://"):] if host else "/var/run/docker.sock"
    if not os.path.exists(path):
        return None  # the guard above already decided; nothing to connect to
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(_CONNECT_TIMEOUT_SEC)
    try:
        sock.connect(path)
    except OSError as e:
        return f"the Docker socket {path} does not accept a connection ({e.strerror or e})"
    finally:
        sock.close()
    return None


def _probe(name: str) -> Optional[str]:
    if name == "local_subprocess":
        return None
    if name == "docker":
        return _docker_unavailable_reason()
    if name == "ssh":
        if not (os.getenv("CODE_EXEC_SSH_HOST") or "").strip():
            return "CODE_EXEC_BACKEND=ssh, but CODE_EXEC_SSH_HOST is not set"
        return None
    from tools.code_exec import default_registry
    try:
        default_registry.create(name)
    except Exception as e:
        return f"backend {name!r} is not available here ({type(e).__name__}: {e})"
    return None


def backend_unavailable_reason(name: Optional[str] = None) -> Optional[str]:
    """``None`` when backend *name* (default: the configured ``CODE_EXEC_BACKEND``)
    can start for this process, else one short reason. Cached."""
    if name is None:
        from tools.code_exec import get_backend_name
        name = get_backend_name()
    name = (name or "").strip() or "local_subprocess"
    key = (name, os.getenv("DOCKER_HOST") or "")
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit is not None and now - hit[0] < PROBE_TTL_SEC:
        return hit[1]
    try:
        reason = _probe(name)
    except Exception:
        logger.debug("exec backend probe failed for %r (fail-open)", name, exc_info=True)
        reason = None
    _CACHE[key] = (now, reason)
    return reason


def gap_from_reason(reason: Optional[str], backend: str) -> Optional[Tuple[str, str]]:
    """The ``availability_gap`` pair for a probe *reason* (``None`` = no gap)."""
    if not reason:
        return None
    from core.tool_gates import remedy_text
    return ("backend-unavailable",
            f"{remedy_text('backend-unavailable', flag=backend)} (probe: {reason})")


def exec_backend_gap() -> Optional[Tuple[str, str]]:
    """``("backend-unavailable", remedy)`` when the configured backend cannot start
    for this process, else ``None`` (093 A3)."""
    try:
        from tools.code_exec import get_backend_name
        name = get_backend_name()
        return gap_from_reason(backend_unavailable_reason(name), name)
    except Exception:
        logger.debug("exec_backend_gap failed (fail-open)", exc_info=True)
        return None


def code_exec_gap() -> Optional[Tuple[str, str]]:
    """The gap of ``code_execution`` (093 A3), else ``None``: the run-time refusal
    ``run_code`` returns before any backend call (no sandbox on a server, host
    code refused, no Docker socket) or, when that passes, the backend probe
    (the socket accepts a connection). Cached like the probe."""
    try:
        from tools.code_exec import get_backend_name
        name = get_backend_name()
        key = (f"code_exec_blocked:{name}", os.getenv("DOCKER_HOST") or "")
        now = time.monotonic()
        hit = _CACHE.get(key)
        if hit is not None and now - hit[0] < PROBE_TTL_SEC:
            blocked = hit[1]
        else:
            # The backend half of code_exec_execution_blocked_reason (the
            # CODE_EXEC_ENABLED half is the tool's flag gate, not a backend gap).
            from tools.code_exec.sandbox_guard import require_sandbox_or_none
            blocked = require_sandbox_or_none(name)
            _CACHE[key] = (now, blocked)
        reason = _first_sentence(blocked) if blocked else backend_unavailable_reason(name)
        return gap_from_reason(reason, name)
    except Exception:
        logger.debug("code_exec_gap failed (fail-open)", exc_info=True)
        return None


def shell_backend_gap() -> Optional[Tuple[str, str]]:
    """The gap of the ``shell`` / ``process`` tools (093 A3), else ``None``.

    Only the sandbox path is probed: a remote ``SHELL_BACKEND`` and the host
    shell (``SHELL_BACKEND=host``, or ``auto`` at ``AGENT_COMPUTE_POSTURE>=3``,
    where the owner's turn may run on the host) are decided per turn, so they
    report no gap here. The sandbox path needs a sandbox backend: a
    ``local_subprocess`` backend is refused by every call
    (``backend_pool._require_persistent_sandbox``)."""
    try:
        from tools.shell.backend_pool import _LOCAL_KINDS, shell_backend_choice
        choice = shell_backend_choice()
        if choice == "host" or (choice != "auto" and choice not in _LOCAL_KINDS):
            return None
        if choice == "auto":
            # The host shell at posture 3 runs only under POLYROB_LOCAL; a server
            # at posture 3 still uses the sandbox, so it is probed.
            from core.config_policy import compute_posture, local_mode_enabled
            if compute_posture() >= 3 and local_mode_enabled():
                return None
        from tools.code_exec import get_backend_name
        name = get_backend_name()
        if name == "local_subprocess":
            return gap_from_reason(
                "backend 'local_subprocess' is not a sandbox; the shell runs only in a "
                "sandbox backend", name)
        return gap_from_reason(backend_unavailable_reason(name), name)
    except Exception:
        logger.debug("shell_backend_gap failed (fail-open)", exc_info=True)
        return None


def probe_identity() -> str:
    """``uid N user`` — the identity a probe in THIS process runs as.

    The probe answers for the calling uid only: ``polyrob doctor`` run as root
    reaches a Docker socket the agent uid cannot (prod 2026-10-28), so the
    doctor line names whom it probed. DEPLOYMENT.md has the command that runs
    doctor as the agent user."""
    getuid = getattr(os, "getuid", None)
    if getuid is None:  # not POSIX
        return "uid unknown"
    uid = getuid()
    try:
        import pwd
        name = pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError) as e:
        name = f"no name ({type(e).__name__})"
    return f"uid {uid} {name}"


def doctor_backend_line(env: Optional[Mapping[str, str]] = None) -> str:
    """``code-exec backends (as uid N user): docker (configured): unavailable — … ·
    local_subprocess: ok (not a sandbox) · ssh: not configured`` — the doctor line
    the ``backend-unavailable`` remedy points the operator to. The probe answers
    for the calling uid, so the line names it (:func:`probe_identity`)."""
    env = os.environ if env is None else env
    configured = (env.get("CODE_EXEC_BACKEND") or "local_subprocess").strip() or "local_subprocess"
    names = ["docker", "local_subprocess", "ssh"]
    if configured not in names:
        names.insert(0, configured)
    parts = []
    for name in names:
        label = f"{name} (configured)" if name == configured else name
        if name == "ssh":
            # The ssh probe only reads CODE_EXEC_SSH_HOST: answer from the same env
            # this line reads, never a second source.
            ok = bool((env.get("CODE_EXEC_SSH_HOST") or "").strip())
            parts.append(f"{label}: {'configured' if ok else 'not configured'}")
            continue
        try:
            reason = backend_unavailable_reason(name)
        except Exception as e:  # pragma: no cover - defensive
            reason = f"probe failed ({type(e).__name__})"
        if reason:
            parts.append(f"{label}: unavailable — {reason}")
        elif name == "local_subprocess":
            parts.append(f"{label}: ok (not a sandbox)")
        else:
            parts.append(f"{label}: ok")
    return f"code-exec backends (as {probe_identity()}): " + " · ".join(parts)


__all__ = ["PROBE_TTL_SEC", "backend_unavailable_reason", "code_exec_gap",
           "doctor_backend_line", "exec_backend_gap", "gap_from_reason", "probe_identity",
           "reset_probe_cache", "shell_backend_gap"]
