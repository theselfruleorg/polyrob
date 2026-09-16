"""045 Phase 1: the single flag reader for the opsec spine.

Separate from ``core/event_log.py::event_log_enabled`` on purpose: an operator
may want the chatty telemetry sink off and still keep the security lanes on (or
the reverse). Both must be on for a security event to be written.
"""
import os

_FALSEY = ("0", "false", "off", "no", "")


def security_event_log_enabled() -> bool:
    """Lanes 1-3 recording. Default ON; OFF is byte-identical to pre-045."""
    return (os.getenv("SECURITY_EVENT_LOG_ENABLED", "true")
            .strip().lower() not in _FALSEY)
