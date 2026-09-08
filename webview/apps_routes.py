"""Console seat for the durable app service (032): read the registry, approve /
reject / kill, and the Apps page. Mounted next to the webgate pages router."""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

router = APIRouter()


def _p():
    from webview import pages
    return pages


def _registry():
    from core.app_service.registry import AppServiceRegistry, default_app_services_db
    return AppServiceRegistry(default_app_services_db())


@router.get("/api/webgate/apps")
async def api_apps(request: Request):
    from core.app_service.config import app_service_enabled
    from core.app_service.owner_ops import row_json
    pages = _p()
    user_id = pages._effective_user_id(request)
    try:
        rows = [row_json(r) for r in _registry().list_for(user_id)]
        return JSONResponse({"enabled": app_service_enabled(), "apps": rows})
    except Exception as exc:  # a failed read is NOT "no apps" — name it
        return JSONResponse({"enabled": app_service_enabled(), "apps": [],
                             "error": f"{type(exc).__name__}: {exc}"[:200]})


async def _decide(request: Request, slug: str, verb: str):
    from core.app_service import owner_ops
    pages = _p()
    pages._mutation_refused()
    pages._owner_console_required()
    user_id = pages._effective_user_id(request)
    fn = {"approve": owner_ops.approve, "reject": owner_ops.reject, "kill": owner_ops.kill}[verb]
    ok, msg = fn(_registry(), slug, user_id, via="webview")
    if not ok:
        raise HTTPException(status_code=409, detail=msg)
    return JSONResponse({"ok": True, "message": msg})


@router.post("/api/webgate/apps/{slug}/approve")
async def api_apps_approve(request: Request, slug: str):
    return await _decide(request, slug, "approve")


@router.post("/api/webgate/apps/{slug}/reject")
async def api_apps_reject(request: Request, slug: str):
    return await _decide(request, slug, "reject")


@router.post("/api/webgate/apps/{slug}/kill")
async def api_apps_kill(request: Request, slug: str):
    return await _decide(request, slug, "kill")


@router.get("/api/webgate/apps/{slug}/logs")
async def api_apps_logs(request: Request, slug: str, n: int = 100):
    """Validate the slug and resolve the tenant's ROW before touching the
    filesystem — mirrors the agent tool's own ``logs()`` action. Building a path
    out of an unvalidated path parameter first is how a read escapes the app's
    log directory."""
    from core.app_service.owner_ops import logs_tail
    from core.publish import valid_slug
    pages = _p()
    user_id = pages._effective_user_id(request)
    if not valid_slug(slug):
        raise HTTPException(status_code=400, detail="invalid slug")
    if _registry().get(slug, user_id) is None:
        raise HTTPException(status_code=404, detail=f"no app {slug!r} for this tenant")
    return JSONResponse({"slug": slug, "logs": logs_tail(pages._data_dir(), user_id, slug, n)})


@router.get("/apps", response_class=HTMLResponse)
async def apps_page(request: Request):
    pages = _p()
    return pages._TEMPLATES.TemplateResponse(request, "apps.html", pages._page_context(request))
