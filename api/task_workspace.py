"""Serving ONE file out of a session workspace (B8).

The A2A layer has always minted artifact URIs of the form
``/api/task/sessions/{id}/workspace/{path}``
(``api/a2a/task_handler.py::_collect_artifacts``,
``api/a2a/streaming.py::_build_artifact_event``) and NO route served them —
every artifact a remote agent was handed 404'd, which made the whole artifact
half of the A2A contract decorative.

This lives in its own module rather than in ``api/task_http_api.py``: that file
sits at its size ratchet (``tests/test_file_size_ratchet.py``), and new
behaviour belongs in a new module, wired with one line.

The shared gates (session-id sanitation, ``guard_remote``, ownership) are
imported LAZILY inside the handler — ``task_http_api`` includes this router, so
a module-level import back into it would be circular.
"""

from fastapi import APIRouter, Depends, HTTPException, Request

#: Mounted into the ``/task`` router by ``api/task_http_api.py``.
router = APIRouter()


def resolve_workspace_file(workspace_dir, relative_path: str):
    """Resolve ``relative_path`` inside ``workspace_dir``, or raise HTTPException.

    Confinement rules, all of them refusals (B8):

    * the path must be RELATIVE (an absolute path is a different tree);
    * no ``..`` segment survives resolution — the resolved real path must sit
      under the workspace's own resolved real path;
    * no component may be a SYMLINK. Resolving first and comparing real paths
      already catches a link pointing outside, but a link pointing *inside* is
      still a second name for a file the agent did not put there, so links are
      refused outright rather than followed;
    * the target must exist and be a regular file.
    """
    from pathlib import Path

    if not relative_path or relative_path.strip() == "":
        raise HTTPException(status_code=400, detail="No file path given")
    candidate = Path(relative_path)
    if candidate.is_absolute() or candidate.drive:
        raise HTTPException(status_code=400, detail="Path must be relative to the workspace")
    if any(part in ("..", "") for part in candidate.parts):
        raise HTTPException(status_code=400, detail="Path may not contain '..'")

    root = Path(workspace_dir).resolve()
    target = (root / candidate)

    # Refuse a symlink anywhere on the way down, before resolving it.
    probe = root
    for part in candidate.parts:
        probe = probe / part
        if probe.is_symlink():
            raise HTTPException(status_code=403, detail="Symlinked paths are not served")

    try:
        resolved = target.resolve()
    except (OSError, RuntimeError):
        raise HTTPException(status_code=400, detail="Path could not be resolved")
    if resolved != root and root not in resolved.parents:
        raise HTTPException(status_code=403, detail="Path escapes the session workspace")
    if not resolved.exists() or not resolved.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return resolved


async def _agent():
    """The TaskAgent dependency (lazy import — see the module docstring)."""
    from api.task_http_api import get_task_agent
    return await get_task_agent()


@router.get("/sessions/{session_id}/workspace/{file_path:path}")
async def get_workspace_file(
    session_id: str,
    file_path: str,
    req: Request = None,
    agent = Depends(_agent)
):
    """Serve ONE file from a session's workspace. Tenant-scoped, confined.

    B8: the A2A layer has always minted artifact URIs of the form
    ``/api/task/sessions/{id}/workspace/{path}``
    (``api/a2a/task_handler.py::_collect_artifacts``,
    ``api/a2a/streaming.py::_build_artifact_event``) and NO route served them —
    every artifact a remote agent was handed 404'd, which made the whole
    artifact half of the A2A contract decorative.

    Gates, in order: session-id sanitation, ``guard_remote`` (an honest 409
    when another worker owns the session, never a false 404), ownership, then
    workspace confinement (:func:`resolve_workspace_file`).
    """
    from fastapi.responses import FileResponse
    import mimetypes
    from agents.task.path import pm
    from api.task_http_api import _guard_session_route, _require_session_owner, \
        clean_session_id_at_entry

    session_id = clean_session_id_at_entry(session_id)
    _guard_session_route(agent, session_id)

    session_info = await agent.get_session_by_id(session_id)
    if not session_info:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    _require_session_owner(req, session_info.get('user_id'))

    workspace_dir = pm().get_workspace_dir(session_id, session_info.get('user_id'))
    if not workspace_dir or not workspace_dir.exists():
        raise HTTPException(status_code=404, detail="Session workspace not found")

    resolved = resolve_workspace_file(workspace_dir, file_path)
    mime_type, _ = mimetypes.guess_type(str(resolved))
    return FileResponse(
        path=str(resolved),
        media_type=mime_type or "application/octet-stream",
        filename=resolved.name,
        # An agent-authored artifact is untrusted content served from the
        # API's own origin: never let a browser render it inline.
        headers={
            "Content-Disposition": f'attachment; filename="{resolved.name}"',
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )


