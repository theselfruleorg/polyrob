"""EM1 — blocking imaplib never runs on the event loop, and every IMAP socket
carries a timeout, so a half-open server cannot freeze the gateway loop."""
import asyncio
import threading

import tools.email_tool as et
from surfaces.email.fetchers import ImapFetcher


class _Conn:
    def __init__(self):
        self.threads = []

    def _mark(self):
        self.threads.append(threading.current_thread())

    def select(self, folder):
        self._mark()
        return ("OK", [b"1"])

    def search(self, charset, criteria):
        self._mark()
        return ("OK", [b"1"])

    def fetch(self, num, spec):
        self._mark()
        return ("OK", [(b"1", b"From: a@b.c\r\nSubject: hi\r\n\r\nbody")])

    def store(self, *a):
        self._mark()
        return ("OK", [])


class _Tool:
    def __init__(self, conn):
        self.imap_connection = conn

    async def ensure_imap(self):
        return None

    async def _connect_imap(self):
        raise AssertionError("connected already")


def test_fetcher_runs_imap_off_the_loop():
    conn = _Conn()
    fetcher = ImapFetcher(_Tool(conn))

    async def go():
        out = await fetcher.fetch_unread()
        await fetcher.mark_handled_async(b"1")
        return out

    out = asyncio.run(go())
    assert len(out) == 1
    assert conn.threads and all(t is not threading.main_thread() for t in conn.threads)


def test_connect_imap_sets_timeout_and_runs_off_the_loop(monkeypatch):
    seen = {}

    class _FakeSSL:
        def __init__(self, host, *a, **kw):
            seen["timeout"] = kw.get("timeout")
            seen["thread"] = threading.current_thread()

        def login(self, user, pw):
            seen["login_thread"] = threading.current_thread()

    monkeypatch.setattr(et.imaplib, "IMAP4_SSL", _FakeSSL)
    tool = et.EmailTool.__new__(et.EmailTool)
    tool.imap_server = "imap.example.com"
    tool.imap_connection = None
    tool.config = type("C", (), {"gmail_email": "a@b.c", "gmail_app_password": "p"})()
    asyncio.run(tool._connect_imap())
    assert seen["timeout"] == et.IMAP_TIMEOUT_S and et.IMAP_TIMEOUT_S <= 60
    assert seen["thread"] is not threading.main_thread()
    assert seen["login_thread"] is not threading.main_thread()
    assert isinstance(tool.imap_connection, _FakeSSL)


def test_failed_login_leaves_no_half_open_handle(monkeypatch):
    class _FakeSSL:
        def __init__(self, host, *a, **kw):
            pass

        def login(self, user, pw):
            raise et.imaplib.IMAP4.error("AUTHENTICATIONFAILED")

        def logout(self):
            pass

    monkeypatch.setattr(et.imaplib, "IMAP4_SSL", _FakeSSL)
    tool = et.EmailTool.__new__(et.EmailTool)
    tool.imap_server = "imap.example.com"
    tool.imap_connection = None
    tool.config = type("C", (), {"gmail_email": "a@b.c", "gmail_app_password": "p"})()
    try:
        asyncio.run(tool._connect_imap())
    except et.AuthenticationError:
        pass
    else:
        raise AssertionError("expected AuthenticationError")
    assert tool.imap_connection is None
