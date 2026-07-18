"""G-1 (metering finalization): rate-limit the swallowed
usage_tracker.record_llm_usage failure log.

Before the fix, `_bill_llm_response` (agents/task/agent/core/next_action_internal.py)
logged a FULL traceback (`exc_info=True`) on EVERY LLM call whenever the metering
write raised (e.g. the FK IntegrityError from an unseeded user_profiles row) —
~15 traceback lines flooding the journal per call. This tests the pure rate-limit
mechanism in isolation, with an injected fake clock (no sleep-based timing).

NOTE: within a test, repeated `ValueError("boom")`/etc. calls that share the
exact same type+message are deliberately used to exercise the TIME-based
suppression/window logic in isolation from the SIGNATURE-based re-arm logic
(tested separately below) — `_logged_once` alone is not enough to silence a
genuinely different failure forever (see the `*_signature_*` tests).
"""
from unittest.mock import MagicMock

from agents.task.agent.core.metering_failure_log import MeteringFailureLimiter


def _fake_clock(box):
    return lambda: box[0]


def test_first_failure_logs_full_traceback():
    box = [0.0]
    limiter = MeteringFailureLimiter(window_sec=60.0, clock=_fake_clock(box))
    logger = MagicMock()

    limiter.record(logger, ValueError("boom"))

    logger.error.assert_called_once()
    _, kwargs = logger.error.call_args
    assert kwargs.get("exc_info") is True
    logger.warning.assert_not_called()


def test_second_failure_within_window_is_fully_suppressed():
    box = [0.0]
    limiter = MeteringFailureLimiter(window_sec=60.0, clock=_fake_clock(box))
    logger = MagicMock()

    limiter.record(logger, ValueError("boom"))
    box[0] = 10.0
    # Same signature (type + message) as the first failure — this is a
    # REPEAT of the same underlying problem, so it stays suppressed.
    limiter.record(logger, ValueError("boom"))

    logger.error.assert_called_once()
    logger.warning.assert_not_called()


def test_failure_after_window_logs_one_liner_with_suppressed_count():
    box = [0.0]
    limiter = MeteringFailureLimiter(window_sec=60.0, clock=_fake_clock(box))
    logger = MagicMock()

    limiter.record(logger, ValueError("boom"))   # t=0: full traceback
    box[0] = 10.0
    limiter.record(logger, ValueError("boom"))   # suppressed (1)
    box[0] = 20.0
    limiter.record(logger, ValueError("boom"))   # suppressed (2)
    box[0] = 61.0
    limiter.record(logger, ValueError("boom"))   # window elapsed: one WARN

    logger.error.assert_called_once()
    logger.warning.assert_called_once()
    msg = logger.warning.call_args[0][0]
    assert "2 occurrence" in msg


def test_window_resets_after_each_rate_limited_log():
    box = [0.0]
    limiter = MeteringFailureLimiter(window_sec=60.0, clock=_fake_clock(box))
    logger = MagicMock()

    limiter.record(logger, ValueError("boom"))    # t=0: full traceback
    box[0] = 61.0
    limiter.record(logger, ValueError("boom"))    # t=61: WARN, 0 suppressed
    box[0] = 62.0
    limiter.record(logger, ValueError("boom"))    # inside the NEW window: suppressed
    box[0] = 122.0
    limiter.record(logger, ValueError("boom"))    # new window elapsed: WARN, 1 suppressed

    assert logger.error.call_count == 1
    assert logger.warning.call_count == 2
    first_warn_msg = logger.warning.call_args_list[0][0][0]
    last_warn_msg = logger.warning.call_args_list[-1][0][0]
    assert "0 occurrence" in first_warn_msg
    assert "1 occurrence" in last_warn_msg


def test_exactly_at_window_boundary_logs():
    box = [0.0]
    limiter = MeteringFailureLimiter(window_sec=60.0, clock=_fake_clock(box))
    logger = MagicMock()

    limiter.record(logger, ValueError("boom"))
    box[0] = 60.0  # exactly the window: should log, not suppress
    limiter.record(logger, ValueError("boom"))

    assert logger.error.call_count == 1
    assert logger.warning.call_count == 1


# --- Signature re-arm (fix pass 1, Finding 2) ------------------------------
#
# `_logged_once` alone permanently reduced any failure AFTER the very first
# one to a one-line WARN, even if it were a completely different error type
# or message months later. The signature (type(error).__name__, str(error))
# must re-arm the full-traceback log on change, then rate-limit repeats of
# that NEW signature exactly as before.


def test_new_signature_relogs_full_traceback_even_within_window():
    box = [0.0]
    limiter = MeteringFailureLimiter(window_sec=60.0, clock=_fake_clock(box))
    logger = MagicMock()

    limiter.record(logger, ValueError("FK constraint failed"))  # t=0: full traceback
    box[0] = 5.0
    # Different type AND message, well within the 60s window — must NOT be
    # silently folded into the first failure's suppression.
    limiter.record(logger, RuntimeError("disk full"))

    assert logger.error.call_count == 2
    for call in logger.error.call_args_list:
        _, kwargs = call
        assert kwargs.get("exc_info") is True
    logger.warning.assert_not_called()


def test_new_signature_relogs_full_traceback_after_a_rate_limited_warn():
    box = [0.0]
    limiter = MeteringFailureLimiter(window_sec=60.0, clock=_fake_clock(box))
    logger = MagicMock()

    limiter.record(logger, ValueError("FK constraint failed"))  # t=0: full traceback
    box[0] = 10.0
    limiter.record(logger, ValueError("FK constraint failed"))  # suppressed
    box[0] = 61.0
    limiter.record(logger, ValueError("FK constraint failed"))  # window elapsed: WARN
    box[0] = 62.0
    # A genuinely new problem shows up right after the WARN — must re-arm
    # the full traceback immediately, not stay silenced.
    limiter.record(logger, RuntimeError("disk full"))

    assert logger.error.call_count == 2
    assert logger.warning.call_count == 1


def test_same_signature_after_a_new_signature_still_rate_limits_normally():
    box = [0.0]
    limiter = MeteringFailureLimiter(window_sec=60.0, clock=_fake_clock(box))
    logger = MagicMock()

    limiter.record(logger, ValueError("FK constraint failed"))  # t=0: full traceback (sig A)
    box[0] = 5.0
    limiter.record(logger, RuntimeError("disk full"))            # t=5: full traceback (sig B, re-armed)
    box[0] = 6.0
    # Repeat of sig B, within its own (freshly-reset) window: suppressed.
    limiter.record(logger, RuntimeError("disk full"))
    box[0] = 66.0
    # Repeat of sig B, window elapsed since sig B's own last log (t=5): WARN.
    limiter.record(logger, RuntimeError("disk full"))

    assert logger.error.call_count == 2
    assert logger.warning.call_count == 1
    msg = logger.warning.call_args[0][0]
    assert "1 occurrence" in msg
