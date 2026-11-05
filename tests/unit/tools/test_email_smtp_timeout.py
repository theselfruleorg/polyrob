"""SMTP has a socket timeout and leaves the event loop. A retry is for a send where nothing
can have gone out: smtplib turns a timed-out reply into SMTPServerDisconnected, so a catch
of TimeoutError alone misses the case that sends twice (the reply after DATA)."""
import asyncio
import smtplib
import socket
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import tools.email_tool as email_tool
from core.exceptions import APIError, DeliveryUnknownError
from core.surfaces.envelopes import MessageKind, OutboundMessage
from surfaces.email.surface import EmailSurface
from tools.email_tool import EmailSendAction, EmailTool


class _Server:
    """A plain SMTP dialogue on loopback. ``after_body`` picks the end: `hang`, `close`,
    or `ok`. ``drop_on_mail`` closes the socket at MAIL: nothing has gone out."""

    def __init__(self, *, after_body="ok", drop_on_mail=False, silent=False):
        self.after_body, self.drop_on_mail, self.silent = after_body, drop_on_mail, silent
        self.bodies = 0
        self._release = threading.Event()
        self._sock = socket.create_server(("127.0.0.1", 0))
        self.port = self._sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._talk, args=(conn,), daemon=True).start()

    def _talk(self, conn):
        with conn:
            f = conn.makefile("rwb")
            if self.silent:
                self._release.wait(10)
                return

            def say(text):
                f.write(text.encode() + b"\r\n")
                f.flush()
            say("220 test")
            while True:
                line = f.readline().decode(errors="replace").upper()
                if not line:
                    return
                if line.startswith(("EHLO", "HELO")):
                    say("250 test")
                elif line.startswith("MAIL"):
                    if self.drop_on_mail:
                        return
                    say("250 ok")
                elif line.startswith("RCPT"):
                    say("250 ok")
                elif line.startswith("DATA"):
                    say("354 go")
                    while f.readline() not in (b".\r\n", b""):
                        pass
                    self.bodies += 1
                    if self.after_body == "hang":
                        self._release.wait(10)
                        return
                    if self.after_body == "close":
                        return
                    say("250 queued")
                elif line.startswith("QUIT"):
                    say("221 bye")
                    return

    def close(self):
        self._release.set()
        self._sock.close()

    def connect(self, timeout=0.3):
        return smtplib.SMTP("127.0.0.1", self.port, timeout=timeout)


@pytest.fixture
def server_factory():
    made = []

    def _make(**kw):
        made.append(_Server(**kw))
        return made[-1]
    yield _make
    for server in made:
        server.close()


def _tool():
    cfg = SimpleNamespace(gmail_email="agent@example.com", gmail_app_password="test-only",
                          email_enabled=True)
    tool = EmailTool(name="email", config=cfg, container=None)
    tool._initialized = True
    tool._enabled = True
    return tool


def _no_reconnect(tool, monkeypatch):
    calls = []

    async def _connect():
        calls.append(1)
        raise AssertionError("a send that may have gone out must not reconnect")
    monkeypatch.setattr(tool, "_connect_smtp", _connect)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("after_body", ["hang", "close"])
async def test_no_reply_after_the_body_never_sends_twice(server_factory, monkeypatch, after_body):
    server = server_factory(after_body=after_body)
    tool = _tool()
    tool.smtp_connection = server.connect()
    calls = _no_reconnect(tool, monkeypatch)
    with pytest.raises(DeliveryUnknownError, match="may have gone out"):
        await tool.send_email("x@example.com", "Hi", "body")
    assert server.bodies == 1 and not calls
    assert tool.smtp_connection is None


@pytest.mark.asyncio
async def test_a_plain_timeout_error_does_not_retry(monkeypatch):
    class _Slow:
        def send_message(self, msg):
            raise TimeoutError("timed out")
    tool = _tool()
    tool.smtp_connection = _Slow()
    calls = _no_reconnect(tool, monkeypatch)
    with pytest.raises(DeliveryUnknownError):
        await tool.send_email("x@example.com", "Hi", "body")
    assert not calls


