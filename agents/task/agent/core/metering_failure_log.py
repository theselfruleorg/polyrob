"""Rate limiter for the swallowed usage_tracker.record_llm_usage failure log
(G-1: metering finalization).

On a headless/single-owner deployment where user_profiles was never seeded for
the owner principal, every ``usage_tracker.record_llm_usage()`` call raised an
IntegrityError (FK to user_profiles) and the swallowed-exception handler in
``NextActionInternalMixin._bill_llm_response`` logged a FULL traceback
(``exc_info=True``) on EVERY LLM call — ~15 traceback lines flooding the
journal per call. ``modules.database.user_profiles.ensure_owner_profile`` fixes
the root cause, but the log itself must never regress into a flood again if
metering fails for any OTHER reason (a genuinely broken DB, disk full, etc.) —
hence this rate limiter, independent of the root-cause fix.

Behavior: full traceback the first failure per process; afterwards at most one
one-line WARNING per ``window_sec``, folding in how many occurrences were
suppressed since the last log line. The full traceback ALSO re-arms whenever
the error's signature (``type(error).__name__``, ``str(error)``) changes from
the last one logged — a genuinely different failure months later must not be
permanently reduced to a one-liner just because some earlier, unrelated
failure already tripped the "logged once" gate. Repeats of that SAME new
signature then rate-limit exactly as before.
"""
from __future__ import annotations

import time
from typing import Callable, Optional, Tuple

_ErrorSignature = Tuple[str, str]


class MeteringFailureLimiter:
    """Rate-limits a repeating failure log.

    Not thread-safe — intended for single-event-loop asyncio use only (matches
    how ``_bill_llm_response`` is invoked).
    """

    def __init__(
        self,
        window_sec: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window_sec = window_sec
        self._clock = clock
        self._logged_once = False
        self._last_logged = 0.0
        self._suppressed = 0
        self._last_signature: Optional[_ErrorSignature] = None

    @staticmethod
    def _signature(error: BaseException) -> _ErrorSignature:
        return (type(error).__name__, str(error))

    def record(self, logger, error: BaseException) -> None:
        """Record one failure occurrence, logging per the rate-limit policy.

        - First call ever: ``logger.error(..., exc_info=True)`` (full traceback).
        - A call whose error signature (type + message) DIFFERS from the last
          one logged: also a full traceback, regardless of the window — a new
          failure mode must never be silently folded into an old one's
          suppression. The window and suppressed-count reset for this new
          signature.
        - Subsequent calls with the SAME signature within ``window_sec`` of
          the last log: silently counted, nothing logged.
        - First same-signature call at/after ``window_sec`` since the last
          log: one ``logger.warning(...)`` line naming how many occurrences
          were suppressed since that last log, then the window resets.
        """
        now = self._clock()
        signature = self._signature(error)
        signature_changed = self._logged_once and signature != self._last_signature
        if not self._logged_once or signature_changed:
            self._logged_once = True
            self._last_signature = signature
            self._last_logged = now
            self._suppressed = 0
            logger.error(f"Failed to record LLM usage: {error}", exc_info=True)
            return
        if now - self._last_logged >= self._window_sec:
            suppressed = self._suppressed
            self._suppressed = 0
            self._last_logged = now
            logger.warning(
                f"Failed to record LLM usage: {error} "
                f"({suppressed} occurrence(s) suppressed in the last "
                f"{int(self._window_sec)}s)"
            )
        else:
            self._suppressed += 1


# Process-wide singleton: every session/agent instance shares ONE rate limit so
# the journal-flood pattern (many concurrent sessions, one shared root cause)
# is suppressed globally, not per-instance (which would still flood the
# journal with one traceback per session).
metering_failure_limiter = MeteringFailureLimiter()

__all__ = ["MeteringFailureLimiter", "metering_failure_limiter"]
