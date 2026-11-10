"""A cron job's minimum gap between its OWN X posts (``payload.post_spacing_hours``).

2026-10-16 18:00Z (intel inbox): the X growth job wakes every 6 h and its text
says "never post twice inside 12 h". The rule was prose the model evaluated
AFTER acting — it quoted a tweet 6.0 h after its last post and only then wrote
"ERROR: … violating the 12h minimum". This makes the spacing a refusal the
model cannot reason around: a run of a job that declares
``post_spacing_hours`` may not publish while a post by that SAME job is newer
than the window.

The record is the one ``external_write`` row (effect ``social``), which
carries ``cron_job_id`` since this guard shipped. A row written before that
has no ``cron_job_id`` key at all and is counted conservatively (it may be
this job's) — such rows age out of every window within one spacing period.
Fail-open on a telemetry fault, like the cross-session cooldown beside it.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

def spaced_actions() -> frozenset:
    """Programme posts: the twitter ``effect = "social"`` + ``outbound = "write"``
    rows (a post, a quote, a thread — derived, so a new public write verb is
    spaced too; a DM is ``comms``), plus the repost. A reply or a like is
    engagement, not a programme post (W4 drift ratchet)."""
    from polyrob.sdk import action_ids_where
    writes = set(action_ids_where(tool="twitter", outbound="write", effect="social"))
    return frozenset((writes - {"twitter_reply"}) | {"twitter_retweet"})


def _last_job_post_ts(log: Any, since: float, user_id: str, job_id: str) -> Optional[float]:
    from core.event_kinds import EXTERNAL_WRITE
    for row in log.query(since_ts=since, kind=EXTERNAL_WRITE, effect="social",
                         user_id=user_id, limit=500):
        a = row.get("attrs") or {}
        if a.get("tool") != "twitter" or a.get("action") not in spaced_actions() \
                or a.get("outcome") != "ok":
            continue
        if "cron_job_id" in a and a.get("cron_job_id") != job_id:
            continue
        return float(row["ts"])
    return None


def spacing_refusal(action_name: str, execution_context: Any) -> Optional[str]:
    """A refusal string, or None to proceed. Never raises."""
    if action_name not in spaced_actions() or execution_context is None:
        return None
    try:
        from core.autonomy_marker import cron_job_for_session, post_spacing_for_job
        job_id = cron_job_for_session(getattr(execution_context, "session_id", None))
        hours = post_spacing_for_job(job_id) if job_id else None
        if not hours:
            return None
        from core.event_log import event_log_enabled, open_event_log
        if not event_log_enabled():
            return None
        log = open_event_log()
        if log is None:
            return None
        user_id = str(getattr(execution_context, "user_id", "") or "")
        last = _last_job_post_ts(log, time.time() - hours * 3600, user_id, job_id)
        if last is None:
            return None
        age_h = (time.time() - last) / 3600
        return (f"Twitter write '{action_name}' refused: this job ({job_id}) posts at most "
                f"once every {hours:g} h (payload.post_spacing_hours) and its last post went "
                f"out {age_h:.1f} h ago — {hours - age_h:.1f} h remain. POST NOTHING this run: "
                f"record a pacing hold (posted:false) and do the rest of the run. This is "
                f"enforced, not a judgement call; do not retry with another write verb.")
    except Exception:
        logger.debug("x post-spacing probe failed (fail-open)", exc_info=True)
        return None


#: Text that is a stand-in, never a post (prod 2026-11-06: a run called twitter_post
#: with "placeholder", and only the cooldown stopped it; a replay posted "verify").
_PLACEHOLDER = re.compile(
    r"\s*(?:placeholder|test|testing|verify|hello|hello world|tbd|todo|lorem ipsum.*|<[^<>]*>|\.+|x+)\s*\.?\s*", re.I | re.S)


def placeholder_refusal(action_name: str, text) -> Optional[str]:
    """The refusal for a post whose whole text is a stand-in, else None."""
    if isinstance(text, str) and _PLACEHOLDER.fullmatch(text):
        return (f"Twitter write '{action_name}' blocked: {text.strip()!r} is a placeholder, "
                "not a post. Nothing went out. Send the real text.")
    return None
