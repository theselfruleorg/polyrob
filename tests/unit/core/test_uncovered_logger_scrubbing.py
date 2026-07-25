"""Validation follow-up (2026-07-23): two loggers owned their handlers and
never propagated to root, so they bypassed setup_logging's handler-level
SecretScrubbingFilter undisclosed — the "config" logger (core/config.py) and
the webview server_launcher's root handlers. Both must carry the filter."""
import logging

from core.security_logging_filter import SecretScrubbingFilter


def _has_scrub_filter(handler) -> bool:
    return any(isinstance(f, SecretScrubbingFilter) for f in handler.filters)


def test_config_logger_handlers_carry_secret_filter(tmp_path):
    from core.config import ServerConfig

    class _Shim:
        # reuse the real method on a minimal stand-in — constructing the full
        # pydantic ServerConfig has side effects (data dirs, env parsing)
        _setup_logger = ServerConfig._setup_logger
        base_dir = ""
        log_level = "INFO"

    shim = _Shim()
    shim.base_dir = str(tmp_path)
    cfg_logger = logging.getLogger("config")
    saved = list(cfg_logger.handlers)
    cfg_logger.handlers.clear()
    try:
        shim._setup_logger()
        handlers = cfg_logger.handlers
        assert handlers, "config logger should have gotten handlers"
        for h in handlers:
            assert _has_scrub_filter(h), f"{h} lacks SecretScrubbingFilter"
    finally:
        for h in list(cfg_logger.handlers):
            cfg_logger.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
        cfg_logger.handlers.extend(saved)


def test_server_launcher_root_handlers_carry_secret_filter(tmp_path, monkeypatch):
    monkeypatch.setenv("WEBVIEW_INSTALL_PREFIX", str(tmp_path))
    from webview import server_launcher

    root = logging.getLogger("")
    before = list(root.handlers)
    try:
        server_launcher.setup_logging("INFO")
        added = [h for h in root.handlers if h not in before]
        assert added, "server_launcher should add a file handler to root"
        for h in added:
            assert _has_scrub_filter(h), f"{h} lacks SecretScrubbingFilter"
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
                try:
                    h.close()
                except Exception:
                    pass
        # setup_logging also attaches the filter to pre-existing root handlers
        # (pytest capture) — strip those so this test leaves no trace.
        for h in before:
            for f in list(h.filters):
                if isinstance(f, SecretScrubbingFilter):
                    h.removeFilter(f)
