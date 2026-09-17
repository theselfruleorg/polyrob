"""Keep run-time log errors to one line on non-verbose CLI runs (027 WP3).

`polyrob run` lifts the bootstrap logging.disable after startup so real errors
surface during the run — but a provider failure then spilled multi-page
tracebacks (logger.error(..., exc_info=True) deep in the LLM stack) onto a
first-time user's terminal. This formatter keeps the one-line message and drops
the traceback; `--verbose` skips the squelch entirely.
"""

from __future__ import annotations

import logging


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


_LEVEL_GLYPH = {"WARNING": "⚠", "ERROR": "✗", "CRITICAL": "✗"}


def _short_name(name: str) -> str:
    """``modules.zai-coding_client`` -> ``zai-coding_client``; a session logger
    ``task.executor[1c6e6fa2-…]`` -> ``executor[1c6e6fa2]``."""
    if "[" in name and name.endswith("]"):
        base, _, sid = name[:-1].partition("[")
        return f"{base.rsplit('.', 1)[-1]}[{sid[:8]}]"
    return name.rsplit(".", 1)[-1]


class _SingleLineErrorFormatter(logging.Formatter):
    """One compact line per record: ``✗ <component>: <message>``.

    The wrapped ``inner`` formatter is kept for back-compat with callers that
    built one, but a chat REPL is not a log viewer: the timestamp, the padded
    level column and the emoji prefix the file formatter adds are noise beside
    a transcript, and they pushed a 401 body past the terminal width. Exception
    and stack text are always dropped (that was the original purpose).
    """

    def __init__(self, inner: logging.Formatter | None = None):
        super().__init__()
        self._inner = inner or logging.Formatter()

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if _missing_browser(message):
            message = "Browser runtime missing. Install it with: python -m playwright install chromium"
        text = " ".join(message.split())
        glyph = _LEVEL_GLYPH.get(record.levelname, record.levelname.lower())
        return f"{glyph} {_short_name(record.name)}: {text}".rstrip()


class _CascadeFilter(logging.Filter):
    """Collapse a re-wrapped exception to its FIRST line on the console.

    One 401 from a provider logged five ERROR records on the way up
    (``tool calling error: <e>`` → ``generate_agent_response failed: <e>`` →
    ``API error: <e>`` → ``Failed to generate response: … <e>`` → ``LLM call
    failed … <e>``): every layer re-logs the same exception text inside its
    own prefix. A record whose message CONTAINS the tail of a message this
    handler passed in the last ``window`` seconds is that same error again
    and is dropped here — the file handler still gets every layer.
    """

    def __init__(self, window: float = 3.0, tail: int = 60, keep: int = 8) -> None:
        super().__init__()
        self._window, self._tail, self._keep = window, tail, keep
        self._recent: list[tuple[float, str]] = []

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.WARNING:
            return True
        message = " ".join(record.getMessage().split())
        now = record.created
        self._recent = [(t, m) for t, m in self._recent if now - t < self._window]
        for _, previous in self._recent:
            tail = previous[-self._tail:]
            if len(tail) >= 20 and tail in message:
                return False
        self._recent.append((now, message))
        del self._recent[:-self._keep]
        return True


def single_line_error_formatter(
    inner: logging.Formatter | None = None,
) -> logging.Formatter:
    return _SingleLineErrorFormatter(inner)


def _is_console_handler(handler: logging.Handler) -> bool:
    """Only POLYROB's own terminal sinks — never a FileHandler, and never a
    foreign StreamHandler subclass (pytest's session-long LogCaptureHandler IS
    one; decorating it with the stateful cascade filter leaked across tests)."""
    if isinstance(handler, logging.FileHandler):
        return False
    sink = getattr(handler, "_polyrob_sink", None)
    if sink is not None:
        return sink == "console"
    try:
        from core.logging import DynamicStderrHandler
    except Exception:
        DynamicStderrHandler = ()  # type: ignore[assignment]
    return isinstance(handler, DynamicStderrHandler) or type(handler) is logging.StreamHandler


def apply_single_line_errors() -> None:
    """Wrap every root console handler's formatter. Idempotent, fail-open."""
    try:
        for handler in logging.getLogger().handlers:
            if not _is_console_handler(handler):
                continue
            if isinstance(handler.formatter, _SingleLineErrorFormatter):
                continue
            handler.setFormatter(_SingleLineErrorFormatter(handler.formatter))
            handler.addFilter(_BrowserPrerequisiteFilter())
            handler.addFilter(_CascadeFilter())
    except Exception:
        pass
