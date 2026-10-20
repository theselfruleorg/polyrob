"""Console seat for one scheduled job's own state (080 WS-2): pause / resume /
run now. The same ``CronService`` verbs `/job pause|resume|run` call, so the
console adds no rule. Contributed to the Work destination
(``webview.contributions``), with the stop (``cancel``) route declared ahead of the ``{verb}`` route."""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.event_kinds import CONSOLE_CRON_CANCEL, CONSOLE_CRON_VERB
from webview import webgate

router = APIRouter()


@router.post("/api/webgate/cron/{job_id}/cancel", dependencies=webgate.MUTATION_DEPS)
async def api_cron_cancel(request: Request, job_id: str):
    """Stop a scheduled job — mirrors `/job stop` (``CronService.
    cancel`` with ``user_id=``). The id is resolved within the caller's OWN
    jobs first, so another tenant's job id is a 404 miss. Declared before the
    ``{verb}`` route so ``cancel`` keeps its own handler."""
    from core.runtime_paths import cron_db_path
    from cron.jobs import CronJobStore
    from cron.service import CronService
    from webview import pages
    from webview.audit import console_write
    user_id = pages._effective_user_id(request)
    service = CronService(CronJobStore(cron_db_path(pages._data_dir())))
    mine = {j.id for j in service.list_jobs(user_id=user_id)}
    if job_id not in mine:
        return JSONResponse(
            {"ok": False, "message": f"no cron job {job_id} for this tenant"},
            status_code=404)
    ok = service.cancel(job_id, user_id=user_id, via="webview")
    if not ok:
        return JSONResponse(
            {"ok": False, "message": f"could not cancel {job_id}"},
            status_code=409)
    console_write(CONSOLE_CRON_CANCEL, user_id=user_id, attrs={"job_id": job_id})
    return JSONResponse({"ok": True, "message": f"cancelled {job_id}"})


@router.post("/api/webgate/cron/{job_id}/{verb}", dependencies=webgate.MUTATION_DEPS)
async def api_cron_verb(request: Request, job_id: str, verb: str):
    """Pause / resume / run one job, tenant-scoped (another tenant's id is a 404)."""
    from core.runtime_paths import cron_db_path
    from cron.jobs import CronJobStore
    from cron.service import CronService, scheduler_off_note
    from webview import pages
    from webview.audit import console_write
    if verb not in ("pause", "resume", "run"):
        return JSONResponse({"ok": False, "error": "unknown_verb"}, status_code=404)
    user_id = pages._effective_user_id(request)
    service = CronService(CronJobStore(cron_db_path(pages._data_dir())))
    job = service.store.get(job_id, user_id=user_id)
    if job is None:
        return JSONResponse({"ok": False, "error": "no_such_job"}, status_code=404)
    if verb == "pause":
        ok, why = service.pause(job_id, user_id=user_id, via="webview"), job.status
    elif verb == "resume":
        ok, why = service.resume(job_id, user_id=user_id, via="webview"), job.status
    else:
        why = service.run_now(job_id, user_id=user_id, via="webview")
        ok = not why
    if not ok:
        return JSONResponse({"ok": False, "status": why}, status_code=409)
    fresh = service.store.get(job_id, user_id=user_id)
    console_write(CONSOLE_CRON_VERB, user_id=user_id, attrs={"job_id": job_id, "verb": verb})
    return JSONResponse({"ok": True, "status": fresh.status if fresh else job.status,
                         "scheduler_note": scheduler_off_note()})


from webview.contributions import register_console_router  # noqa: E402

register_console_router(router, destination="work", source="webview.job_routes")
