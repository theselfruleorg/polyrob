"""Regression: the interactive-terminal formatter must interpolate %-style args.

``ComfyFormatter`` rendered ``str(record.msg)`` on a TTY, so a call like
``logger.warning("providers.yaml unreadable (%s)", exc)`` printed a literal
``%s`` and dropped the exception — the one detail the operator needed. The
non-TTY path (``super().format``) was always right; only the console was wrong.
"""
import logging
import sys
from unittest.mock import patch

from core.logging import ComfyFormatter


def _record(level, msg, args):
    return logging.LogRecord("modules.llm.provider_spec", level, "x.py", 1, msg, args, None)


def _format_on_tty(record):
    with patch.object(sys.stdout, "isatty", return_value=True):
        return ComfyFormatter().format(record)


def test_warning_args_are_interpolated_on_a_tty():
    out = _format_on_tty(_record(
        logging.WARNING, "providers.yaml unreadable (%s) — no user providers loaded",
        (PermissionError("ancestor dir is group-writable"),)))
    assert "%s" not in out
    assert "ancestor dir is group-writable" in out


def test_info_args_are_interpolated_on_a_tty():
    out = _format_on_tty(_record(logging.INFO, "loaded %d providers from %s", (2, "providers.yaml")))
    assert "loaded 2 providers from providers.yaml" in out


def test_message_without_args_is_unchanged():
    out = _format_on_tty(_record(logging.WARNING, "100% literal, no args", None))
    assert "100% literal, no args" in out
