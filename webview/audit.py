"""043 W4 — the console-write audit rail.

Every mutating console route answers ONE question after the fact: *who did this,
and from where.* Before W4 the console wrote settings, decided pending items,
cancelled cron jobs and settled invoices with no durable actor trail — only the
pause/resume verbs (``core.autonomy_control``) and the app verbs
(``core.app_service.owner_ops``) recorded downstream, and both already stamp
``via="webview"``. Everything else was silent.

``console_write`` is the ONE helper the mutating routes call. It stamps
``source="webview"`` and folds ``via="webview"`` into ``attrs`` so a query over
``telemetry_events`` can tell a console action from the same effect taken on the
CLI, the REPL or Telegram. It is FAIL-OPEN — a broken telemetry sink must never
break the write it observes — and it NEVER raises.

The ``kind`` strings live in :mod:`core.event_kinds` (the SSOT the contract test
greps). ``tests/unit/webview/test_console_write_audit_ratchet.py`` is the
structural proof that every ``post``/``patch``/``delete`` route defined in
``webview/`` either calls this helper or records an actor downstream.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("webview.audit")


def console_write(kind: str, *, user_id: str = "",
                  attrs: Optional[Dict[str, Any]] = None) -> None:
    """Record one console mutation to the durable event log. Fail-open.

    ``kind`` is a constant from :mod:`core.event_kinds`; ``user_id`` is the
    tenant the console resolved (the actor); ``attrs`` carries the verb-specific
    detail. ``via="webview"`` is always folded in so a later query can separate a
    console action from the same effect on another seat. Respects the existing
    ``TELEMETRY_EVENT_LOG_ENABLED`` switch (``event_log_enabled``) — no new flag.
    """
    try:
        from core.event_log import event_log_enabled, get_event_log
        if not event_log_enabled():
            return
        payload = dict(attrs or {})
        payload["via"] = "webview"
        get_event_log().record(kind, user_id=str(user_id or ""),
                               source="webview", attrs=payload)
    except Exception as exc:  # telemetry must never break the write it observes
        logger.debug("console_write failed (%s): %s", kind, exc)
