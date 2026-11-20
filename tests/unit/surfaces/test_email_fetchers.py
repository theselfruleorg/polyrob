"""MailFetcher seam — ImapFetcher parity + AgentMailFetcher (Task 5, 2026-08-18 plan)."""
import email as email_lib

import pytest

from surfaces.email.dedup import MessageDedup
from surfaces.email.fetchers import AgentMailFetcher, ImapFetcher


# --- ImapFetcher parity ---------------------------------------------------

class _FakeImapConn:
    def __init__(self, raw_messages):
        self.raw = raw_messages  # {uid: rfc822 bytes}
        self.stored = []

    def select(self, folder):
        assert folder == "INBOX"
        return "OK", [b""]

    def response(self, code):
        return code, [b"9"]

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            assert args[-1] == "UNSEEN"
            return "OK", [b" ".join(k for k in self.raw)]
        if cmd == "FETCH":
            return "OK", [(b"", self.raw[args[0]])]
        if cmd == "STORE":
            self.stored.append(args)
            return "OK", []
        raise AssertionError(cmd)

    def logout(self):
        pass


class _FakeImapTool:
    def __init__(self, conn):
        self.imap_connection = None
        self._conn = conn

    async def ensure_initialized(self):
        return None

    def _mbox_open(self):
        return self._conn


RAW = (b"Message-ID: <m1@a.b>\r\nFrom: Al <al@a.b>\r\nSubject: Hi\r\n"
       b"Content-Type: text/plain\r\n\r\nbody line\r\n")


@pytest.mark.asyncio
async def test_imap_fetcher_normalizes_and_marks_seen():
    conn = _FakeImapConn({b"1": RAW})
    fetcher = ImapFetcher(_FakeImapTool(conn))
    out = await fetcher.fetch_unread()
    assert len(out) == 1
    handle, norm = out[0]
    assert norm["message_id"] == "<m1@a.b>"
    assert norm["subject"] == "Hi"
    assert "body line" in norm["body"]
    assert handle.key() == "uid:9:1"
    fetcher.mark_handled(handle)
    assert conn.stored == [(b"1", "+FLAGS", "(\\Seen)")]


# --- AgentMailFetcher -----------------------------------------------------

class _FakeAgentMail:
    def __init__(self, messages, threads=None):
        self._messages = messages
        self._threads = threads or {}
        self.address = "alice@agentmail.to"

    async def list_messages(self, limit=25):
        return [{"message_id": m["message_id"], "thread_id": m.get("thread_id")}
                for m in self._messages]

    async def get_message(self, mid):
        for m in self._messages:
            if m["message_id"] == mid:
                return m
        raise KeyError(mid)

    def minted_mid_for_thread(self, thread_id):
        return self._threads.get(thread_id)


def _msg(mid, thread_id="t1", **extra):
    base = {"message_id": mid, "thread_id": thread_id, "from": "Al <al@a.b>",
            "subject": "Hi", "text": "raw text", "extracted_text": "reply only",
            "timestamp": "2026-08-18T00:00:00Z"}
    base.update(extra)
    return base


@pytest.mark.asyncio
async def test_agentmail_fetcher_skips_own_outbound_and_dedups(tmp_path):
    dedup = MessageDedup(str(tmp_path / "dedup.db"))
    client = _FakeAgentMail([_msg("m1"), _msg("m2")])
    fetcher = AgentMailFetcher(client, dedup)

    out = await fetcher.fetch_unread()
    assert [n["message_id"] for _, n in out] == ["m1", "m2"]

    # The harness marks dedup on route; simulate then refetch -> nothing new.
    for _, norm in out:
        dedup.seen(norm["message_id"])  # returns False and records
    out2 = await fetcher.fetch_unread()
    assert out2 == []


@pytest.mark.asyncio
async def test_agentmail_fetcher_prefers_extracted_text(tmp_path):
    dedup = MessageDedup(str(tmp_path / "dedup.db"))
    fetcher = AgentMailFetcher(_FakeAgentMail([_msg("m1")]), dedup)
    (_, norm), = await fetcher.fetch_unread()
    assert norm["body"] == "reply only"
    assert norm["from"] == "Al <al@a.b>"


@pytest.mark.asyncio
async def test_agentmail_fetcher_synthesizes_thread_anchor(tmp_path):
    # Reply lost its In-Reply-To, but we sent into thread t9 with a minted mid.
    dedup = MessageDedup(str(tmp_path / "dedup.db"))
    client = _FakeAgentMail(
        [_msg("m1", thread_id="t9", in_reply_to="", references="")],
        threads={"t9": "<minted@agentmail.to>"})
    fetcher = AgentMailFetcher(client, dedup)
    (_, norm), = await fetcher.fetch_unread()
    assert norm["in_reply_to"] == "<minted@agentmail.to>"


@pytest.mark.asyncio
async def test_agentmail_fetcher_keeps_real_in_reply_to(tmp_path):
    dedup = MessageDedup(str(tmp_path / "dedup.db"))
    client = _FakeAgentMail(
        [_msg("m1", thread_id="t9", in_reply_to="<real@a.b>")],
        threads={"t9": "<minted@agentmail.to>"})
    fetcher = AgentMailFetcher(client, dedup)
    (_, norm), = await fetcher.fetch_unread()
    assert norm["in_reply_to"] == "<real@a.b>"


