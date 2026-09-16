"""A12 fix round 3 (2026-09-14, 043 review): ``Browser.__del__``'s structure bug.

The round-2 fix wrapped the "browser wasn't properly closed" warning in
``if sys is not None and not sys.is_finalizing(): ... else: ...`` — but the
``else:`` bound to that OUTER condition, not to the inner
``if hasattr(self, 'logger'):``. Two consequences, both wrong:

1. During interpreter finalization, the outer ``if`` is False, so the
   ``else:`` fired — calling the bare, unguarded ``logging.warning(...)``
   during finalization, which is EXACTLY the crash this guard exists to
   prevent (a shutdown-time log call can walk into
   ``RotatingFileHandler.emit -> shouldRollover -> _open -> import os``,
   raising ``ImportError``, which ``logging.Handler.handleError`` prints as
   its own multi-line ``"--- Logging error ---"`` dump).
2. During NORMAL (non-finalizing) garbage collection with no ``self.logger``
   attribute set, the outer ``if`` is True but the inner
   ``if hasattr(self, 'logger')`` is False — and since the ``else:`` was
   bound to the OUTER ``if``, not the inner one, NEITHER branch ran: the
   fallback warning was silently dropped.

The fix: an early ``if sys is not None and sys.is_finalizing(): return`` as
the very first statement, then a correctly-nested
``if hasattr(self, "logger"): ... else: ...``.
"""
from unittest.mock import patch

from tools.browser.browser import Browser


def _make_bare_browser(*, with_logger: bool):
    """A ``Browser`` instance built via ``object.__new__`` (bypassing
    ``BaseTool.__init__``'s config/container requirement) with only the
    attributes ``__del__`` reads: an active ``_browser`` handle (so the
    warning branch is reached at all) and ``_started_xvfb=False`` (so the
    unrelated Xvfb cleanup tail is a no-op)."""
    b = object.__new__(Browser)
    b._browser = object()  # any truthy sentinel — __del__ never calls it
    b._playwright = object()
    b._started_xvfb = False
    if with_logger:
        import logging as _logging
        b.logger = _logging.getLogger("test-a12-del-finalization")
    return b


def test_del_during_finalization_logs_nothing(caplog):
    """``sys.is_finalizing() -> True`` must short-circuit __del__ before any
    logging call is attempted — not fall through to the bare
    ``logging.warning`` the round-2 bug produced."""
    browser = _make_bare_browser(with_logger=True)

    caplog.set_level("WARNING")
    with patch("tools.browser.browser.sys.is_finalizing", return_value=True):
        browser.__del__()

    assert caplog.records == [], (
        f"__del__ logged during finalization: "
        f"{[(r.name, r.levelname, r.getMessage()) for r in caplog.records]}"
    )
    # The nullify + Xvfb tail must ALSO be skipped (the early return is
    # unconditional, not just around the logging call) — _browser is still
    # the sentinel we set, not None.
    assert browser._browser is not None


def test_del_normal_gc_no_logger_falls_back_to_module_logging(caplog):
    """Not finalizing + no ``self.logger`` -> the ``else:`` branch (bare
    ``logging.warning``) must fire — the round-2 bug silently dropped this
    because the ``else:`` was bound to the wrong ``if``."""
    browser = _make_bare_browser(with_logger=False)
    assert not hasattr(browser, "logger")

    caplog.set_level("WARNING")
    with patch("tools.browser.browser.sys.is_finalizing", return_value=False):
        browser.__del__()

    assert len(caplog.records) == 1, (
        f"expected exactly one fallback warning, got: "
        f"{[(r.name, r.levelname, r.getMessage()) for r in caplog.records]}"
    )
    record = caplog.records[0]
    assert record.levelname == "WARNING"
    assert "not properly closed" in record.getMessage()
    # Cleanup still ran (this path is NOT the early-return one).
    assert browser._browser is None


def test_del_normal_gc_with_logger_warns_via_self_logger(caplog):
    """Not finalizing + a real ``self.logger`` -> the warning goes through
    ``self.logger.warning``, not the bare module-level fallback."""
    browser = _make_bare_browser(with_logger=True)

    caplog.set_level("WARNING", logger="test-a12-del-finalization")
    with patch("tools.browser.browser.sys.is_finalizing", return_value=False):
        browser.__del__()

    records = [r for r in caplog.records if r.name == "test-a12-del-finalization"]
    assert len(records) == 1
    assert "not properly closed" in records[0].getMessage()
    assert browser._browser is None
