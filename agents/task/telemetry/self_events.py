"""First-class self-modification observability (T4-06, 2026-07-06 review).

Skill writes, self-context edits, owner promotions/rejections, and curator
archives previously surfaced only as generic ``tool_execution`` — no owner
surface could answer "what did the agent change about itself?".

``emit_self_modification`` records one ``self_modification`` row on the durable
event log (telemetry_events.db → `/telemetry` CLI + webview `/activity`):

- ``kind``       — skill | self_context
- ``action``     — create | patch | delete | promote | reject | archive | reactivate
- ``item_id``    — the skill id, or the tenant for a self-context doc
- ``pending``    — True when the write landed in the review quarantine
- ``created_by`` — provenance (agent | background_review | owner)
- ``ok``         — whether the mutation took effect
- ``source``     — the producing seam (skill_manage / self_context_manage /
                   owner_review / curator)

Fail-open throughout: telemetry must never break the write path it observes.
"""
import logging

logger = logging.getLogger("task.telemetry.self_events")


def emit_self_modification(*, kind: str, action: str, item_id: str,
                           user_id: str = "", session_id: str = "",
                           pending=None, created_by: str = "", source: str = "",
                           ok: bool = True, **attrs) -> None:
    """Record one self_modification event on the durable event log. Fail-open."""
    try:
        from agents.task.telemetry.event_log import event_log_enabled, get_event_log
        if not event_log_enabled():
            return
        # NOTE: `kind` collides with record()'s positional event-kind param, so
        # the attributes ride the explicit attrs-dict escape hatch.
        get_event_log().record(
            "self_modification",
            user_id=str(user_id or ""),
            session_id=str(session_id or ""),
            source=source,
            attrs={
                "kind": kind,
                "action": action,
                "item_id": str(item_id or ""),
                "pending": pending,
                "created_by": created_by,
                "ok": bool(ok),
                **attrs,
            },
        )
    except Exception as e:
        logger.debug(f"self_modification event emit skipped: {e}")
