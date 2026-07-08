"""First-class memory observability events (T4-02, 2026-07-06 structural review).

Memory recall and memory writes were invisible on every surface: the producers
(`memory_prefetch.build_prefetch_message`, `memory_writer` sync, the curated
`memory` tool) logged only at debug, and recall rode an ephemeral LLM message no
owner view ever rendered — a self-evolution channel with zero audit.

`emit_memory_event` records `memory_recall` / `memory_write` rows on the durable
event log (telemetry_events.db), which the `/telemetry` CLI queries and the
webview `/activity` hub tails. Each row carries:

- ``scope``   — cross_session | kb | cross_session+kb | curated
- ``chars``   — size of the recalled/written content
- ``preview`` — ≤120-char single-line, secret-scrubbed excerpt
- ``source``  — the producing seam (prefetch / sync_turn / memory_tool)

Fail-open throughout: telemetry must never break the memory path it observes.
"""
import logging

logger = logging.getLogger("task.telemetry.memory_events")

PREVIEW_CAP = 120


def scrubbed_preview(text, cap: int = PREVIEW_CAP) -> str:
    """Single-line, secret-scrubbed, ≤cap-char excerpt of *text*."""
    t = " ".join(str(text or "").split())
    try:
        from core.secret_scrub import scrub_secret_shapes
        t = scrub_secret_shapes(t)
    except Exception:
        pass
    return t[:cap]


def emit_memory_event(kind: str, *, user_id: str = "", session_id: str = "",
                      source: str = "", scope: str = "", content: str = "",
                      **attrs):
    """Record one memory event on the durable event log. Fail-open.

    Returns the recorded attrs dict (scope/chars/preview + extras) so callers
    with a live-feed handle can mirror the event there, or None when the log is
    disabled/unavailable.
    """
    try:
        from agents.task.telemetry.event_log import event_log_enabled, get_event_log
        if not event_log_enabled():
            return None
        payload = dict(
            scope=scope,
            chars=len(str(content or "")),
            preview=scrubbed_preview(content),
            **attrs,
        )
        get_event_log().record(
            kind,
            user_id=str(user_id or ""),
            session_id=str(session_id or ""),
            source=source,
            **payload,
        )
        return payload
    except Exception as e:
        logger.debug(f"memory event emit skipped: {e}")
        return None
