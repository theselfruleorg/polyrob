"""Validation follow-up (2026-07-23): a logger that owns its handlers and never
propagates to root bypasses setup_logging's handler-level SecretScrubbingFilter
undisclosed — the webview server_launcher's root handlers must carry the filter.
(The "config" logger half was retired 2026-08-28: ``ServerConfig._setup_logger``
had no live caller, so that surface never existed at runtime.)"""
import logging

from core.security_logging_filter import SecretScrubbingFilter


def _has_scrub_filter(handler) -> bool:
    return any(isinstance(f, SecretScrubbingFilter) for f in handler.filters)


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
