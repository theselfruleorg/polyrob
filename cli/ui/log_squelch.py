"""Keep run-time log errors to one line on non-verbose CLI runs (027 WP3).

`polyrob run` lifts the bootstrap logging.disable after startup so real errors
surface during the run — but a provider failure then spilled multi-page
tracebacks (logger.error(..., exc_info=True) deep in the LLM stack) onto a
first-time user's terminal. This formatter keeps the one-line message and drops
the traceback; `--verbose` skips the squelch entirely.
"""

from __future__ import annotations

import logging


class _SingleLineErrorFormatter(logging.Formatter):
    """Delegates to the handler's original formatter minus exc/stack info."""

    def __init__(self, inner: logging.Formatter | None = None):
        super().__init__()
        self._inner = inner or logging.Formatter()

    def format(self, record: logging.LogRecord) -> str:
        record.exc_info = None
        record.exc_text = ""
        record.stack_info = None
        return self._inner.format(record).rstrip()


def single_line_error_formatter(
    inner: logging.Formatter | None = None,
) -> logging.Formatter:
    return _SingleLineErrorFormatter(inner)


def apply_single_line_errors() -> None:
    """Wrap every root stream handler's formatter. Idempotent, fail-open."""
    try:
        for handler in logging.getLogger().handlers:
            if isinstance(handler.formatter, _SingleLineErrorFormatter):
                continue
            handler.setFormatter(_SingleLineErrorFormatter(handler.formatter))
    except Exception:
        pass
