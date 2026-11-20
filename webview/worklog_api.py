"""Work › Log — the classified activity stream, tenant-scoped (043 A16/§5).

``GET /api/webgate/log?class=&raw=&diagnostics=&limit=`` — read-only. It reads
the ONE activity stream :mod:`webview.activity` already builds (the per-session
feed plus the durable rows: the cold backfill merged with the live hub), scopes
it to the caller's tenant, and stamps each event with the client-facing class
:func:`core.activity_class.classify` gives it. It records nothing.

Four honest-state rules this endpoint keeps:

1. **Tenant-scoped, fail-closed.** The tenant is
   :func:`webview.pages._effective_user_id`, which 403s an unbound own_ops
   console and a multitenant caller with no identity. Only events recorded for
   that tenant are returned — the global stream is never leaked whole.
   ⚠️ On a single-owner console (and for the instance owner on a multitenant
   one) an event with an EMPTY ``user_id`` belongs to this tenant too: the
   snapshot's own tenant filter is ``(user_id = ? OR user_id = '')``
   (``core/status_snapshot.py``), because several writers record a
   single-owner instance's rows with no tenant stamp. Filtering them out made
   Work › Log a strictly smaller day than every other surface (043 A11). A
   NON-EMPTY user_id that is not this tenant's is still another tenant's and
   is still dropped — and the number dropped rides out as ``filtered_out``, so
   a short list is visibly short rather than silently so.
2. **A source it cannot read is NAMED, never a silent drop.** When the read
   raises, the answer carries ``unreadable`` (the machine reason) and
   ``entries`` is ``null`` — the console renders that as a dashed entry, not as
   a day with nothing in it. An empty LIST with ``unreadable`` null is the
   genuine empty state, and the two must never look alike.
3. **Diagnostics are off by default.** The low-signal engine kinds
   (:func:`core.activity_class.is_diagnostic`) are excluded unless
   ``diagnostics`` is set — the same default the switch on the page keeps.
4. **The raw record is opt-in.** The exact payload rides only when ``raw`` is
   set, so a summary-only consumer stays light and the payload is present the
   moment the page's per-row toggle asks for it.

``class`` filters to one member of :data:`core.activity_class.CLIENT_CLASSES`;
an unknown value is ignored (the whole list), never a 400 on a read.

Two more things each row carries, both read from the event's OWN record:

- **Owner words.** A kind the owner would read as a machine name
  (``console_goal_verb``, ``cron_cancelled``, ``owner_command``,
  ``goal_status_paused``) gets one sentence from the copy layer
  (``work.log.kind.<kind>``, :func:`owner_words`); a job is named by its name,
  never its id. A kind with no words keeps the stream's summary.
- **The act's reason** (proposal 122 W1). An ``external_write`` act record
  carries the ``why`` the agent bound for it (``core.act_reason``) — the same
  field ``core.act_report`` reports. It rides as ``why`` on the rows that have
  one; a row with none has no ``why`` key (no invented reason).
"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from core.activity_class import CLIENT_CLASSES, classify, is_diagnostic
from core.event_kinds import EXTERNAL_WRITE, OWNER_COMMAND
from webview.copy import has, t

logger = logging.getLogger(__name__)

router = APIRouter()

_TRUTHY = {"1", "true", "yes", "on"}


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in _TRUTHY


def _recent_events(limit: int) -> List[Dict[str, Any]]:
    """The activity stream's own events, oldest first, at most *limit*.

    ALWAYS the cold backfill over the stores MERGED with the live hub ring,
    deduplicated by event id (audit WV3). The hub holds only what arrived
    since this process started, so reading the cold window only when the ring
    was empty lost everything before a restart the moment the first live
    event landed. May raise; the caller turns that into an honest
    ``unreadable`` answer.
    """
    from webview import activity
    merged: Dict[Any, Dict[str, Any]] = {}
    for event in activity._cold_backfill(limit) + activity.get_hub().recent(limit):
        if not isinstance(event, dict):
            continue
        key = event.get("id") or ("anon", id(event))
        merged[key] = event  # the live copy wins over the cold one
    events = sorted(merged.values(), key=lambda ev: ev.get("ts") or 0.0)
    return events[-limit:]


#: The job kinds whose words name a job (they carry ``job_id``).
_JOB_KINDS = frozenset({
    "console_cron_cancel", "console_cron_create", "console_cron_verb",
    "cron_scheduled", "cron_cancelled", "cron_edited", "cron_paused",
    "cron_resumed", "cron_run_now",
})
#: A job verb's button, in the words the Scheduled jobs tab shows on it.
_CRON_BUTTONS = {"pause": "work.schedule.pause", "resume": "work.schedule.resume",
                 "run": "work.schedule.run", "cancel": "work.schedule.cancel"}
#: A goal verb's button, in the words Work › Goals shows on it.
_GOAL_BUTTONS = {"pause": "work.now.pause", "resume": "work.now.resume",
                 "retry": "work.now.retry", "stop": "work.now.stop",
                 "drop": "work.now.drop"}
_GOAL_STATUS_PREFIX = "goal_status_"


def _quoted(text: str) -> str:
    return f"“{text}”"


def _job_names(user_id: str) -> Dict[str, str]:
    """``job_id -> name`` for the caller's jobs, by the scheduler's own name rule
    (``cron.scheduler._job_name``: the payload name, else the task). ``{}`` when
    the store cannot be read: a row then says "a scheduled job", never the id."""
    try:
        from core.runtime_paths import cron_db_path
        from cron.jobs import CronJobStore
        from cron.scheduler import _job_name
        from webview import pages
        store = CronJobStore(cron_db_path(pages._data_dir()))
        return {j.id: _job_name(j) for j in store.list(user_id=user_id)}
    except Exception:
        logger.debug("work log: job names unreadable", exc_info=True)
        return {}


def _goal_title(payload: Dict[str, Any]) -> str:
    title = str(payload.get("title") or "").strip()
    if not title and payload.get("goal_id"):
        try:
            from webview import activity
            title = str(activity._goal_title(payload.get("goal_id")) or "").strip()
        except Exception:
            title = ""
    return _quoted(title[:70]) if title else t("work.log.a_goal")


def owner_words(kind: str, payload: Any, job_names: Optional[Dict[str, str]] = None) -> str:
    """The owner sentence for one event, or ``""`` when the kind has no words.

    Pure over its inputs (the job names are handed in), so a test needs no store.
    Every word is copy; the fields come from the event's own record."""
    p = payload if isinstance(payload, dict) else {}
    if kind.startswith(_GOAL_STATUS_PREFIX):
        state = kind[len(_GOAL_STATUS_PREFIX):]
        key = f"work.log.goal_state.{state}"
        return t("work.log.kind.goal_status", title=_goal_title(p),
                 state=t(key) if has(key) else t("work.log.goal_state.other"))
    key = f"work.log.kind.{kind}"
    if not has(key):
        return ""
    fields: Dict[str, str] = {}
    if kind in _JOB_KINDS:
        jid = str(p.get("job_id") or "")
        name = str(p.get("name") or (job_names or {}).get(jid) or "").strip()
        fields["job"] = _quoted(name) if name else t("work.log.a_job")
        if kind.startswith("cron_"):
            # The service records the seat that drove it; ``agent`` is the agent.
            agent = str(p.get("via") or "").strip().lower() == "agent"
            fields["who"] = t("work.log.who_agent" if agent else "work.log.who_you")
        if kind == "console_cron_verb":
            verb = str(p.get("verb") or "")
            fields["button"] = _quoted(t(_CRON_BUTTONS[verb]) if verb in _CRON_BUTTONS
                                       else verb)
    if kind in ("console_goal_verb", "console_goal_create"):
        fields["title"] = _goal_title(p)
        verb = str(p.get("verb") or "")
        if kind == "console_goal_verb":
            fields["button"] = _quoted(t(_GOAL_BUTTONS[verb]) if verb in _GOAL_BUTTONS
                                       else verb)
    if kind in ("console_config_write", "console_pref_write"):
        setting = str(p.get("key") or "")
        label = f"pref.{setting}.label"
        fields["setting"] = _quoted(t(label) if has(label) else setting)
    if kind == OWNER_COMMAND:
        seat = str(p.get("seat") or "").strip().lower()
        seat_key = f"work.log.seat.{seat}"
        fields["verb"] = str(p.get("verb") or "?")
        fields["seat"] = t(seat_key) if has(seat_key) else (seat or "?")
    try:
        return t(key, **fields)
    except Exception:
        logger.debug("work log: words for %s did not format", kind, exc_info=True)
        return ""


