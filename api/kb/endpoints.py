"""KB (knowledge-base) HTTP API — gated, multi-tenant.

Mount in ``api/app.py`` behind ``KB_API_ENABLED`` (default OFF), mirroring the
OpenAI-compat block:

    from api.kb.endpoints import router as kb_router, kb_api_enabled
    if kb_api_enabled():
        app.include_router(kb_router)

Auth
----
``user_id`` is ALWAYS resolved from the authenticated request state via the shared
``get_user_id`` dependency (same seam the task HTTP API and other routers use).
A caller-supplied ``user_id`` in the body is never trusted.

Provider registration
---------------------
The SQLite memory backend (``MEMORY_BACKEND=sqlite``, default) is registered lazily
on the first ingest/search request via ``_ensure_backend``.  On the full server path
``build_server_bot`` does NOT call ``maybe_register_memory_backend`` — that call lives
in ``construction.py`` (per-session agent build).  Calling it here on first request is
idempotent: the factory's active-check short-circuits if a provider is already live
(a sibling session already registered it).

Path confinement (path-body variant)
-------------------------------------
``POST /api/kb/ingest`` with a path in the body: the path must be **relative** and
must not escape the authenticated tenant's session workspace.  Absolute paths and
``..``-escapes are rejected with HTTP 400 *before* the engine is called (defence in
depth — the engine also confines, but we fail-fast here).

File-upload variant
-------------------
``POST /api/kb/ingest/upload`` accepts a multipart file upload.  The bytes are
written to a unique temp file UNDER the authenticated tenant's session workspace
(``<workspace>/.kb_uploads/<unique>``), so the shared ``kb_ingest`` engine's
confinement check passes naturally.  The upload is ingested through the SAME engine
as the path-body variant (no duplicated chunking/hashing), with ``source_name`` set
to the stripped original filename so a re-upload of the same file dedups by its
logical name rather than the volatile temp path.  The temp file is removed in a
``finally`` block even on error.
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from api.dependencies import get_user_id
from core.path_safety import is_within_root

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/kb", tags=["knowledge-base"])

# ---------------------------------------------------------------------------
# Gate helper
# ---------------------------------------------------------------------------


def kb_api_enabled() -> bool:
    """Whether the KB HTTP API is mounted (default OFF).

    Uses the project's shared ``_bool_env`` parser (falsey-set semantics:
    none/off/false/0/no/'' = off), so the flag behaves consistently with the
    rest of POLYROB's env flags.
    """
    from agents.task.constants import _bool_env
    return _bool_env("KB_API_ENABLED", False)


# ---------------------------------------------------------------------------
# Lazy backend registration (idempotent)
# ---------------------------------------------------------------------------

def _ensure_backend() -> None:
    """Register the memory backend if it isn't already active.

    Called on the first ingest/search request; idempotent — the factory
    short-circuits if a provider is already registered.
    """
    try:
        from modules.memory.backend_factory import maybe_register_memory_backend
        from core.config import BotConfig
        try:
            cfg = BotConfig()
            data_dir = getattr(cfg, "data_dir", "data") or "data"
        except Exception:
            data_dir = "data"
        maybe_register_memory_backend(data_dir=data_dir)
    except Exception as e:
        logger.debug("KB API: memory backend registration skipped: %s", e)


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class KbIngestBody(BaseModel):
    """Body for ``POST /api/kb/ingest`` when providing a workspace-relative path."""
    path: str = Field(..., description="Path relative to the tenant's session workspace.")
    session_id: str = Field(..., description="Session ID whose workspace root to use.")
    collection: str = Field("default", description="KB collection name.")
    recursive: bool = Field(True, description="Recurse into subdirectories.")
    globs: Optional[list[str]] = Field(None, description="Optional glob patterns.")


class KbSearchBody(BaseModel):
    """Body for ``POST /api/kb/search``."""
    query: str = Field(..., description="Search query.")
    collection: str = Field("default", description="KB collection to search.")
    limit: int = Field(8, ge=1, le=50, description="Maximum number of results.")


# ---------------------------------------------------------------------------
# Path-safety helper (endpoint-level early rejection)
# ---------------------------------------------------------------------------

def _resolve_and_guard(
    path: str,
    session_id: str,
    user_id: str,
) -> Path:
    """Resolve *path* against the tenant workspace and return the resolved Path.

    Raises ``HTTPException(400)`` for:
    - Absolute paths.
    - Paths that escape the workspace root after resolution.
    """
    if os.path.isabs(path):
        raise HTTPException(
            status_code=400,
            detail="Absolute paths are not allowed. Provide a path relative to your session workspace.",
        )

    from agents.task.path import pm
    try:
        workspace_root = pm().get_workspace_dir(session_id, user_id)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Could not resolve workspace for session '{session_id}': {exc}",
        )

    resolved = (workspace_root / path).resolve()

    if not is_within_root(str(resolved), str(workspace_root.resolve())):
        raise HTTPException(
            status_code=400,
            detail="Path escapes the session workspace. Only paths within the workspace are allowed.",
        )

    return resolved


# ---------------------------------------------------------------------------
# POST /api/kb/ingest  (path body variant)
# ---------------------------------------------------------------------------

@router.post("/ingest")
async def kb_ingest_path(
    body: KbIngestBody,
    user_id: str = Depends(get_user_id),
) -> JSONResponse:
    """Ingest a file or directory from the tenant's session workspace into the KB.

    The ``path`` in the body must be **relative** to the authenticated tenant's
    session workspace.  Absolute paths and ``..``-escaping paths are rejected.
    ``user_id`` is derived from the auth dependency — the body never supplies it.
    """
    _ensure_backend()

    # Early path safety — reject absolute / escaping paths before calling the engine.
    _resolve_and_guard(body.path, body.session_id, user_id)

    try:
        from tools.knowledge_ingest import kb_ingest
        result = await kb_ingest(
            body.path,
            collection=body.collection,
            recursive=body.recursive,
            globs=body.globs,
            user_id=user_id,
            session_id=body.session_id,
        )
    except Exception as exc:
        logger.warning("KB ingest failed for user=%s: %s", user_id, exc)
        raise HTTPException(status_code=500, detail=f"KB ingest error: {exc}")

    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])

    return JSONResponse(content=result)


# ---------------------------------------------------------------------------
# POST /api/kb/ingest/upload  (file-upload variant)
# ---------------------------------------------------------------------------

@router.post("/ingest/upload")
async def kb_ingest_upload(
    file: UploadFile = File(...),
    collection: str = "default",
    session_id: str = "_kb_upload_",
    user_id: str = Depends(get_user_id),
) -> JSONResponse:
    """Ingest an uploaded file into the tenant's KB.

    The bytes are written to a unique temp file UNDER the authenticated tenant's
    session workspace, then ingested through the SAME ``kb_ingest`` engine as the
    path-body variant — so size guards, MIME detection, dedup and confinement all
    apply for free.  ``source_name`` is the stripped original filename, so a
    re-upload of the same file dedups by logical name (not the volatile temp path).
    ``user_id`` comes from the auth dependency, NEVER from the request body.
    """
    _ensure_backend()

    # Resolve the tenant's confined workspace root and write the upload UNDER it,
    # so the engine's is_within_root confinement check passes naturally.
    from agents.task.path import pm
    try:
        workspace_root = pm().get_workspace_dir(session_id, user_id)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Could not resolve workspace for session '{session_id}': {exc}",
        )

    upload_dir = Path(workspace_root) / ".kb_uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)

    # Strip path components from the client filename (no traversal); keep it as the
    # STABLE source identity so a re-upload dedups by its original name.
    source_name = Path(file.filename or "upload.bin").name
    suffix = Path(source_name).suffix or ".bin"

    tmp_file: Optional[Path] = None
    try:
        fd, tmp_path_str = tempfile.mkstemp(suffix=suffix, dir=str(upload_dir))
        tmp_file = Path(tmp_path_str)
        try:
            with os.fdopen(fd, "wb") as fh:
                content = await file.read()
                fh.write(content)
        except Exception:
            os.close(fd)
            raise

        # Engine path is relative to the workspace root (it resolves
        # allowed_root / path); pass the temp file's path relative to the root.
        rel_path = str(tmp_file.relative_to(Path(workspace_root)))

        from tools.knowledge_ingest import kb_ingest
        result = await kb_ingest(
            rel_path,
            collection=collection,
            recursive=False,
            user_id=user_id,
            session_id=session_id,
            source_name=source_name,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("KB upload ingest failed for user=%s: %s", user_id, exc)
        raise HTTPException(status_code=500, detail=f"KB ingest error: {exc}")
    finally:
        if tmp_file is not None:
            try:
                tmp_file.unlink(missing_ok=True)
            except OSError:
                pass

    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])

    return JSONResponse(content=result)


# ---------------------------------------------------------------------------
# POST /api/kb/search
# ---------------------------------------------------------------------------

@router.post("/search")
async def kb_search_endpoint(
    body: KbSearchBody,
    user_id: str = Depends(get_user_id),
) -> JSONResponse:
    """Search the tenant's KB.

    Results are scoped to the authenticated ``user_id``; a caller cannot search
    another tenant's data.
    """
    _ensure_backend()

    try:
        from modules.memory.registry import kb_search as _kb_search
        result = await _kb_search(
            body.query,
            user_id=user_id,
            collection=body.collection,
            limit=body.limit,
        )
    except Exception as exc:
        logger.warning("KB search failed for user=%s: %s", user_id, exc)
        raise HTTPException(status_code=500, detail=f"KB search error: {exc}")

    return JSONResponse(content={"results": result or ""})
