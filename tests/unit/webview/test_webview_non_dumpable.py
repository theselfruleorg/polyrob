"""2026-09-25 server assessment L9: the webview holds JWT_SECRET_KEY in its
exec-time env; its startup makes the process non-dumpable the same way the
seed processes do (core.security.process_hardening)."""
import inspect


def test_webview_startup_goes_non_dumpable():
    from webview import server
    src = inspect.getsource(server.startup_event)
    assert "harden_custody_process()" in src
    # before any service starts (first statement after the startup log line)
    assert src.index("harden_custody_process()") < src.index("assert_console_posture()")
