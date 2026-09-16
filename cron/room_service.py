"""044 T20: the owner seat for the room SERVICE job.

``/groups service here every 30m [max 3]`` starts a durable, recurring CRON job
that services one room: it reads that room's ledger since its own checkpoint and
answers only what needs answering (``agents/task/goals/group_service.py`` builds
the turn; ``cron/runner.py`` binds the run to the room session). An empty tail is
a ``$0`` tick — no session, no model call.

A cron job, not a goal: servicing a room is a CADENCE, and cron is the one
durable recurring path.

⚠️ This is the twin of ``core/surfaces/group_admin.py`` — the ONE helper set
every seat (Telegram ``/groups``, ``polyrob owner groups``, the REPL) renders
through, one function, one verified sentence back. It lives HERE rather than
there because ``core`` may not import ``cron`` (tier-0 -> tier-4, the layering
ratchet): a helper that CREATES a cron job belongs in the tier that owns the
store it writes to. The chat-id normalization is shared
(``group_admin.normalize_chat_id``) so both modules always mean the same room.
"""
from __future__ import annotations

import logging
from typing import Any, List

logger = logging.getLogger(__name__)

#: ``/groups service here off`` — the owner's word for "stop servicing this room".
_SERVICE_OFF = ("off", "stop", "none", "never", "cancel")


def _cron_service(data_dir: str):
    """A ``CronService`` on the SAME ``<data_dir>/cron.db`` the lifespan ticker
    runs from — a job written anywhere else would never tick."""
    from core.runtime_paths import cron_db_path
    from cron.jobs import CronJobStore
    from cron.service import CronService
    return CronService(CronJobStore(cron_db_path(data_dir)))


def _service_jobs(cron, owner_uid: str, surface: str, chat_id: str) -> List[Any]:
    """This room's LIVE service jobs (a cancelled/finished one does not count)."""
    out = []
    for job in cron.list_jobs(owner_uid) or []:
        group = (job.payload or {}).get("group") or {}
        if (str(group.get("surface") or "") == surface
                and str(group.get("chat_id") or "") == str(chat_id)
                and job.enabled and job.status not in ("cancelled", "done")):
            out.append(job)
    return out


def service(container: Any, owner_uid: str, surface: str, chat_id: str, *,
            every: str = "30m", max_replies: int = 3) -> str:
    """Start (or, with ``every`` in ``off``/``stop``/``none``, stop) this room's
    service job. ONE verified sentence back.

    Idempotent per room: a second call names the job that already exists rather
    than leaving two of them answering the same chat.
    """
    from core.surfaces import group_admin

    chat_id = group_admin.normalize_chat_id(chat_id)
    label = f"{surface}:{chat_id}"
    data_dir = group_admin.data_home(container)
    try:
        cron = _cron_service(data_dir)
    except Exception as e:
        logger.warning("room service: cron store unavailable for %s: %s", label, e)
        return f"❌ Cannot reach the cron store — {e}"
    existing = _service_jobs(cron, owner_uid, surface, chat_id)
    if str(every).strip().lower() in _SERVICE_OFF:
        if not existing:
            return f"No service job for {label}."
        for job in existing:
            cron.cancel(job.id, user_id=owner_uid)
        return (f"🛑 Stopped servicing {label} "
                f"({', '.join(j.id[:8] for j in existing)}).")
    # Fix round 1 (Minor 10): refuse a room the agent is not IN. The run would
    # bind to a chat that is ingress-DENIED (or `left` after a kick), read an
    # empty ledger and skip forever — a job that silently does nothing.
    #
    # Fix round 2 (Obs 2): BEFORE the existing-job branch. A room the bot was
    # kicked from still has its job row, so the owner asking about it was told
    # "Already servicing" — a confident answer about a room the agent cannot
    # reach. The `off` branch stays first, so he can always stop a job for a room
    # he has since denied. (N3: inside a try — every failure of this verb is a
    # sentence, never a traceback.)
    try:
        allowed = group_admin.is_room_allowed(container, surface, chat_id)
    except Exception as e:
        logger.warning("room service: allowlist probe failed for %s: %s", label, e)
        return f"❌ Cannot read the room allowlist for {label} — {e}"
    if not allowed:
        hint = (f" (its service job {existing[0].id[:8]} is still scheduled — "
                "`/groups service here off` to stop it)") if existing else ""
        return (f"❌ {label} is not an allowed room — the agent is not in it (or was "
                f"removed). Run `/groups allow {surface} {chat_id}` first.{hint}")
    if existing:
        job = existing[0]
        return (f"Already servicing {label} every {job.schedule_spec} "
                f"({job.id[:8]}). `/groups service here off` to stop.")
    payload = {"group": {"surface": surface, "chat_id": str(chat_id),
                         "chat_type": "supergroup"},
               "max_replies": int(max_replies), "max_steps": 8}
    try:
        # Inside the try (Minor 9): a hand-edited overlay that cannot be parsed
        # must cost the job its NAME, never the whole verb.
        from core.surfaces import chat_policy
        name = chat_policy.load(data_dir, owner_uid, surface, chat_id).name or chat_id
        job = cron.schedule(task=f"Service room {name}", schedule_spec=every,
                            user_id=owner_uid, payload=payload)
    except Exception as e:  # ScheduleError for an unparseable cadence
        return f"❌ {e}"
    n = int(max_replies)
    return (f"🛠 Servicing {label} every {every} — at most {n} "
            f"{'reply' if n == 1 else 'replies'} per run ({job.id[:8]}). "
            "`/groups service here off` to stop.")


__all__ = ["service"]
