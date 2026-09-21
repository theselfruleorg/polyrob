"""A rejected SMTP login is remembered and not retried for a while.

Prod 2026-09-18: with a stale Gmail app password the email surface re-tested
the login every minute and every agent session tested it again at start —
5,071 rejected logins in 24 h (59/h from polyrob-email alone), each dumping a
12-line traceback and hammering Gmail with bad credentials. The tool must
still fail closed (no email), but after one 535 it fails FAST from memory
until the backoff expires, then probes again.

057 WS-F moved that memory into the durable core-tier verdict store, so the
backoff also survives a restart and is shared by the three service units that
share one data dir — and the refusal costs ONE WARNING per process per
outage instead of an ERROR per call.
"""
import smtplib
import time
import types
from unittest.mock import MagicMock

import pytest

from core import credential_verdicts as cv
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


@pytest.fixture
def _short_backoff(monkeypatch):
    """Make the SMTP re-probe window tiny so the lapse is testable in real time."""
    monkeypatch.setitem(cv.DEFAULT_TTL_BY_KIND, "smtp", 0.05)
    return 0.05


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
async def test_probe_resumes_after_the_backoff_window(monkeypatch, _short_backoff):
    ctor = _rejecting_smtp(monkeypatch)
    t = _tool()
    with pytest.raises(Exception):
        await t._test_smtp_connection()
    time.sleep(_short_backoff + 0.02)
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
async def test_success_clears_a_cached_failure(monkeypatch, _short_backoff):
    ctor = _rejecting_smtp(monkeypatch)
    t = _tool()
    with pytest.raises(Exception):
        await t._test_smtp_connection()
    ok = MagicMock()
    monkeypatch.setattr(et.smtplib, "SMTP", MagicMock(return_value=ok))
    time.sleep(_short_backoff + 0.02)  # past the backoff
    await t._test_smtp_connection()
    assert not et._SMTP_AUTH_FAILURES


# --- 057 WS-F ------------------------------------------------------------------
@pytest.mark.asyncio
async def test_verdict_survives_a_new_tool_instance(monkeypatch):
    """A restart used to forget the 535 and re-probe. The store does not."""
    ctor = _rejecting_smtp(monkeypatch)
    with pytest.raises(Exception, match="535"):
        await _tool()._test_smtp_connection()
    with pytest.raises(Exception, match="backoff"):
        await _tool()._test_smtp_connection()   # a FRESH tool, i.e. a new process
    assert ctor.call_count == 1


@pytest.mark.asyncio
async def test_refusal_names_when_the_outage_started(monkeypatch):
    _rejecting_smtp(monkeypatch)
    t = _tool()
    with pytest.raises(Exception):
        await t._test_smtp_connection()
    refusal = t._smtp_verdict_refusal()
    assert "since" in refusal and "fix the app password" in refusal
    assert "1 rejection(s)" in refusal


@pytest.mark.asyncio
async def test_initialize_refuses_without_a_probe_and_warns_once(monkeypatch, caplog):
    """A dead SMTP rail costs ONE WARNING per process, not an ERROR per caller."""
    ctor = _rejecting_smtp(monkeypatch)
    with pytest.raises(Exception):
        await _tool()._test_smtp_connection()
    with caplog.at_level("DEBUG"):
        for _ in range(5):
            t = _tool()
            with pytest.raises(Exception, match="backoff"):
                await t.initialize()
    assert ctor.call_count == 1, "initialize() must not re-probe while the verdict is live"
    warns = [r for r in caplog.records if r.levelname == "WARNING" and "backoff" in r.message]
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(warns) == 1, f"one WARNING per outage, got {len(warns)}"
    assert errors == [], "a known-dead rail is not an ERROR on every call"


@pytest.mark.asyncio
async def test_ensure_imap_ignores_a_live_smtp_verdict(monkeypatch):
    """Inbound mail keeps flowing while the send half is refused."""
    ctor = _rejecting_smtp(monkeypatch)
    t = _tool()
    with pytest.raises(Exception):
        await t._test_smtp_connection()
    await t.ensure_imap()          # no raise, no probe
    assert ctor.call_count == 1
    with pytest.raises(Exception, match="backoff"):
        await t.ensure_smtp()
    assert ctor.call_count == 1


@pytest.mark.asyncio
async def test_ensure_imap_still_requires_credentials():
    cfg = types.SimpleNamespace(gmail_email="", gmail_app_password="",
                                gmail_smtp_server="smtp.test", gmail_smtp_port=587,
                                gmail_imap_server="imap.test")
    t = EmailTool(name="email", config=cfg, container=None)
    with pytest.raises(Exception, match="credentials"):
        await t.ensure_imap()


@pytest.mark.asyncio
async def test_email_auth_rejected_event_fires_once_per_outage(monkeypatch):
    events = []
    import core.event_log as evl
    monkeypatch.setattr(evl, "emit",
                        lambda kind, **kw: events.append((kind, kw)))
    _rejecting_smtp(monkeypatch)
    for _ in range(3):
        # a new tool each time = a restart; the verdict (and the event) is not re-minted
        with pytest.raises(Exception):
            await _tool()._test_smtp_connection()
    assert [k for k, _ in events] == ["email_auth_rejected"], (
        "105 identical events in 24 h said nothing the first one did not")
    assert events[0][1]["attrs"]["code"] == "535"


@pytest.mark.asyncio
async def test_consecutive_rejections_lengthen_the_hold(monkeypatch):
    """Intel inbox 2026-09-19: a flat 900 s hold was shorter than the 20–35 min cron
    cadence, so a password revoked for days was re-probed on every run. Each repeat
    doubles the hold; inside it no login attempt reaches the network."""
    ctor = _rejecting_smtp(monkeypatch)
    t = _tool()
    now = [1_000_000.0]
    monkeypatch.setattr(cv.time, "time", lambda: now[0])
    for probes in (1, 2, 3):
        with pytest.raises(Exception, match="535"):
            await t._test_smtp_connection()
        assert ctor.call_count == probes
        now[0] += et.SMTP_AUTH_BACKOFF_SEC * (2 ** (probes - 1)) - 1
        with pytest.raises(Exception, match="backoff"):
            await t._test_smtp_connection()
        assert ctor.call_count == probes, "still inside the doubled hold — no network"
        now[0] += 2


@pytest.mark.asyncio
async def test_a_changed_password_is_probed_immediately_and_a_success_clears_the_old_row(monkeypatch):
    """The verdict is keyed on a digest of the password: the owner's fix must not wait
    out a six-hour hold earned by the OLD password."""
    ctor = _rejecting_smtp(monkeypatch)
    t = _tool()
    for _ in range(3):
        with pytest.raises(Exception):
            await t._test_smtp_connection()
    assert ctor.call_count == 1, "hold is live for the bad password"
    assert cv.rejected_within("smtp", et.SMTP_AUTH_BACKOFF_SEC)
    t.config.gmail_app_password = "good"
    ok = MagicMock()
    monkeypatch.setattr(et.smtplib, "SMTP", MagicMock(return_value=ok))
    await t._test_smtp_connection()  # probes at once, no backoff refusal
    ok.login.assert_called_once()
    assert not cv.rejected_within("smtp", et.SMTP_AUTH_BACKOFF_SEC), \
        "a working login clears the whole smtp kind, the old password's row included"
    assert not cv.active("smtp")