def act_why(kind: str, payload: Any) -> str:
    """The reason an act record carries (122 W1), or ``""``. Only the act record
    (``external_write``) has one; ``core.act_report`` reads the same field."""
    if kind != EXTERNAL_WRITE or not isinstance(payload, dict):
        return ""
    return " ".join(str(payload.get("why") or "").split())


def _own_untenanted_rows(user_id: str) -> bool:
    """Do rows with NO tenant stamp belong to *user_id*? On a single-owner
    console, yes (rule 1). On a multitenant console only the instance owner
    owns them — another tenant must never read them (audit WR6)."""
    from webview import webgate
    if not webgate.is_multitenant():
        return True
    try:
        return str(user_id) == str(webgate.local_owner_id())
    except Exception:
        return False


@router.get("/api/webgate/log")
async def api_log(
    request: Request,
    class_: str = Query("", alias="class"),
    raw: str = "",
    diagnostics: str = "",
    limit: int = 200,
) -> JSONResponse:
    # Resolve the tenant FIRST and OUTSIDE any try — a 403 (unbound own_ops /
    # multitenant without identity) must propagate, never be swallowed into an
    # empty list.
    from webview.pages import _effective_user_id
    user_id = str(_effective_user_id(request))

    try:
        window = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        window = 200
    want_raw = _truthy(raw)
    want_diagnostics = _truthy(diagnostics)
    class_filter = class_ if class_ in CLIENT_CLASSES else ""

    try:
        events = _recent_events(window)
    except Exception as exc:
        logger.warning("activity log read failed for tenant %s", user_id, exc_info=True)
        from webview.pages import _safe_reason
        return JSONResponse({
            "entries": None,
            "classes": list(CLIENT_CLASSES),
            "filtered_out": None,
            "unreadable": _safe_reason(exc),
            "user_id": user_id,
        })

    entries: List[Dict[str, Any]] = []
    job_names: Optional[Dict[str, str]] = None  # read once, only if a row needs it
    #: Rows this reader REMOVED that a person might otherwise expect to see —
    #: another tenant's events plus the diagnostics the switch is hiding. The
    #: count is what turns "a quiet day" into "a filtered view".
    filtered_out = 0
    own_untenanted = _own_untenanted_rows(user_id)
    for event in events:
        if not isinstance(event, dict):
            continue
        # Tenant isolation: this caller's own events, PLUS the untenanted rows
        # a single-owner instance writes — the same rule the status snapshot
        # applies. Another tenant's row is dropped and COUNTED.
        row_user = str(event.get("user_id") or "")
        if (row_user and row_user != user_id) or (not row_user and not own_untenanted):
            filtered_out += 1
            continue
        kind = str(event.get("kind") or "")
        diagnostic = is_diagnostic(kind)
        if diagnostic and not want_diagnostics:
            filtered_out += 1
            continue
        client_class = classify(kind, effect=event.get("effect"))
        if class_filter and client_class != class_filter:
            filtered_out += 1
            continue
        payload = event.get("payload")
        if kind in _JOB_KINDS and job_names is None:
            job_names = _job_names(user_id)
        words = owner_words(kind, payload, job_names)
        entry: Dict[str, Any] = {
            "id": event.get("id"),
            "ts": event.get("ts"),
            "kind": kind,
            "cls": client_class,
            "diagnostic": diagnostic,
            "summary": words or str(event.get("summary") or ""),
        }
        why = act_why(kind, payload)
        if why:
            entry["why"] = why
        if want_raw:
            entry["payload"] = payload
        entries.append(entry)

    entries.sort(key=lambda item: item.get("ts") or 0.0, reverse=True)
    return JSONResponse({
        "entries": entries,
        "classes": list(CLIENT_CLASSES),
        "filtered_out": filtered_out,
        "unreadable": None,
        "user_id": user_id,
    })