@pytest.mark.asyncio
async def test_agentmail_fetcher_skips_messages_from_self(tmp_path):
    dedup = MessageDedup(str(tmp_path / "dedup.db"))
    client = _FakeAgentMail(
        [_msg("m1", **{"from": "alice@agentmail.to"}), _msg("m2")])
    fetcher = AgentMailFetcher(client, dedup)
    out = await fetcher.fetch_unread()
    assert [n["message_id"] for _, n in out] == ["m2"]


# --- 057 WS-F: inbound must not wait on the SEND half ---------------------

class _SmtpDeadTool:
    """A tool whose SMTP login is refused but whose IMAP half is fine."""

    def __init__(self, conn):
        self.imap_connection = None
        self._conn = conn
        self.calls = []

    async def ensure_initialized(self):
        self.calls.append("full")
        raise RuntimeError("SMTP connection test failed: login rejected (535)")

    async def ensure_imap(self):
        self.calls.append("imap")

    def _mbox_open(self):
        return self._conn


@pytest.mark.asyncio
async def test_imap_fetcher_uses_the_receive_half_only():
    """A 535 on send used to stop inbound mail and log an ERROR every 60 s poll."""
    conn = _FakeImapConn({b"1": RAW})
    tool = _SmtpDeadTool(conn)
    out = await ImapFetcher(tool).fetch_unread()
    assert len(out) == 1
    assert tool.calls == ["imap"], "the poll must never run the SMTP probe"


@pytest.mark.asyncio
async def test_imap_fetcher_falls_back_for_a_tool_without_the_seam():
    """Duck-typed/legacy tools keep working (fail-open seam lookup)."""
    conn = _FakeImapConn({b"1": RAW})
    out = await ImapFetcher(_FakeImapTool(conn)).fetch_unread()
    assert len(out) == 1


# --- proposal 125 S35: each fetcher pins the authserv-id of its transport -----

_AUTH_RAW = (b"Authentication-Results: {id}; dmarc=pass header.from=a.b\r\n"
             b"Message-ID: <m9@a.b>\r\nFrom: Al <al@a.b>\r\nSubject: Hi\r\n"
             b"Content-Type: text/plain\r\n\r\nbody\r\n")


def _imap_tool(raw, host):
    tool = _FakeImapTool(_FakeImapConn({b"1": raw}))
    tool.imap_server = host
    return tool


@pytest.mark.asyncio
async def test_imap_fetcher_pins_gmail_authserv_id():
    ok = _AUTH_RAW.replace(b"{id}", b"mx.google.com")
    (_, norm), = await ImapFetcher(_imap_tool(ok, "imap.gmail.com")).fetch_unread()
    assert norm["sender_authenticated"] is True
    forged = _AUTH_RAW.replace(b"{id}", b"attacker.example")
    (_, norm), = await ImapFetcher(_imap_tool(forged, "imap.gmail.com")).fetch_unread()
    assert norm["sender_authenticated"] is False


@pytest.mark.asyncio
async def test_imap_fetcher_self_hosted_host_fails_closed():
    raw = _AUTH_RAW.replace(b"{id}", b"mail.self-hosted.example")
    (_, norm), = await ImapFetcher(_imap_tool(raw, "mail.self-hosted.example")).fetch_unread()
    assert norm["sender_authenticated"] is False


@pytest.mark.asyncio
async def test_agentmail_fetcher_passes_the_agentmail_row(tmp_path, monkeypatch):
    import tools.email_providers.mime as mime
    ordered = [["Authentication-Results", "mx.agentmail.example; dmarc=pass header.from=a.b"],
               ["Authentication-Results", "forged.example; dmarc=fail header.from=a.b"]]
    # Audit 2026-11-21 M-2: not pinned yet (empty) — no sender is authenticated.
    fetcher = AgentMailFetcher(_FakeAgentMail([_msg("m1", headers=ordered)]),
                               MessageDedup(str(tmp_path / "d1.db")))
    (_, norm), = await fetcher.fetch_unread()
    assert norm["sender_authenticated"] is False
    # Once the row is pinned, the topmost header with the pinned id counts.
    monkeypatch.setitem(mime.KNOWN_AUTHSERV, "agentmail", frozenset({"mx.agentmail.example"}))
    fetcher = AgentMailFetcher(_FakeAgentMail([_msg("m2", headers=ordered)]),
                               MessageDedup(str(tmp_path / "d2.db")))
    (_, norm), = await fetcher.fetch_unread()
    assert norm["sender_authenticated"] is True
    # A forged id on top is refused.
    fetcher = AgentMailFetcher(_FakeAgentMail([_msg("m3", headers=ordered[::-1])]),
                               MessageDedup(str(tmp_path / "d3.db")))
    (_, norm), = await fetcher.fetch_unread()
    assert norm["sender_authenticated"] is False


@pytest.mark.asyncio
async def test_agentmail_fetcher_refuses_an_unordered_header_map(tmp_path, monkeypatch):
    """A plain mapping cannot show the topmost header (M-2): no proof, even pinned."""
    import tools.email_providers.mime as mime
    monkeypatch.setitem(mime.KNOWN_AUTHSERV, "agentmail", frozenset({"mx.agentmail.example"}))
    hdrs = {"Authentication-Results": "mx.agentmail.example; dmarc=pass header.from=a.b"}
    fetcher = AgentMailFetcher(_FakeAgentMail([_msg("m4", headers=hdrs)]),
                               MessageDedup(str(tmp_path / "d4.db")))
    (_, norm), = await fetcher.fetch_unread()
    assert norm["sender_authenticated"] is False
