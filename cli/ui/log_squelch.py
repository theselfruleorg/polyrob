"""Keep run-time log errors to one line on non-verbose CLI runs (027 WP3).

`polyrob run` lifts the bootstrap logging.disable after startup so real errors
surface during the run — but a provider failure then spilled multi-page
tracebacks (logger.error(..., exc_info=True) deep in the LLM stack) onto a
first-time user's terminal. This formatter keeps the one-line message and drops
the traceback; `--verbose` skips the squelch entirely.
"""

from __future__ import annotations

import logging
import copy


def _missing_browser(message):
    return ("playwright is installed but its browser is not" in message
            or ("Executable doesn't exist" in message and "playwright" in message.lower()))


class _BrowserPrerequisiteFilter(logging.Filter):
    def __init__(self):
        super().__init__()
        self.reported = False

    def filter(self, record):
        if not _missing_browser(record.getMessage()):
            return True
        if self.reported:
            return False
        self.reported = True
        return True


class _SingleLineErrorFormatter(logging.Formatter):
    """Delegates to the handler's original formatter minus exc/stack info."""

    def __init__(self, inner: logging.Formatter | None = None):
        super().__init__()
        self._inner = inner or logging.Formatter()

    def format(self, record: logging.LogRecord) -> str:
        record = copy.copy(record)
        message = record.getMessage()
        record.msg = ("Browser runtime missing. Install it with: python -m playwright install chromium"
                      if _missing_browser(message) else " ".join(message.splitlines()))
        record.args = ()
        record.exc_info = None
        record.exc_text = ""
        record.stack_info = None
        return " ".join(self._inner.format(record).splitlines()).rstrip()


def single_line_error_formatter(
    inner: logging.Formatter | None = None,
) -> logging.Formatter:
    return _SingleLineErrorFormatter(inner)


def apply_single_line_errors() -> None:
    """Wrap every root stream handler's formatter. Idempotent, fail-open."""
    try:
        for handler in logging.getLogger().handlers:
            if not isinstance(handler, logging.StreamHandler) or isinstance(handler, logging.FileHandler):
                continue
            if isinstance(handler.formatter, _SingleLineErrorFormatter):
                continue
            handler.setFormatter(_SingleLineErrorFormatter(handler.formatter))
            handler.addFilter(_BrowserPrerequisiteFilter())
    except Exception:
        pass
