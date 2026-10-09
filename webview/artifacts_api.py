"""The session's artifacts, typed — the Files half of the Work pane (043 A16).

``GET /api/webgate/artifacts?session_id=<id>`` — read-only, tenant-scoped. Lists
the ledger rows :mod:`core.artifacts` recorded for the session, each with its
``kind`` (page/code/report/data/file), any published ``url``, and a freshly
computed ``verdict`` (``ok``/``changed``/``missing``/``unknown``). It reads the
ONE ledger the tools write on every produced file; it never records anything —
the verdict is computed with ``record=False`` (no ``verified_at`` write) and
off the event loop, because a changed file is re-hashed (audit WR1).

Three honest-state rules this endpoint keeps:

1. A session with no recorded artifacts is an empty list, NOT an error — the
   agent that wrote nothing is a fact, not a failure.
2. A ledger the endpoint cannot READ is named (``error`` is set) AND its
   ``artifacts`` is ``null``, never a confident empty list. ⚠️ An empty LIST is
   the sentence "this session produced nothing"; ``null`` is "I did not look"
   / "I could not look". Work › Apps drew the first over real files for weeks
   (043 A3) because a call with no ``session_id`` answered ``[]``.
3. A call with no ``session_id`` is REFUSED, not answered: this ledger is
   session-scoped, so ``{"artifacts": null, "error": "session-scoped"}``.

Tenant scoping is :func:`webview.pages._effective_user_id`, which 403s an
unbound own_ops console and a multitenant caller with no identity — so this
never lists the instance owner's files to a caller who is not them.
"""
import asyncio
import logging
import os
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter()


def workspace_relative(stored: Optional[str], ws_root: Optional[str]) -> Optional[str]:
    """*stored* relative to *ws_root* when its realpath is inside it, else None."""
    if not stored or not ws_root:
        return None
    try:
        real = os.path.realpath(stored)
        if os.path.commonpath([real, ws_root]) != ws_root or real == ws_root:
            return None
        return os.path.relpath(real, ws_root).replace(os.sep, "/")
    except (ValueError, OSError):
        return None


@router.get("/api/webgate/artifacts")
async def api_artifacts(request: Request, session_id: Optional[str] = None) -> JSONResponse:
    """List the recorded artifacts for *session_id*, tenant-scoped and read-only."""
    # Resolve the tenant FIRST and outside any try — a 403 here (unbound own_ops
    # / multitenant without identity) must propagate, never be swallowed into an
    # empty list.
    from webview.pages import _effective_user_id
    user_id = _effective_user_id(request)

    if not session_id:
        # No session named → there is nothing this reader can answer. Saying
        # "no artifacts" here is the confident zero A3 recorded: Work › Apps
        # rendered "Nothing built" over a session tree full of files.
        return JSONResponse({"artifacts": None, "error": "session-scoped"})

    from agents.task.path import pm
    from webview.session_access import http_session_id
    clean_id = http_session_id(session_id, pm())

    try:
        from core.artifacts import get_artifact_ledger
        ledger = get_artifact_ledger()
        rows = ledger.list_for_session(str(user_id), clean_id)
    except Exception:
        logger.warning("artifact ledger read failed for session %s", session_id, exc_info=True)
        return JSONResponse({"artifacts": None, "error": "unreadable"})

    # 070 W0.15: `path` is relative to THIS chat's workspace when the stored
    # realpath is inside it, else None (no link: the file lives elsewhere).
    # Resolved only when there are rows: get_workspace_dir creates the folder.
    ws_root = None
    if rows:
        try:
            from agents.task.path import pm
            ws_root = os.path.realpath(str(pm().get_workspace_dir(clean_id, user_id=str(user_id))))
        except Exception:
            logger.debug("artifact rows: no workspace for %s", clean_id, exc_info=True)

    def _verdicts() -> list:
        out = []
        for art in rows:
            try:
                verdict = ledger.verdict(art, record=False)
            except Exception:
                verdict = "unknown"
            out.append(verdict)
        return out

    verdicts = await asyncio.to_thread(_verdicts) if rows else []
    artifacts = []
    for art, verdict in zip(rows, verdicts):
        artifacts.append({
            "id": art.id,
            "name": os.path.basename(art.path or ""),
            "path": workspace_relative(art.path, ws_root),
            "kind": art.kind,
            "url": art.url,
            "verdict": verdict,
        })
    return JSONResponse({"artifacts": artifacts, "error": None})
