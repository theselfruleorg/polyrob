"""A DEAD IMAP socket must not blind the rail forever.

⚠️ Observed on prod 2026-09-23: `polyrob-email` fetched fine at 06:08, then from
06:16:37 logged *"IMAP read failed: abort: socket error: EOF occurred in
violation of protocol"* **once a minute, ten times, with zero recoveries** — and
it would have kept doing so indefinitely. Meanwhile three consecutive manual
`IMAP4_SSL` logins from the same host with the same credential succeeded, so
neither the mailbox nor the network was at fault.

The cause is the reconnect condition:

    if not getattr(tool, "imap_connection", None):
        await tool._connect_imap()

Gmail closes an idle IMAP socket, but the `IMAP4_SSL` OBJECT survives and stays
truthy — so the guard never fires, every poll reuses the dead handle, and the
rail never heals. The rail's own error text says *"no new mail cannot be
distinguished from this"*, which is honest and exactly why this matters: the
owner's inbound mail goes unread and nothing says so except a log line.

So a non-auth read failure now DROPS the handle and reconnects once. An AUTH
failure deliberately does not retry — the credential is wrong, hammering the
mailbox is how you get the account throttled, and the durable verdict is the
right answer there.
"""
import pytest

from core.exceptions import AuthenticationError
from surfaces.email.fetchers import ImapFetcher, MailFetchError


class _Conn:
    """An IMAP connection that is dead until the tool reconnects."""

    def __init__(self, alive, unseen=b""):
        self.alive = alive
        self.unseen = unseen
        self.selects = 0

    def select(self, folder):
        self.selects += 1
        if not self.alive:
            raise OSError("socket error: EOF occurred in violation of protocol")
        return ("OK", [b"1"])

    def search(self, charset, criteria):
        return ("OK", [self.unseen])

    def fetch(self, num, spec):
        raise AssertionError("no message in these fixtures")


class _Tool:
    def __init__(self, first, second=None, auth_error=False):
        self.imap_connection = first
        self._next = second
        self.connects = 0
        self._auth_error = auth_error

    async def ensure_imap(self):
        return None

    async def _connect_imap(self):
        self.connects += 1
        if self._auth_error:
            raise AuthenticationError("AUTHENTICATIONFAILED invalid credentials")
        self.imap_connection = self._next


@pytest.mark.asyncio
async def test_a_dead_socket_is_dropped_and_the_poll_reconnects():
    """The whole defect: one dead handle must not blind the rail forever."""
    dead, fresh = _Conn(alive=False), _Conn(alive=True)
    tool = _Tool(dead, fresh)
    assert await ImapFetcher(tool).fetch_unread() == []
    assert tool.connects == 1              # it reconnected
    assert tool.imap_connection is fresh   # and kept the new handle
    assert fresh.selects == 1              # and actually re-read the mailbox


@pytest.mark.asyncio
async def test_a_healthy_connection_is_reused_without_reconnecting():
    """The common path must not pay for the rare one."""
    conn = _Conn(alive=True)
    tool = _Tool(conn)
    assert await ImapFetcher(tool).fetch_unread() == []
    assert tool.connects == 0
    assert conn.selects == 1


@pytest.mark.asyncio
async def test_it_retries_exactly_once_and_then_reports_honestly():
    """Two dead handles in a row is a real outage, not something to loop on."""
    tool = _Tool(_Conn(alive=False), _Conn(alive=False))
    with pytest.raises(MailFetchError) as exc:
        await ImapFetcher(tool).fetch_unread()
    assert tool.connects == 1
    assert "IMAP read failed" in str(exc.value)


@pytest.mark.asyncio
async def test_an_auth_failure_does_NOT_retry():
    """A wrong credential is not a stale socket. Retrying hammers the mailbox,
    and the durable verdict is what the owner needs to see."""
    tool = _Tool(_Conn(alive=False), auth_error=True)
    with pytest.raises(MailFetchError) as exc:
        await ImapFetcher(tool).fetch_unread()
    assert exc.value.auth is True
    assert tool.connects == 1   # the reconnect attempt itself raised; no second try


@pytest.mark.asyncio
async def test_a_missing_connection_still_connects_as_before():
    """The pre-existing cold-start path is untouched."""
    fresh = _Conn(alive=True)
    tool = _Tool(None, fresh)
    assert await ImapFetcher(tool).fetch_unread() == []
    assert tool.connects == 1
