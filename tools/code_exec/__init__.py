"""Code-execution tool package (Item 3 — minimal WS-C; P0-A added the docker backend).

Exposes an ``ExecutionBackend`` seam + registry with two backends —
``local_subprocess`` (convenience only, NOT a sandbox) and ``docker`` (hardened,
sandbox-capable container) — selected via ``CODE_EXEC_BACKEND``, and a
``CodeExecutionTool`` with a single ``run_code`` action. Gated OFF by default
(``CODE_EXEC_ENABLED``) and never in the default ``tool_ids`` — agents must
explicitly request ``code_execution``.

P1-B (opt-in): the ``docker`` backend also supports a PERSISTENT per-session mode
(one long-lived container, ``docker exec`` per call — state/pip-installs/cwd survive
across ``run_code`` calls within a session) — see ``tools/code_exec/backends/
docker.py``. ``resolve_backend`` accepts an optional ``session_id`` seam for this;
``code_exec_docker_persistent_enabled()`` is the flag that turns it on. Wired
end-to-end (F7b) into both call sites — ``CodeExecutionTool.run_code``
(``tools/code_exec/tool.py``) and ``CodingTool.run_tests``
(``tools/coding/tool.py``) — each resolving + caching a session-scoped
persistent backend when the flag is on and an ``execution_context.session_id``
is present; both stay byte-identical (ephemeral, session-less) otherwise.
"""
from __future__ import annotations

import logging
import os

from core.env import bool_env as _bool_env
from tools.code_exec.backend import (
    ExecutionBackend,
    ExecutionBackendError,
    ExecutionBackendRegistry,
    default_registry,
)
from tools.code_exec.result import ExecutionRequest, ExecutionResult
from tools.code_exec.backends.local_subprocess import LocalSubprocessBackend

# The one backend that ships. Self-registers on import.
default_registry.register("local_subprocess", LocalSubprocessBackend)

# P0-A: hardened container backend (sandbox-capable). Import lazily-safe (stdlib only)
# so registering it can't break tool import if the module grows a heavier dep later.
from tools.code_exec.backends.docker import DockerBackend  # noqa: E402
default_registry.register("docker", DockerBackend)


def code_exec_enabled() -> bool:
    return _bool_env("CODE_EXEC_ENABLED", False)


def get_backend_name() -> str:
    return os.getenv("CODE_EXEC_BACKEND", "local_subprocess")


def code_exec_docker_persistent_enabled() -> bool:
    """Opt-in (P1-B): when True AND a ``session_id`` is supplied to
    ``resolve_backend``, the resolved ``docker`` backend is constructed in
    PERSISTENT per-session mode (one long-lived container, ``docker exec`` per
    ``run_code`` call — state/pip-installs/cwd survive across calls) instead of the
    default ephemeral ``docker run --rm`` per call. Default OFF => ``resolve_backend``
    behavior is completely unchanged for every existing caller.

    Deliberately NOT part of the ``POLYROB_LOCAL`` safe-flag group — this is a
    server-shaped statefulness trade-off (a python-REPL-ish sandbox that keeps state
    across turns), not a default-on local convenience.

    Wired end-to-end (P1-B F7b): both callers of ``resolve_backend`` —
    ``tools/code_exec/tool.py::CodeExecutionTool._get_backend`` and
    ``tools/coding/tool.py::CodingTool._get_code_exec_backend`` — accept the
    action's ``execution_context`` and, when this flag is on AND
    ``execution_context.session_id`` is truthy, call
    ``resolve_backend(session_id=sid)`` and cache the result keyed by
    ``session_id`` (a dict per tool instance, NOT the single instance-level
    ``self._backend`` slot the ephemeral path uses) — so a persistent backend is
    scoped to exactly the session that created it and never leaks into another
    session's calls. Flag off, or no ``session_id`` on the context, is
    byte-identical to the pre-existing ephemeral, session-less caching.
    """
    return _bool_env("CODE_EXEC_DOCKER_PERSISTENT", False)


def resolve_backend(
    registry: ExecutionBackendRegistry | None = None,
    *,
    session_id: str | None = None,
) -> ExecutionBackend:
    """Resolve the configured backend (``CODE_EXEC_BACKEND``) from a registry.

    ``session_id`` is an opt-in seam (P1-B): when resolving through the DEFAULT
    registry (``registry`` not explicitly supplied) AND the resolved backend name is
    ``docker`` AND ``CODE_EXEC_DOCKER_PERSISTENT`` is on AND a ``session_id`` was
    passed, construct it directly in PERSISTENT mode (``DockerBackend(session_id=
    ...)``) instead of via the registry's zero-arg factory. Every other case —
    including every existing caller today, none of which passes ``session_id`` — is
    byte-identical to before. An explicitly-supplied ``registry`` is always honored
    as-is (never silently bypassed by this seam).
    """
    name = get_backend_name()
    if (
        registry is None
        and session_id
        and name == "docker"
        and code_exec_docker_persistent_enabled()
    ):
        from tools.code_exec.backends.docker import DockerBackend
        return DockerBackend(session_id=session_id)
    return (registry or default_registry).create(name)


def register_code_exec_tool(force: bool = False) -> bool:
    """Register the code_execution descriptor + class IFF enabled (or forced).

    Returns True when registered. No-op (returns False) when ``CODE_EXEC_ENABLED``
    is off — so flag-off => ``get_tool_class('code_execution')`` is None.
    """
    from tools.descriptors import (
        ToolDescriptor,
        ToolCategory,
        register_optional_tool,
    )
    from tools.code_exec.tool import CodeExecutionTool

    registered = register_optional_tool(
        "code_execution",
        CodeExecutionTool,
        ToolDescriptor(
            name="code_execution",
            description="Execute python/bash code in a sandbox backend (timeout + output cap + env allowlist)",
            category=ToolCategory.INTEGRATION,
            is_optional=True,
            init_priority=80,
        ),
        code_exec_enabled,
        force=force,
    )
    if registered:
        try:
            from tools.code_exec.sandbox_guard import require_sandbox_or_none
            reason = require_sandbox_or_none(get_backend_name())
            if reason:
                logging.getLogger(__name__).warning(
                    "code_execution registered but execution will be REFUSED at run time: %s",
                    reason,
                )
        except Exception:
            pass
    return registered


__all__ = [
    "ExecutionBackend",
    "ExecutionBackendError",
    "ExecutionBackendRegistry",
    "ExecutionRequest",
    "ExecutionResult",
    "LocalSubprocessBackend",
    "default_registry",
    "code_exec_enabled",
    "get_backend_name",
    "code_exec_docker_persistent_enabled",
    "resolve_backend",
    "register_code_exec_tool",
]
