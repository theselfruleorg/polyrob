"""Revalidation of the 2026-09-21 interface audit's email rails (D4 / D28).

Two defects the audit's own fixes left standing:

1. ``EmailTool.read_emails`` still fetched ``(RFC822)``. That sets ``\\Seen`` as
   a side effect of the FETCH, and the email SURFACE's inbound queue IS the
   UNSEEN set — so one agent-side read silently emptied the surface's queue and
   nothing was ever routed. D4 fixed exactly this shape one file over.

2. ``AgentMailFetcher`` had no ``_verdict_key``, so ``EmailHarness.
   _clear_fetch_outage`` cleared the standing ``imap`` verdict under ``""``
   while ``_note_fetch_outage`` had recorded it under the inbox ADDRESS. A
   recovered managed inbox left a permanent "INBOUND mailbox unreadable" WARN
   on every status seat.
"""
import inspect

import pytest


def test_read_emails_peeks_and_does_not_consume_the_surface_queue():
    """The read must not mark mail ``\\Seen``: the surface polls UNSEEN."""
    import tools.email_tool as et

    src = inspect.getsource(et.EmailTool.read_emails)
    assert "fetch(num, '(BODY.PEEK[])')" in src
    assert "fetch(num, '(RFC822)')" not in src


def test_only_mark_as_read_writes_the_seen_flag():
    """``mark_as_read`` stays the ONE place a message is marked handled."""
    import tools.email_tool as et

    writers = [name for name, fn in vars(et.EmailTool).items()
               if inspect.isfunction(fn) and "\\\\Seen" in inspect.getsource(fn)]
    assert writers == ["mark_as_read"], writers


def test_the_imap_fetch_response_tag_is_not_what_the_parser_keys_on():
    """PROOF over a real ``imaplib`` parse: a server answers ``BODY.PEEK[]``
    with a ``BODY[]`` tag, and the extraction is positional either way.

    A parser that keyed on the literal ``RFC822`` tag would have silently
    stopped returning mail the moment D4 changed the request.
    """
    import email as _email
    import imaplib
    import socket
    import threading

    raw = (b"From: a@b.c\r\nSubject: hi\r\nMessage-ID: <m1@x>\r\n\r\nbody\r\n")
    requested = []

    def serve(sock):
        conn, _ = sock.accept()
        stream = conn.makefile("rwb")
        conn.sendall(b"* OK fake ready\r\n")
        while True:
            line = stream.readline()
            if not line:
                return
            tag = line.split(b" ")[0]
            upper = line.upper()
            if b"CAPABILITY" in upper:
                conn.sendall(b"* CAPABILITY IMAP4rev1\r\n" + tag + b" OK done\r\n")
            elif b"LOGIN" in upper:
                conn.sendall(tag + b" OK done\r\n")
            elif b"SELECT" in upper:
                conn.sendall(b"* 1 EXISTS\r\n" + tag + b" OK [READ-WRITE] done\r\n")
            elif b"SEARCH" in upper:
                conn.sendall(b"* SEARCH 1\r\n" + tag + b" OK done\r\n")
            elif b"FETCH" in upper:
                requested.append(line.decode().strip())
                conn.sendall(b"* 1 FETCH (FLAGS () BODY[] {%d}\r\n" % len(raw)
                             + raw + b")\r\n" + tag + b" OK done\r\n")
            elif b"LOGOUT" in upper:
                conn.sendall(b"* BYE\r\n" + tag + b" OK done\r\n")
                return
            else:
                conn.sendall(tag + b" OK done\r\n")

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    threading.Thread(target=serve, args=(sock,), daemon=True).start()
    conn = imaplib.IMAP4("127.0.0.1", sock.getsockname()[1])
    try:
        conn.login("u", "p")
        conn.select("INBOX")
        _, nums = conn.search(None, "UNSEEN")
        _, data = conn.fetch(nums[0].split()[0], "(BODY.PEEK[])")
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    assert "BODY.PEEK[]" in requested[0]
    # The answer carries a BODY[] tag, NOT the request's name...
    assert b"BODY[]" in data[0][0] and b"BODY.PEEK" not in data[0][0]
    # ...and the bytes come out of the tuple by POSITION, which is what both
    # `ImapFetcher.fetch_unread` and `EmailTool.read_emails` do.
    assert _email.message_from_bytes(data[0][1]).get("Message-ID") == "<m1@x>"


def test_agentmail_records_and_clears_its_verdict_under_one_key():
    """Record and clear must name the SAME rail, in every ordering."""
    from surfaces.email.fetchers import AgentMailFetcher, ImapFetcher

    fetcher = AgentMailFetcher(client=object(), dedup=None)
    assert callable(getattr(fetcher, "_verdict_key", None))
    key = fetcher._verdict_key()
    assert key, "an empty key cannot name the rail the owner must fix"
    # Stable: the address is only known AFTER provisioning, so a provisioning
    # failure and a later list failure must still agree.
    assert fetcher._verdict_key() == key
    assert callable(getattr(ImapFetcher(object()), "_verdict_key", None))


@pytest.mark.asyncio
async def test_a_recovered_agentmail_inbox_clears_the_standing_verdict(monkeypatch, tmp_path):
    """The end-to-end shape: an outage writes a verdict, a good read clears it."""
    monkeypatch.setenv("VERDICTS_DB_PATH", str(tmp_path / "verdicts.db"))
    import core.credential_verdicts as cv

    from surfaces.email.fetchers import AgentMailFetcher, MailFetchError
    from surfaces.email.harness import EmailHarness

    fetcher = AgentMailFetcher(client=object(), dedup=None)
    harness = EmailHarness.__new__(EmailHarness)
    harness.fetcher = fetcher

    harness._note_fetch_outage(MailFetchError("401 unauthorized", auth=True,
                                              key=fetcher._verdict_key()))
    assert cv.verdict("imap", fetcher._verdict_key()) is not None
    harness._clear_fetch_outage()
    assert cv.verdict("imap", fetcher._verdict_key()) is None
