"""Regression tests for CLI logging containment (2026-07-13 rendering finalization).

The REPL runs under prompt_toolkit's patch_stdout; ANY raw log record reaching the
terminal corrupts the pinned prompt region. These tests pin the four containment
guarantees: (1) component-logger creation never loosens root/handler levels,
(2) noisy libraries are pinned at setup, (3) console sinks are quiet while the
file sink stays useful, and (4) stderr handlers resolve sys.stderr at emit time.
"""

import io
import logging
import sys

import pytest

import core.logging as core_logging


@pytest.fixture
def fresh_root(monkeypatch):
    """Snapshot the root logger + module state; give the test a clean slate."""
    root = logging.getLogger()
    saved_level = root.level
    saved_handlers = list(root.handlers)
    saved_filters = list(root.filters)
    saved_httpx_level = logging.getLogger("httpx").level
    saved_httpx_client_level = logging.getLogger("httpx._client").level
    saved_httpcore_trace_level = logging.getLogger("httpcore._trace").level
    saved_task_propagate = logging.getLogger("task").propagate
    root.handlers.clear()
    root.filters.clear()
    monkeypatch.setattr(core_logging, "_ROOT_LOGGER_CONFIGURED", False)
    monkeypatch.setattr(core_logging, "_LOGGING_BANNER_PRINTED", True)
    yield root
    for h in root.handlers:
        if h not in saved_handlers:
            try:
                h.close()
            except Exception:
                pass
    root.handlers[:] = saved_handlers
    root.filters[:] = saved_filters
    root.setLevel(saved_level)
    logging.getLogger("httpx").setLevel(saved_httpx_level)
    logging.getLogger("httpx._client").setLevel(saved_httpx_client_level)
    logging.getLogger("httpcore._trace").setLevel(saved_httpcore_trace_level)
    logging.getLogger("task").propagate = saved_task_propagate


def test_component_logger_does_not_loosen_root_or_handlers(fresh_root):
    core_logging.setup_logging(log_level="ERROR")
    assert fresh_root.level == logging.ERROR
    # A brand-new component logger (what get_task_logger mints per session)
    # must NOT bounce root or any handler back to INFO.
    core_logging.get_component_logger("task.agent[5535e149]")
    core_logging.get_component_logger("task.orchestrator[5535e149]")
    assert fresh_root.level == logging.ERROR
    for handler in fresh_root.handlers:
        assert handler.level >= logging.ERROR, f"{handler} loosened to {handler.level}"


def test_explicit_setup_logging_still_relevels(fresh_root):
    core_logging.setup_logging(log_level="ERROR")
    core_logging.setup_logging(log_level="INFO")  # explicit call keeps old semantics
    assert fresh_root.level == logging.INFO


def test_setup_logging_pins_noisy_libraries(fresh_root):
    logging.getLogger("httpx").setLevel(logging.NOTSET)
    core_logging.setup_logging(log_level="INFO")
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore._trace").level == logging.ERROR


def test_task_configure_library_loggers_delegates(fresh_root):
    from agents.task.logging_config import configure_library_loggers

    logging.getLogger("httpx").setLevel(logging.NOTSET)
    configure_library_loggers()
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("task").propagate is False


def _sinks(root):
    return {getattr(h, "_polyrob_sink", None): h for h in root.handlers}


def test_console_level_split_on_first_config(fresh_root):
    core_logging.setup_logging(log_level="INFO", console_level="ERROR")
    sinks = _sinks(fresh_root)
    assert sinks["file"].level == logging.INFO
    assert sinks["console"].level == logging.ERROR
    assert sinks["httpx"].level == logging.ERROR
    assert fresh_root.level == logging.INFO


def test_console_level_split_reapplied_on_reconfig(fresh_root):
    core_logging.setup_logging(log_level="INFO")  # first config: everything INFO
    core_logging.setup_logging(log_level="INFO", console_level="ERROR")
    sinks = _sinks(fresh_root)
    assert sinks["file"].level == logging.INFO
    assert sinks["console"].level == logging.ERROR
    assert sinks["httpx"].level == logging.ERROR


def test_single_arg_call_keeps_legacy_semantics(fresh_root):
    core_logging.setup_logging(log_level="ERROR")
    sinks = _sinks(fresh_root)
    assert sinks["file"].level == logging.ERROR
    assert sinks["console"].level == logging.ERROR
    assert fresh_root.level == logging.ERROR


def test_stderr_handlers_resolve_stream_at_emit_time(fresh_root, monkeypatch):
    core_logging.setup_logging(log_level="INFO")
    fake = io.StringIO()
    monkeypatch.setattr(sys, "stderr", fake)
    # WARNING passes the Task-2 httpx pin; the record must land on the
    # CURRENT sys.stderr (the REPL's patch_stdout proxy in production).
    logging.getLogger("httpx").warning("late-binding-probe")
    assert "late-binding-probe" in fake.getvalue()


def test_httpx_record_emits_exactly_once(fresh_root, monkeypatch):
    core_logging.setup_logging(log_level="INFO")
    fake = io.StringIO()
    monkeypatch.setattr(sys, "stderr", fake)
    logging.getLogger("httpx").warning("dedupe-probe")
    assert fake.getvalue().count("dedupe-probe") == 1
