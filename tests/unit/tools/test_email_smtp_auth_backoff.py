"""A rejected SMTP login is remembered process-wide and not retried for a while.

Prod 2026-09-18: with a stale Gmail app password the email surface re-tested
the login every minute and every agent session tested it again at start —
5,071 rejected logins in 24 h (59/h from polyrob-email alone), each dumping a
12-line traceback and hammering Gmail with bad credentials. The tool must
still fail closed (no email), but after one 535 it fails FAST from memory
until the backoff expires, then probes again.
"""
import smtplib
import types
from unittest.mock import MagicMock

import pytest

from tools import email_tool as et
from tools.email_tool import EmailTool


def _tool():
    cfg = types.SimpleNamespace(gmail_email="bot@example.com", gmail_app_password="bad",
                                gmail_smtp_server="smtp.test", gmail_smtp_port=587,
                                gmail_imap_server="imap.test")
    return EmailTool(name="email", config=cfg, container=None)


@pytest.fixture(autouse=True)
def _clear_backoff():
    et._SMTP_AUTH_FAILURES.clear()
    yield
    et._SMTP_AUTH_FAILURES.clear()


def _rejecting_smtp(monkeypatch):
    server = MagicMock()
    server.login.side_effect = smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")
    ctor = MagicMock(return_value=server)
    monkeypatch.setattr(et.smtplib, "SMTP", ctor)
    return ctor


@pytest.mark.asyncio
async def test_second_probe_within_backoff_does_not_touch_the_network(monkeypatch):
    ctor = _rejecting_smtp(monkeypatch)
    t = _tool()
    with pytest.raises(Exception, match="535"):
        await t._test_smtp_connection()
    assert ctor.call_count == 1
    with pytest.raises(Exception, match="backoff"):
        await t._test_smtp_connection()
    assert ctor.call_count == 1, "the cached 535 must answer without a new login attempt"


@pytest.mark.asyncio
async def test_probe_resumes_after_the_backoff_window(monkeypatch):
    ctor = _rejecting_smtp(monkeypatch)
    t = _tool()
    now = [1000.0]
    monkeypatch.setattr(et.time, "monotonic", lambda: now[0])
    with pytest.raises(Exception):
        await t._test_smtp_connection()
    now[0] += et.SMTP_AUTH_BACKOFF_SEC + 1
    with pytest.raises(Exception, match="535"):
        await t._test_smtp_connection()
    assert ctor.call_count == 2


@pytest.mark.asyncio
async def test_non_auth_failures_are_not_cached(monkeypatch):
    server = MagicMock()
    server.starttls.side_effect = OSError("network down")
    ctor = MagicMock(return_value=server)
    monkeypatch.setattr(et.smtplib, "SMTP", ctor)
    t = _tool()
    for _ in range(2):
        with pytest.raises(Exception, match="network down"):
            await t._test_smtp_connection()
    assert ctor.call_count == 2, "a transient network error must be retried, only a 535 is cached"


@pytest.mark.asyncio
async def test_success_clears_a_cached_failure(monkeypatch):
    ctor = _rejecting_smtp(monkeypatch)
    t = _tool()
    with pytest.raises(Exception):
        await t._test_smtp_connection()
    ok = MagicMock()
    monkeypatch.setattr(et.smtplib, "SMTP", MagicMock(return_value=ok))
    monkeypatch.setattr(et.time, "monotonic", lambda: 10**9)  # past the backoff
    await t._test_smtp_connection()
    assert not et._SMTP_AUTH_FAILURES