@pytest.mark.asyncio
async def test_a_timeout_with_no_data_step_to_see_reports_unknown(server_factory, monkeypatch):
    class _Slow:
        def send_message(self, msg):
            try:
                raise TimeoutError("timed out")
            except OSError:
                raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed: timed out")
    tool = _tool()
    tool.smtp_connection = _Slow()
    calls = _no_reconnect(tool, monkeypatch)
    with pytest.raises(DeliveryUnknownError):
        await tool.send_email("x@example.com", "Hi", "body")
    assert not calls


@pytest.mark.asyncio
async def test_a_timeout_before_data_retries_and_the_mail_is_sent(server_factory, monkeypatch):
    """Nothing can have gone out before DATA. Unknown here would mark the mail delivered
    on the outbound queue, and the mail would be lost."""
    class _HalfOpen:
        def data(self, msg):
            raise AssertionError("the MAIL step timed out first")

        def send_message(self, msg):
            raise TimeoutError("timed out")

        def close(self):
            pass
    fresh = server_factory()
    tool = _tool()
    tool.smtp_connection = _HalfOpen()

    async def _connect():
        tool.smtp_connection = fresh.connect()
    monkeypatch.setattr(tool, "_connect_smtp", _connect)
    assert await tool.send_email("x@example.com", "Hi", "body") is True
    assert fresh.bodies == 1


@pytest.mark.asyncio
async def test_a_stale_connection_before_data_still_retries_once(server_factory, monkeypatch):
    stale, fresh = server_factory(drop_on_mail=True), server_factory()
    tool = _tool()
    tool.smtp_connection = stale.connect()

    async def _connect():
        tool.smtp_connection = fresh.connect()
    monkeypatch.setattr(tool, "_connect_smtp", _connect)
    assert await tool.send_email("x@example.com", "Hi", "body") is True
    assert stale.bodies == 0 and fresh.bodies == 1


@pytest.mark.asyncio
async def test_a_server_refusal_after_the_body_is_not_unknown(monkeypatch):
    """A 5xx after DATA is a definite answer: the old retry and the old error stay."""
    class _Refusing:
        def send_message(self, msg):
            raise smtplib.SMTPDataError(554, b"refused")

    tool = _tool()
    tool.smtp_connection = _Refusing()

    async def _connect():
        tool.smtp_connection = _Refusing()
    monkeypatch.setattr(tool, "_connect_smtp", _connect)
    with pytest.raises(APIError) as caught:
        await tool.send_email("x@example.com", "Hi", "body")
    assert not isinstance(caught.value, DeliveryUnknownError)


@pytest.mark.asyncio
async def test_the_send_leaves_the_event_loop(monkeypatch):
    seen = {}

    class _Recording:
        def send_message(self, msg):
            seen["thread"] = threading.current_thread()
    tool = _tool()
    tool.smtp_connection = _Recording()
    await tool.send_email("x@example.com", "Hi", "body")
    assert seen["thread"] is not threading.main_thread()


@pytest.mark.asyncio
async def test_connect_carries_the_timeout_and_assigns_only_after_login(monkeypatch):
    conn = MagicMock()
    conn.login.side_effect = smtplib.SMTPAuthenticationError(535, b"no")
    smtp = MagicMock(return_value=conn)
    monkeypatch.setattr(email_tool.smtplib, "SMTP", smtp)
    monkeypatch.setattr("core.credential_verdicts.record_rejection",
                        lambda *a, **k: SimpleNamespace(count=2))
    tool = _tool()
    with pytest.raises(Exception, match="authentication failed"):
        await tool._connect_smtp()
    assert smtp.call_args.kwargs["timeout"] == email_tool.SMTP_TIMEOUT_S
    assert tool.smtp_connection is None
    conn.close.assert_called_once()


