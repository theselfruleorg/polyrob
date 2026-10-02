"""The workspace tree: ``GET /api/session/{session_id}/workspace/tree``.

Moved out of ``webview/server.py`` (070 W0.14) and changed in four ways:

* **Capped.** At most :data:`TREE_MAX_ENTRIES` entries, at most
  :data:`TREE_MAX_DEPTH` folders deep; :data:`TREE_SKIP` folders (and every
  dot-name) are not walked. On prod the workspace IS the shared project folder,
  and the old route walked all of it on every tick.
* **Breadth first, newest first.** Each level is sorted by ``st_mtime``
  descending, so the cap keeps the files a person most likely wants.
* **On demand.** ``?path=`` reads one sub-folder (refused with 403 when it is
  absolute, holds ``..``, or resolves outside the workspace); ``?depth=`` is
  clamped to 1..4. A folder at the depth limit answers ``children: null,
  truncated: true``.
* **No exception text to the client.** A failure answers 500
  ``{children: null, error: "unreadable"}`` and logs the exception.

The response is ``{name, type: "dir", children, truncated, total, shared}``:
``total`` = entries returned, ``truncated`` = the cap or a depth limit was
hit, ``shared`` = the workspace is the one shared project folder
(``pm().is_project_root_workspace``).

Auth is unchanged: the auth middleware gates the path, and the data is read
under the session OWNER's id (``pm().get_session_user``), else the caller's.
"""
from __future__ import annotations

import logging
import os
from collections import deque
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter()

TREE_MAX_ENTRIES = 500
TREE_MAX_DEPTH = 4
TREE_SKIP = frozenset({"node_modules", "__pycache__", "venv", ".venv", "dist", "build"})


def _safe_subpath(ws_dir: Path, path: str) -> Path:
    """The folder *path* names inside *ws_dir*, or a 403."""
    if not path:
        return ws_dir
    if os.path.isabs(path) or path.startswith(("/", "\\")) or ".." in Path(path).parts \
            or ".." in path.replace("\\", "/").split("/"):
        raise HTTPException(403, {"error": "forbidden_path"})
    target = (ws_dir / path).resolve()
    root = ws_dir.resolve()
    if target != root and root not in target.parents:
        raise HTTPException(403, {"error": "forbidden_path"})
    return target


def _entries(folder: Path) -> list:
    """The visible children of *folder*, newest first."""
    kept = []
    for child in folder.iterdir():
        if child.name.startswith(".") or child.name in TREE_SKIP:
            continue
        # Audit WR7: a symlink may point OUTSIDE the workspace; following it
        # listed foreign names. The tree shows what the workspace holds.
        if child.is_symlink():
            continue
        try:
            stat = child.stat()
        except OSError:
            continue
        kept.append((stat.st_mtime, child))
    kept.sort(key=lambda item: item[0], reverse=True)
    return kept


def walk_tree(root: Path, *, depth: int = TREE_MAX_DEPTH,
              max_entries: int = TREE_MAX_ENTRIES) -> dict:
    """Breadth-first, newest-first, capped walk of *root*.

    Returns ``{children, truncated, total}``. A folder at the depth limit is
    ``{name, type: "dir", children: None, truncated: True}``.
    """
    depth = max(1, min(TREE_MAX_DEPTH, int(depth)))
    top: list = []
    queue = deque([(root, top, 1)])
    total = 0
    truncated = False
    while queue:
        folder, into, level = queue.popleft()
        for mtime, child in _entries(folder):
            if total >= max_entries:
                truncated = True
                queue.clear()
                break
            is_dir = child.is_dir()
            item = {"name": child.name, "type": "dir" if is_dir else "file", "mtime": mtime}
            if is_dir:
                if level >= depth:
                    item["children"] = None
                    item["truncated"] = True
                    truncated = True
                else:
                    item["children"] = []
                    queue.append((child, item["children"], level + 1))
            into.append(item)
            total += 1
    return {"children": top, "truncated": truncated, "total": total}


@router.get("/api/session/{session_id}/workspace/tree", response_class=JSONResponse)
async def api_workspace_tree(request: Request, session_id: str, path: str = "",
                             depth: int = TREE_MAX_DEPTH):
    """The session's workspace tree, capped (see the module docstring)."""
    from agents.task.path import pm
    from utils.auth_utils import get_authenticated_user_id

    paths = pm()
    clean_id = paths.clean_session_id(session_id)
    owner = paths.get_session_user(clean_id)
    user_id = owner if owner else get_authenticated_user_id(request)
    try:
        ws_dir = paths.get_workspace_dir(clean_id, user_id=user_id)
        target = _safe_subpath(ws_dir, path)
        shared = bool(getattr(paths, "is_project_root_workspace", False))  # a property
        if not target.is_dir():
            body = {"children": [], "truncated": False, "total": 0}
        else:
            body = walk_tree(target, depth=depth)
        return JSONResponse({"name": target.name, "type": "dir", **body, "shared": shared})
    except HTTPException:
        raise
    except Exception:
        logger.error("workspace tree: could not read %s", clean_id, exc_info=True)
        return JSONResponse({"name": "workspace", "type": "dir", "children": None,
                             "error": "unreadable"}, status_code=500)


from webview.contributions import register_console_router  # noqa: E402

register_console_router(router, destination="new", source="webview.workspace_routes")
