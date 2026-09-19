"""Each service identity gets its OWN log file — one shared ``bot.log`` cannot work
across three non-root users.

Since the 2026-09-16 identity cutover ``polyrob.service`` (polyrob-agent),
``polyrob-email.service`` (polyrob-email) and ``polyrob-webview.service``
(polyrob-web) all opened ``<data_home>/logs/bot.log`` through a
RotatingFileHandler. Whoever rotated last owned the file 0644, and the other
two units raised ``PermissionError: '/var/lib/polyrob/logs/bot.log'`` on EVERY
log emit — 760 tracebacks in two hours on prod (2026-09-17 19:30–21:30Z), and
their file logs silently lost. Rotation by rename across owners can never be
made safe, so the file is named per entrypoint instead.
"""
from core.logging import log_file_name


def test_agent_entrypoint_keeps_legacy_name(monkeypatch):
    monkeypatch.delenv("POLYROB_LOG_FILE", raising=False)
    assert log_file_name(["/opt/polyrob/venv/bin/polyrob", "telegram"]) == "bot.log"
    assert log_file_name(["polyrob"]) == "bot.log"
    assert log_file_name([]) == "bot.log"


def test_email_surface_gets_its_own_file(monkeypatch):
    monkeypatch.delenv("POLYROB_LOG_FILE", raising=False)
    assert log_file_name(["/opt/polyrob/venv/bin/polyrob", "email"]) == "email.log"


def test_webview_uvicorn_entrypoint_gets_its_own_file(monkeypatch):
    monkeypatch.delenv("POLYROB_LOG_FILE", raising=False)
    argv = ["/opt/polyrob/venv/lib/python3.12/site-packages/uvicorn/__main__.py",
            "webview.server:app", "--host", "127.0.0.1", "--port", "5050"]
    assert log_file_name(argv) == "webview.log"
    assert log_file_name(["python", "-m", "webview.server_launcher"]) == "webview.log"


def test_env_override_wins_and_is_a_basename(monkeypatch):
    monkeypatch.setenv("POLYROB_LOG_FILE", "../../etc/evil.log")
    assert log_file_name(["polyrob", "email"]) == "evil.log"
    monkeypatch.setenv("POLYROB_LOG_FILE", "  ")
    assert log_file_name(["polyrob", "email"]) == "email.log"