@pytest.mark.asyncio
async def test_a_silent_server_times_out_off_the_loop(server_factory, monkeypatch):
    monkeypatch.setattr(email_tool, "SMTP_TIMEOUT_S", 0.3)
    server = server_factory(silent=True)
    tool = _tool()
    tool.smtp_server, tool.smtp_port = "127.0.0.1", server.port
    worst, last = 0.0, time.monotonic()

    async def _beat():
        nonlocal worst, last
        while True:
            await asyncio.sleep(0.02)
            now = time.monotonic()
            worst, last = max(worst, now - last), now
    beat = asyncio.create_task(_beat())
    try:
        with pytest.raises(APIError, match="SMTP connection failed"):
            await asyncio.wait_for(tool._connect_smtp(), timeout=5)
    finally:
        beat.cancel()
    assert worst < 0.25
    assert tool.smtp_connection is None


@pytest.mark.asyncio
async def test_the_probe_runs_off_the_loop_with_the_timeout(monkeypatch):
    conn, seen = MagicMock(), {}

    def _smtp(*a, **k):
        seen.update(k, thread=threading.current_thread())
        return conn
    monkeypatch.setattr(email_tool.smtplib, "SMTP", _smtp)
    monkeypatch.setattr("core.credential_verdicts.clear_kind", lambda *_: None)
    tool = _tool()
    monkeypatch.setattr(tool, "_smtp_verdict_refusal", lambda: None)
    await tool._test_smtp_connection()
    assert seen["timeout"] == email_tool.SMTP_TIMEOUT_S
    assert seen["thread"] is not threading.main_thread()
    conn.quit.assert_called_once()


@pytest.mark.asyncio
async def test_a_cancelled_send_gives_up_the_connection():
    """The worker thread still owns the socket: the next send must not write to it."""
    gate = threading.Event()

    class _Blocking:
        def send_message(self, msg):
            gate.wait(5)
    tool = _tool()
    tool.smtp_connection = _Blocking()
    task = asyncio.create_task(tool.send_email("x@example.com", "Hi", "body"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.set()
    assert tool.smtp_connection is None


@pytest.mark.asyncio
async def test_the_action_says_delivery_is_unknown_not_failed(monkeypatch):
    monkeypatch.setenv("POLYROB_OWNER_EMAIL", "owner@example.com")
    tool = _tool()
    tool.container = SimpleNamespace(get_service=lambda name: None)

    async def _unknown(*a, **k):
        raise DeliveryUnknownError("delivery unknown: it may have gone out")
    monkeypatch.setattr(tool, "send_email_ex", _unknown)
    res = await tool.email_send(EmailSendAction(to="owner@example.com", subject="s", body="b"),
                                execution_context=SimpleNamespace(
                                    user_id="u1", session_id="s1", is_sub_agent=False,
                                    role="orchestrator", metadata={}))
    assert res.error == "delivery unknown: it may have gone out"


@pytest.mark.asyncio
async def test_the_outbound_queue_does_not_retry_an_unknown_delivery():
    """``remaining_text=""`` is how a surface tells the queue that nothing is owed."""
    class _Sender:
        async def send_email_ex(self, *a, **k):
            raise DeliveryUnknownError("delivery unknown: it may have gone out")
    res = await EmailSurface(_Sender()).send(OutboundMessage(
        session_key="agent:main:email:dm:someone@example.com", text="body",
        kind=MessageKind.AGENT_TEXT))
    assert res.success is False and res.remaining_text == ""
    assert "may have gone out" in res.error
    from core.surfaces.outbound_dispatcher import unsent_text
    assert unsent_text("body", res.remaining_text) == ""


@pytest.mark.asyncio
async def test_another_surface_failure_stays_retryable():
    class _Sender:
        async def send_email_ex(self, *a, **k):
            raise APIError("SMTP connection failed")
    res = await EmailSurface(_Sender()).send(OutboundMessage(
        session_key="agent:main:email:dm:someone@example.com", text="body",
        kind=MessageKind.AGENT_TEXT))
    assert res.success is False and res.remaining_text is None
