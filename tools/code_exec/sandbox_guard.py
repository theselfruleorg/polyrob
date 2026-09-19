"""Sandbox-required invariant for code execution (P0-4).

Security law (INDEX): agent-authored code NEVER runs in the trusted host process — it
runs only inside a backend whose ``capabilities["sandbox"]`` is True. Locally
(``POLYROB_LOCAL``) the single-user operator trades that for the convenience of the
``local_subprocess`` backend on their own box; on a server we REFUSE to execute unless a
sandbox-capable backend is resolved.

Both helpers return ``None`` when execution is allowed here, else a human/LLM-facing
refusal reason. No ``@BaseTool.action`` closures — ``from __future__`` is safe.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_DOCKER_SOCK = "/var/run/docker.sock"


def docker_socket_unreachable_reason(backend_name: str) -> Optional[str]:
    """None unless ``backend_name`` is ``docker`` AND this process cannot open the
    daemon's unix socket; else ONE honest refusal to return before any docker call.

    Prod 2026-09-19: the agent runs as ``polyrob-agent``, deliberately outside the
    ``docker`` group (security-rollout — rootful Docker authority removed until the
    rootless sandbox of proposal 053 lands), so ``docker run`` failed with
    "permission denied while trying to connect to the docker API". That text reads
    like a transient and the model retried it. The condition is a POSTURE, not a
    fault, and it is knowable before the CLI is spawned — so say so, once, and say
    not to retry. A TCP ``DOCKER_HOST`` has no socket file (skipped); any probe error
    fails open (the backend's own error still surfaces).
    """
    if (backend_name or "").strip() != "docker":
        return None
    try:
        host = (os.getenv("DOCKER_HOST") or "").strip()
        if host and not host.startswith("unix://"):
            return None
        sock = host[len("unix://"):] if host else _DEFAULT_DOCKER_SOCK
        if not os.path.exists(sock):
            return (
                f"code execution unavailable on this deploy: the Docker socket {sock} "
                f"does not exist (no daemon). Do not retry; this is not transient."
            )
        if os.access(sock, os.R_OK | os.W_OK):
            return None
        return (
            f"code execution unavailable on this deploy: the agent identity cannot open "
            f"the Docker socket {sock} (permission denied). This is a deliberate hardening "
            f"posture — rootful Docker access was removed from the agent until the rootless "
            f"sandbox lands (proposal 053, owner approval pending). Do not retry run_tests/"
            f"run_code; verify by reading files (coding_grep / read_file) instead."
        )
    except Exception:
        logger.debug("docker socket probe failed for %r", backend_name, exc_info=True)
        return None


def require_sandbox_or_none(backend_name: str) -> Optional[str]:
    """None if ``backend_name`` may execute here; else a refusal reason.

    Local mode allows host execution only without wallet custody. On a server the backend must advertise
    ``capabilities["sandbox"] is True``.
    """
    from core.config_policy import local_mode_enabled
    from core.security.host_execution import wallet_custody_enabled
    if local_mode_enabled() and not wallet_custody_enabled():
        return docker_socket_unreachable_reason(backend_name)
    from tools.code_exec import default_registry
    try:
        backend = default_registry.create(backend_name)
        sandboxed = bool(backend.capabilities.get("sandbox") is True)
    except Exception as e:  # unknown/broken backend on a server -> refuse
        return (
            f"code execution refused: could not resolve a sandbox-capable backend "
            f"'{backend_name}' ({type(e).__name__}: {e})."
        )
    if sandboxed:
        return docker_socket_unreachable_reason(backend_name)
    return (
        f"code execution refused on this server: backend '{backend_name}' is not a "
        f"sandbox (capabilities.sandbox is not True). Set CODE_EXEC_BACKEND to a sandbox "
        f"backend (e.g. 'docker') to run code here."
    )


def code_exec_execution_blocked_reason() -> Optional[str]:
    """None if code execution may run now; else a refusal reason.

    Local mode: require isolation when wallet custody is enabled. Server: refuse when CODE_EXEC_ENABLED is off, otherwise
    require a sandbox-capable backend.
    """
    from core.config_policy import local_mode_enabled
    if local_mode_enabled():
        from tools.code_exec import get_backend_name
        return require_sandbox_or_none(get_backend_name())
    from tools.code_exec import code_exec_enabled, get_backend_name
    if not code_exec_enabled():
        return "code execution is disabled on this server (CODE_EXEC_ENABLED is off)."
    return require_sandbox_or_none(get_backend_name())
