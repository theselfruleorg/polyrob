"""Console seat for one scheduled job's own state (080 WS-2): pause / resume /
run now. The same ``CronService`` verbs `/job pause|resume|run` call, so the
console adds no rule. Contributed to the Work destination
(``webview.contributions``), with the stop (``cancel``) route declared ahead of the ``{verb}`` route."""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.event_kinds import CONSOLE_CRON_CANCEL, CONSOLE_CRON_VERB
from webview import webgate
from webview.copy import t

router = APIRouter()


def job_label(job) -> str:
    """A job as the owner names it: its name (the payload name, else its task — the
    scheduler's own rule, ``cron.scheduler._job_name``), quoted. Never the id."""
    from cron.scheduler import _job_name
    name = _job_name(job) if job is not None else ""
    return f"“{name}”" if name else t("work.log.a_job")


def console_scheduler_note() -> str:
    """The scheduler-off sentence for the console, or ``""`` when it runs.

    The ONE remedy is ``cron.service.scheduler_off_note`` (a chat seat:
    ``SCHEDULER_ON_CHAT_LINE``). The console says the same remedy in its own
    words — Agent › Settings, no flag name, no backticks
    (``work.schedule.off_note``). A probe fault keeps core's own sentence: it
    says that it could not check, which no copy may turn into "off"."""
    from cron.service import SCHEDULER_ON_CHAT_LINE, scheduler_off_note
    note = scheduler_off_note("chat")
    if not note:
        return ""
    if SCHEDULER_ON_CHAT_LINE in note:
        return t("work.schedule.off_note")
    return note.replace("`", "")


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
    mine = {j.id: j for j in service.list_jobs(user_id=user_id)}
    if job_id not in mine:
        return JSONResponse(
            {"ok": False, "message": t("work.schedule.no_job")}, status_code=404)
    job = mine[job_id]
    name = job_label(job)
    ok = service.cancel(job_id, user_id=user_id, via="webview")
    if not ok:
        return JSONResponse(
            {"ok": False, "message": t("work.schedule.cancel_failed", job=name)},
            status_code=409)
    from cron.scheduler import _job_name
    console_write(CONSOLE_CRON_CANCEL, user_id=user_id,
                  attrs={"job_id": job_id, "name": _job_name(job)})
    return JSONResponse({"ok": True, "message": t("work.schedule.cancelled", job=name)})


@router.post("/api/webgate/cron/{job_id}/{verb}", dependencies=webgate.MUTATION_DEPS)
async def api_cron_verb(request: Request, job_id: str, verb: str):
    """Pause / resume / run one job, tenant-scoped (another tenant's id is a 404)."""
    from core.runtime_paths import cron_db_path
    from cron.jobs import CronJobStore
    from cron.service import CronService
    from webview import pages
    from webview.audit import console_write
    if verb not in ("pause", "resume", "run"):
        return JSONResponse({"ok": False, "error": "unknown_verb"}, status_code=404)
    user_id = pages._effective_user_id(request)
    service = CronService(CronJobStore(cron_db_path(pages._data_dir())))
    job = service.store.get(job_id, user_id=user_id)
    if job is None:
        return JSONResponse({"ok": False, "error": "no_such_job"}, status_code=404)
    cut = ""
    if verb == "pause":
        cut = service.cut_note(job_id, user_id=user_id)
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
                         "scheduler_note": (console_scheduler_note() + cut).strip()})


from webview.contributions import register_console_router  # noqa: E402

register_console_router(router, destination="work", source="webview.job_routes")
