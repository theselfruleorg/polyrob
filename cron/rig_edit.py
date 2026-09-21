"""Set a cron job's named tool rig (``payload.rig``) — 057 WS-A.

Its own module rather than another ``CronJobStore`` setter: ``cron/jobs.py`` is
the durable-store SSOT and this is a payload EDIT, the same shape as
``set_priority``, needed by exactly one owner seat. Merge, never replace, the
payload — a rig edit must not drop a job's delivery routing or its wake gate.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from core.sqlite_util import execute_retry


def set_job_payload_key(store: Any, job_id: str, key: str, value: Any, *,
                        user_id: Optional[str] = None) -> bool:
    """Set (``value`` not None) or drop (``value`` None) ONE key of ``payload``.

    The one payload-edit seam: merge, never replace. Returns False for an
    unknown job or another tenant's job — the caller renders the reason.
    """
    job = store.get(job_id)
    if job is None or (user_id is not None and job.user_id != user_id):
        return False
    payload = dict(job.payload or {})
    if value is None:
        payload.pop(key, None)
    else:
        payload[key] = value
    sql = "UPDATE cron_jobs SET payload=? WHERE id=?"
    params: tuple = (json.dumps(payload), job_id)
    if user_id is not None:
        sql += " AND user_id=?"
        params = params + (user_id,)
    return bool(execute_retry(store.db_path, sql, params))


def set_job_rig(store: Any, job_id: str, rig: Optional[str], *,
                user_id: Optional[str] = None) -> bool:
    """Set (or, with ``rig=None``/``""``, clear) ``payload.rig`` on *job_id*.

    Returns False for an unknown rig name, an unknown job, or a job belonging to
    another tenant — the caller renders the reason.
    """
    from core.config_policy.rigs import is_rig
    clearing = rig is None or str(rig).strip() == ""
    if not clearing and not is_rig(rig):
        return False
    return set_job_payload_key(store, job_id, "rig",
                               None if clearing else str(rig).strip().lower(),
                               user_id=user_id)


def set_job_deliver(store: Any, job_id: str, target: Optional[str], *,
                    user_id: Optional[str] = None) -> bool:
    """Set (or, with ``target=None``/``"none"``, DROP) ``payload.deliver``.

    Why: the hourly SCOUT rail carried ``deliver=telegram`` from before its
    own "close silently" rule and DM'd the owner ~20 no-op reports a day
    (intel 2026-09-21 01:20Z); ``is_silent`` honours only a literal
    ``[SILENT]``. Returns False for a target outside the delivery allowlist,
    an unknown job or another tenant's job.
    """
    from cron.delivery import ALLOWED_TARGETS
    clearing = target is None or str(target).strip().lower() in ("", "none", "-")
    if not clearing and str(target).strip().lower() not in ALLOWED_TARGETS:
        return False
    return set_job_payload_key(store, job_id, "deliver",
                               None if clearing else str(target).strip().lower(),
                               user_id=user_id)
