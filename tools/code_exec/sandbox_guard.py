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
from typing import Optional

logger = logging.getLogger(__name__)


def require_sandbox_or_none(backend_name: str) -> Optional[str]:
    """None if ``backend_name`` may execute here; else a refusal reason.

    Local mode always allows. On a server the backend must advertise
    ``capabilities["sandbox"] is True``.
    """
    from core.config_policy import local_mode_enabled
    if local_mode_enabled():
        return None
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
        return None
    return (
        f"code execution refused on this server: backend '{backend_name}' is not a "
        f"sandbox (capabilities.sandbox is not True). Set CODE_EXEC_BACKEND to a sandbox "
        f"backend (e.g. 'docker') to run code here."
    )


def code_exec_execution_blocked_reason() -> Optional[str]:
    """None if code execution may run now; else a refusal reason.

    Local mode: always None. Server: refuse when CODE_EXEC_ENABLED is off, otherwise
    require a sandbox-capable backend.
    """
    from core.config_policy import local_mode_enabled
    if local_mode_enabled():
        return None
    from tools.code_exec import code_exec_enabled, get_backend_name
    if not code_exec_enabled():
        return "code execution is disabled on this server (CODE_EXEC_ENABLED is off)."
    return require_sandbox_or_none(get_backend_name())
