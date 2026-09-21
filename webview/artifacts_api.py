"""The session's artifacts, typed — the Files half of the Work pane (043 A16).

``GET /api/webgate/artifacts?session_id=<id>`` — read-only, tenant-scoped. Lists
the ledger rows :mod:`core.artifacts` recorded for the session, each with its
``kind`` (page/code/report/data/file), any published ``url``, and a freshly
computed ``verdict`` (``ok``/``changed``/``missing``/``unknown``). It reads the
ONE ledger the tools write on every produced file; it never records anything.

Two honest-state rules this endpoint keeps:

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
import logging
import os
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter()


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

    try:
        from agents.task.path import pm
        clean_id = pm().clean_session_id(session_id)
    except Exception:
        clean_id = session_id

    try:
        from core.artifacts import get_artifact_ledger
        ledger = get_artifact_ledger()
        rows = ledger.list_for_session(str(user_id), clean_id)
    except Exception:
        logger.warning("artifact ledger read failed for session %s", session_id, exc_info=True)
        return JSONResponse({"artifacts": None, "error": "unreadable"})

    artifacts = []
    for art in rows:
        try:
            verdict = ledger.verify(art.id, str(user_id))
        except Exception:
            verdict = "unknown"
        artifacts.append({
            "id": art.id,
            "path": os.path.basename(art.path or ""),
            "kind": art.kind,
            "url": art.url,
            "verdict": verdict,
        })
    return JSONResponse({"artifacts": artifacts, "error": None})
