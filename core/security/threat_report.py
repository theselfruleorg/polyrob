"""045 lane 3 — threat scans report to somebody.

``modules.memory.task.threat_scan.is_suspicious`` runs at about a dozen sites
(authored skills, deliverables, MCP self-install, message send, controller emit)
and returns a bool that changes ONE local decision and is then discarded. A
scanner whose hits are never counted cannot tell you that you are under attack.

This helper is additive: call it AFTER the existing verdict, never instead of it.
"""
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Where the flagged content came from. Closed set — see refusals.py's note.
THREAT_ORIGINS = frozenset({
    "sender",   # an inbound turn from a chat surface
    "url",      # fetched web content (web_fetch, browser)
    "server",   # an MCP tool result
    "skill",    # an authored or installed skill body
    "file",     # a workspace file / deliverable / attachment
})


def _log():
    from core.event_log import get_event_log
    return get_event_log()


def report_threat(origin: str, *, user_id: str = "", session_id: str = "",
                  source: str = "", detail: Any = "") -> None:
    """Record ONE threat-scan hit. Raises ValueError on an unknown origin (a
    programming error, caught in tests), fail-open on anything else."""
    if origin not in THREAT_ORIGINS:
        raise ValueError(f"unknown threat origin: {origin!r} "
                         f"(add it to THREAT_ORIGINS)")
    try:
        from core.event_log import event_log_enabled
        from core.event_kinds import INJECTION_FLAGGED
        from core.security_flags import security_event_log_enabled
        if not (security_event_log_enabled() and event_log_enabled()):
            return
        _log().record(INJECTION_FLAGGED, user_id=str(user_id or ""),
                      session_id=str(session_id or ""), source="threat_scan",
                      attrs={"origin": origin, "source": str(source or ""),
                             "detail": str(detail or "")[:200]})
    except Exception:
        logger.debug("threat_report: record skipped", exc_info=True)
