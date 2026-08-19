"""MailFetcher seam — ImapFetcher parity + AgentMailFetcher (Task 5, 2026-08-18 plan)."""
import email as email_lib

import pytest

from surfaces.email.dedup import MessageDedup
from surfaces.email.fetchers import AgentMailFetcher, ImapFetcher


# --- ImapFetcher parity ---------------------------------------------------

class _FakeImapConn:
    def __init__(self, raw_messages):
        self.raw = raw_messages  # {num: rfc822 bytes}
        self.stored = []

    def select(self, folder):
        assert folder == "INBOX"
        return "OK", [b""]

    def search(self, charset, criteria):
        assert criteria == "UNSEEN"
        return "OK", [b" ".join(k for k in self.raw)]

    def fetch(self, num, spec):
        return "OK", [(b"", self.raw[num])]

    def store(self, num, op, flag):
        self.stored.append((num, op, flag))


class _FakeImapTool:
    def __init__(self, conn):
        self.imap_connection = conn

    async def ensure_initialized(self):
        return None

    async def _connect_imap(self):
        return None


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
    fetcher.mark_handled(handle)
    assert conn.stored == [(b"1", "+FLAGS", "\\Seen")]


# --- AgentMailFetcher -----------------------------------------------------

class _FakeAgentMail:
    def __init__(self, messages, threads=None):
        self._messages = messages
        self._threads = threads or {}
        self.address = "rob@agentmail.to"

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
        [_msg("m1", **{"from": "rob@agentmail.to"}), _msg("m2")])
    fetcher = AgentMailFetcher(client, dedup)
    out = await fetcher.fetch_unread()
    assert [n["message_id"] for _, n in out] == ["m2"]
