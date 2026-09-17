"""The REPL's console log line: compact, one per error, no re-wrapped repeats."""
import logging
import sys

from cli.ui.log_squelch import _CascadeFilter, single_line_error_formatter


def _rec(msg, name="modules.zai-coding_client", level=logging.ERROR, created=None):
    r = logging.LogRecord(name, level, __file__, 1, msg, (), None)
    if created is not None:
        r.created = created
    return r


def test_console_line_is_compact():
    f = single_line_error_formatter()
    line = f.format(_rec("tool calling error: Error code: 401 - {'error': 'x'}"))
    assert line == "✗ zai-coding_client: tool calling error: Error code: 401 - {'error': 'x'}"
    assert f.format(_rec("careful", level=logging.WARNING)).startswith("⚠ ")


def test_session_logger_name_is_shortened():
    f = single_line_error_formatter()
    line = f.format(_rec("LLM call failed", name="task.executor[1c6e6fa2-da47-4722-a69c]"))
    assert line.startswith("✗ executor[1c6e6fa2]: ")


def test_exception_text_dropped():
    f = single_line_error_formatter()
    try:
        raise ValueError("boom")
    except ValueError:
        r = logging.LogRecord("x", logging.ERROR, __file__, 1, "it broke", (), sys.exc_info())
    out = f.format(r)
    assert "it broke" in out and "Traceback" not in out and "\n" not in out


def test_cascade_collapses_to_first_line():
    err = "Error code: 401 - {'error': {'message': 'token expired or incorrect', 'type': '401'}}"
    flt = _CascadeFilter()
    msgs = [
        f"z.ai GLM Coding Plan tool calling error: {err}",
        f"generate_agent_response failed: {err}",
        f"z.ai GLM Coding Plan API error: {err}",
        f"Failed to generate response: z.ai GLM Coding Plan API error: {err}",
        f"LLM call failed after 1 attempts for provider=zai-coding: LLMAuthenticationError: {err}",
    ]
    kept = [m for m in msgs if flt.filter(_rec(m, created=100.0))]
    assert kept == [msgs[0]]


def test_cascade_keeps_a_different_error_and_expires():
    flt = _CascadeFilter(window=3.0)
    assert flt.filter(_rec("Failed to validate Anthropic connection: Error code: 400 - credit balance too low", created=100.0))
    assert flt.filter(_rec("Gemini function_call missing name. Type: FunctionCall", created=100.5))
    # Same error again after the window is a new occurrence.
    assert flt.filter(_rec("Failed to initialize: Failed to validate Anthropic connection: Error code: 400 - credit balance too low", created=104.0))


def test_cascade_ignores_info():
    flt = _CascadeFilter()
    assert flt.filter(_rec("same", level=logging.INFO, created=1.0))
    assert flt.filter(_rec("same", level=logging.INFO, created=1.0))


def test_apply_touches_only_polyrob_console_handlers():
    import logging as _l
    from cli.ui.log_squelch import _CascadeFilter, apply_single_line_errors
    from core.logging import DynamicStderrHandler

    class _Foreign(_l.StreamHandler):  # pytest's LogCaptureHandler has this shape
        pass

    root = _l.getLogger()
    ours, foreign, marked_file = DynamicStderrHandler(), _Foreign(), _l.StreamHandler()
    marked_file._polyrob_sink = "file"
    for h in (ours, foreign, marked_file):
        root.addHandler(h)
    try:
        apply_single_line_errors()
        assert any(isinstance(f, _CascadeFilter) for f in ours.filters)
        assert not any(isinstance(f, _CascadeFilter) for f in foreign.filters)
        assert not any(isinstance(f, _CascadeFilter) for f in marked_file.filters)
    finally:
        for h in (ours, foreign, marked_file):
            root.removeHandler(h)
