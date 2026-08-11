"""Shared session-keyed persistent-backend resolution (P1-B F7b dedup).

One helper used by BOTH ``tools.code_exec.tool.CodeExecutionTool._get_backend``
and ``tools.coding.tool.CodingTool._get_code_exec_backend`` — the two blocks
were byte-identical. The helper operates on the tool's EXISTING attributes
(``_persistent_backends``, ``_persistent_lock``, ``_backend``) so tests (and
any other caller) that set those attribute names directly keep working.

PERSISTENT (opt-in): when ``CODE_EXEC_DOCKER_PERSISTENT`` is on AND
``execution_context`` carries a truthy ``session_id``, resolve via
``resolve_backend(session_id=sid)`` and cache PER SESSION — the container is
created once (one ``setup()`` call) and reused for every later call in that
session. The cache is keyed ``(sid, dev_mode)`` (WS-1) so a dev and a non-dev
container for the same session never share mounts; non-dev keeps the legacy
``resolve_backend(session_id=sid)`` call shape byte-identically.

EPHEMERAL (default): flag off, or no session_id — one process-wide,
session-less backend cached on ``tool._backend``.

``resolve_backend`` is passed in by the caller (not imported here) because the
two tools' tests patch it at DIFFERENT seams: ``tools.code_exec.tool.
resolve_backend`` (module attribute) vs ``tools.code_exec.resolve_backend``
(package attribute). Each tool hands over the callable it just looked up, so
both monkeypatch points keep working.
"""
from __future__ import annotations


async def resolve_cached_backend(tool, execution_context, dev_mode, resolve_backend):
    """Resolve (and cache) the execution backend for ``tool``.

    See module docstring for the persistent-vs-ephemeral contract.
    """
    from tools.code_exec import code_exec_docker_persistent_enabled

    sid = None
    if code_exec_docker_persistent_enabled():
        sid = getattr(execution_context, "session_id", None) or None

    if sid:
        key = (sid, bool(dev_mode))
        cached = tool._persistent_backends.get(key)
        if cached is not None:
            return cached
        async with tool._persistent_lock:
            cached = tool._persistent_backends.get(key)  # re-check: lost the race?
            if cached is None:
                if dev_mode:
                    cached = resolve_backend(session_id=sid, dev_mode=True)
                else:
                    cached = resolve_backend(session_id=sid)
                await cached.setup()
                tool._persistent_backends[key] = cached
            return cached

    if tool._backend is None:
        backend = resolve_backend()
        await backend.setup()
        tool._backend = backend
    return tool._backend
